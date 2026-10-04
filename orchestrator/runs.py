"""Run lifecycle: start one task, watch it, let a human answer, persist the result.

The interesting part is `LiveInteraction`. A run that finishes in nine seconds
needs no coordination, but a run that stops to ask "the PDF says 7995 and the page
says 7275, which is right?" must be able to wait minutes for a click without
losing its place. So a session owns the interaction object, publishes a
`question` event when one opens, and wakes the parked loop from an HTTP handler
when an answer lands.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from agent.runtime import (
    LiveInteraction,
    RunConfig,
    RunHandle,
    build_run,
    confirm_with_browser,
)
from common.config import Settings, get_settings
from common.logging_setup import get_logger
from orchestrator.events import EventBus

log = get_logger(__name__)

#: Statuses a caller should never be allowed to sit on forever.
TERMINAL = frozenset({"completed", "partial", "failed", "error",
                       "blocked", "budget_exhausted", "cancelled", "interrupted"})

#: The one outcome worth spending a second browser on.
VERIFIED = "verified"

_INVOICE_KEYS = ("invoice_number", "invoice", "number")


def _claimed_bill(claim: dict[str, Any]) -> tuple[str, str]:
    """Pull (invoice_number, vendor) out of whatever shape a finish claim has.

    The claim is the agent's own output and its schema has moved once already
    (`invoice` at the top level, then `values.bills[].invoice_number`). Rather
    than pin the orchestrator to one shape and quietly stop confirming when the
    agent words it differently, look in the plausible places and take the first
    invoice number that looks like one.
    """
    containers: list[dict[str, Any]] = [claim]
    values = claim.get("values")
    if isinstance(values, dict):
        containers.append(values)
    for container in containers:
        bills = container.get("bills")
        if isinstance(bills, list) and bills and isinstance(bills[0], dict):
            bill = bills[0]
            for key in _INVOICE_KEYS:
                if bill.get(key):
                    return str(bill[key]), str(bill.get("vendor") or "")
        for key in _INVOICE_KEYS:
            if container.get(key):
                return str(container[key]), str(container.get("vendor") or "")
    return "", ""


class RunSession:
    def __init__(self, run_id: str, goal: str, settings: Settings,
                 *, headless: bool = True, max_steps: int = 0,
                 confirm_in_browser: bool = True) -> None:
        self.run_id = run_id
        self.goal = goal
        self.settings = settings
        self.headless = headless
        self.max_steps = max_steps
        self.confirm_in_browser = confirm_in_browser
        self.bus = EventBus()
        self.interaction = LiveInteraction(
            on_pending=self._on_question,
            timeout_seconds=max(120.0, settings.question_timeout_seconds),
        )
        self.status = "pending"
        self.outcome: dict[str, Any] | None = None
        self.confirmation: dict[str, Any] | None = None
        self.error: str = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.handle: RunHandle | None = None
        self.task: asyncio.Task | None = None

    # -- events ------------------------------------------------------------
    @property
    def run_dir(self) -> Path:
        return Path(self.settings.runs_dir) / self.run_id

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def _publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.bus.publish(event, data)

    def _on_question(self, question) -> None:
        """Called on the loop's thread the moment a question opens."""
        if self.status == "running":
            self.status = "waiting"
        self._publish("question", question.to_dict())

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        """Bridge the loop's emit callback onto the bus, with UI-facing extras."""
        self._publish(event, data)
        if event == "step" and self.handle is not None:
            # The loop knows nothing about panels, so the session reads plan and
            # memory on its own schedule rather than teaching the loop about the UI.
            self._publish("state", self._ui_state())

    def _ui_state(self) -> dict[str, Any]:
        handle = self.handle
        if handle is None:
            return {}
        planner = getattr(handle.loop, "planner", None)
        memory = getattr(handle.loop, "memory", None)
        plan = planner.plan.to_dict() if planner is not None else {}
        facts = []
        if memory is not None:
            facts = [f.to_dict() for f in list(memory.facts.values())[:24]]
        return {"plan": plan, "facts": facts,
                "status": self.status, "steps": len(plan.get("steps", []))}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.started_at = time.time()
        self.status = "running"
        self.task = asyncio.create_task(self._run(), name=f"aiworker-{self.run_id}")
        self._publish("run.state", self.summary())

    async def _run(self) -> None:
        config = RunConfig(
            goal=self.goal,
            run_id=self.run_id,
            interaction_mode="live",
            headless=self.headless,
            max_steps=self.max_steps,
            timeout_seconds=self.settings.agent_timeout_seconds,
            settings=self.settings,
        )
        try:
            self.handle = await build_run(config, emit=self._emit,
                                          settings=self.settings,
                                          interaction=self.interaction)
            outcome = await self.handle.loop.run(self.handle.loop.briefing)
            self.outcome = outcome.to_dict()
            self.status = outcome.status
            if self.confirm_in_browser and outcome.status == VERIFIED:
                await self._confirm()
        except asyncio.CancelledError:
            self.status = "cancelled"
            self._publish("run.state", self.summary())
            raise
        except Exception as exc:                      # noqa: BLE001
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("run %s crashed", self.run_id)
            self._publish("run.error", {"error": self.error})
        finally:
            self.finished_at = time.time()
            try:
                if self.handle is not None:
                    await self.handle.aclose()
            except Exception:                         # noqa: BLE001
                log.exception("teardown failed for %s", self.run_id)
            self._persist()
            self._publish("run.state", self.summary())

    async def _confirm(self) -> None:
        """Re-open the AP system from a clean browser and look for the bill.

        The verifier already read the database. This is the check that a *person*
        would see it, which is the only way to catch a write that landed in a
        column the ledger page never renders.
        """
        if self.handle is None:
            return
        invoice, vendor = _claimed_bill(self.handle.loop.finish_payload or {})
        if not invoice:
            return
        try:
            self.confirmation = await confirm_with_browser(
                self.handle, invoice, vendor, settings=self.settings)
            self._publish("confirm", self.confirmation)
        except Exception as exc:                      # noqa: BLE001
            log.warning("browser confirmation failed for %s: %s", invoice, exc)
            self.confirmation = {"invoice_number": invoice, "visible": False,
                                 "ok": False, "detail": str(exc)}
            self._publish("confirm", self.confirmation)

    async def answer(self, question_id: str, answer: str) -> bool:
        delivered = self.interaction.respond(question_id, answer)
        if delivered and self.status == "waiting":
            self.status = "running"
            self._publish("question.resolved",
                          {"id": question_id, "status": self.status})
        return delivered

    async def cancel(self) -> bool:
        if not self.running or self.task is None:
            return False
        self.interaction.release_all("")
        self.task.cancel()
        try:
            await self.task
        except BaseException:                      # noqa: BLE001 - cancellation is the point
            pass
        return True

    # -- projection for the UI ---------------------------------------------
    def summary(self) -> dict[str, Any]:
        steps = (self.outcome or {}).get("steps") or []
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(
                (self.finished_at or time.time()) - (self.started_at or time.time()), 1),
            "error": self.error,
            "steps": len(steps),
            "outcome": self.outcome,
            "confirmation": self.confirmation,
            "pending": self.interaction.pending(),
            "questions": [q.to_dict() for q in self.interaction.questions],
            "run_dir": str(self.run_dir),
        }

    def _persist(self) -> None:
        """Write a flat summary next to the trace, for the run history list."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            payload = self.summary()
            payload.pop("outcome", None)      # the trace already holds every step
            (self.run_dir / "session.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except OSError:
            log.warning("could not persist session for %s", self.run_id)


class RunManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._sessions: dict[str, RunSession] = {}

    def get(self, run_id: str) -> RunSession | None:
        return self._sessions.get(run_id)

    def sessions(self) -> list[RunSession]:
        return list(self._sessions.values())

    async def start(self, goal: str, *, run_id: str = "", headless: bool = True,
                    max_steps: int = 0,
                    confirm_in_browser: bool = True) -> RunSession:
        from agent.runtime import new_run_id
        session = RunSession(
            run_id=run_id or new_run_id(), goal=goal, settings=self.settings,
            headless=headless, max_steps=max_steps,
            confirm_in_browser=confirm_in_browser)
        self._sessions[session.run_id] = session
        await session.start()
        return session

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            if session.running:
                await session.cancel()