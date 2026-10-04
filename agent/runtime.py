"""Run assembly: everything one task needs, wired together.

Kept separate from the loop so the same code serves the CLI, the HTTP API and the
eval harness. A run is constructed, executed, and torn down, and nothing about
the vendor portal or the AP system leaks in here beyond settings and paths.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent.context import SYSTEM_PROMPT, build_briefing
from agent.ground_truth import BrowserConfirmer, SqliteGroundTruth
from agent.llm import build_llm_client
from agent.loop import AgentLoop, LoopOutcome
from agent.memory import WorkingMemory
from agent.planner import Planner
from agent.recovery import RecoveryConfig, RecoveryManager
from agent.safety import ApprovalStore, Policy
from agent.tools.base import ToolContext
from agent.tools.browser import BrowserSession
from agent.tools.registry import build_registry
from agent.trace import Tracer
from agent.verifier import Verifier
from common.config import Settings, get_settings, load_policy
from common.logging_setup import get_logger

log = get_logger(__name__)


def new_run_id() -> str:
    """Sortable, unique, and readable in a directory listing."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


# --------------------------------------------------------------------------
# interaction: how a human answers, when there is one
# --------------------------------------------------------------------------

@dataclass
class PendingQuestion:
    kind: str                 # "ask" | "approval"
    question: str
    options: list[str] = field(default_factory=list)
    context: str = ""
    fingerprint: str = ""
    justification: str = ""
    created_at: float = 0.0
    answered: str = ""
    approved: bool | None = None
    #: Correlates a question with the answer that unblocks it. Only `LiveInteraction`
    #: needs this, but it lives on the dataclass so any consumer can address a
    #: question without caring which interaction class produced it.
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "question": self.question,
            "options": list(self.options),
            "context": self.context,
            "fingerprint": self.fingerprint,
            "justification": self.justification,
            "created_at": self.created_at,
            "answered": self.answered,
            "approved": self.approved,
        }


@dataclass
class ApprovalAnswer:
    """What a human said about a risky action."""

    granted: bool
    by: str = "user"
    reason: str = ""


class ApprovalGateway:
    """The approvals service, shared by the policy gate and the tool wrapper.

    The policy asks `has(fingerprint)` before letting a write through, while the
    `request_approval` tool uses this same object to go and get a human. Keeping
    one object means an approval can never be granted to one component and
    invisible to the other.
    """

    def __init__(self, interaction: "Interaction") -> None:
        self.store = ApprovalStore()
        self.interaction = interaction

    # -- used by Policy.enforce -----------------------------------------
    def has(self, fingerprint: str) -> bool:
        return self.store.has(fingerprint)

    def get(self, fingerprint: str) -> Any:
        return self.store.get(fingerprint)

    def grant(self, fingerprint: str, tool: str, description: str,
              granted_by: str = "user", reason: str = "") -> Any:
        return self.store.grant(fingerprint, tool, description, granted_by, reason)

    def deny(self, fingerprint: str, reason: str = "") -> None:
        self.store.deny(fingerprint, reason)

    def clear(self) -> None:
        self.store.clear()

    # -- used by the request_approval tool -------------------------------
    async def request(self, ctx: ToolContext, action: str, fingerprint: str,
                      justification: str) -> ApprovalAnswer:
        if self.store.has(fingerprint):
            return ApprovalAnswer(True, by="previous approval",
                                  reason="already approved for this exact action")
        answer = await self.interaction.ask_approval(
            ctx, action=action, fingerprint=fingerprint,
            justification=justification,
        )
        # Recording the decision is the whole point of asking. Without this the
        # gate would keep refusing the very call the human just approved.
        if answer.granted:
            self.store.grant(fingerprint, "irreversible_write", action,
                             granted_by=answer.by, reason=answer.reason)
        else:
            self.store.deny(fingerprint, answer.reason or "declined")
        self.interaction.record(
            PendingQuestion(kind="approval", question=action, fingerprint=fingerprint,
                            justification=justification, approved=answer.granted,
                            answered="approved" if answer.granted else "denied",
                            created_at=time.time())
        )
        log.info("approval %s (%s) -> %s", action[:80], fingerprint[:12], answer.granted)
        return answer


class Interaction:
    """Where a human answers.

    The default answers from a script, which is what the CLI in batch mode and
    the eval harness want. The orchestrator swaps in a subclass that waits on a
    browser tab instead.
    """

    def __init__(self, *, auto_approve: bool = True,
                 default_answer: str = "", max_questions: int = 6) -> None:
        self.auto_approve = auto_approve
        self.default_answer = default_answer
        self.max_questions = max_questions
        self.questions: list[PendingQuestion] = []
        self.answers: list[str] = []
        self._canned: list[str] = []

    def queue_answer(self, text: str) -> None:
        """Pre-seed one answer, so an eval can script a human decision."""
        self._canned.append(text)

    def record(self, question: PendingQuestion) -> None:
        self.questions.append(question)

    async def ask(self, ctx: ToolContext, question: str, options: list[str],
                  context: str = "") -> str:
        answer = self._resolve(question, options)
        self.record(PendingQuestion(kind="ask", question=question, options=options,
                                    context=context, answered=answer,
                                    created_at=time.time()))
        if answer:
            self.answers.append(answer)
        log.info("ask_user: %s -> %s", question[:110], (answer or "<silent>")[:60])
        return answer

    async def ask_approval(self, ctx: ToolContext, *, action: str, fingerprint: str,
                           justification: str) -> ApprovalAnswer:
        granted = self._approve(action, justification)
        return ApprovalAnswer(
            granted=granted,
            by="scripted interaction",
            reason=("approved by the run's interaction script" if granted
                    else "declined by the run's interaction script"),
        )

    # -- overridable ------------------------------------------------------
    def _resolve(self, question: str, options: list[str]) -> str:
        if self._canned:
            return self._canned.pop(0)
        for option in options:
            # Prefer an option that defers to the authoritative document.
            if re.search(r"pdf|authoritative|document", option, re.I):
                return option
        if options:
            return options[0]
        return self.default_answer

    def _approve(self, action: str, justification: str) -> bool:
        return self.auto_approve


class NullInteraction(Interaction):
    """Refuses everything and answers nothing — the strictest headless mode."""

    def _resolve(self, question: str, options: list[str]) -> str:
        return ""

    def _approve(self, action: str, justification: str) -> bool:
        return False


#: Answers that count as "yes" for an approval prompt.
_APPROVAL_YES = {"approve", "approved", "yes", "y", "allow", "grant", "ok"}


class LiveInteraction(Interaction):
    """Parks the run until a human answers. This is what the orchestrator drives.

    `Interaction` answers from a script, which is right for a batch run and wrong
    for a person watching a browser tab. Here the loop suspends on an
    `asyncio.Future` that `respond()` completes, so the run keeps its place — no
    state is discarded, no re-planning happens, the agent is simply still holding
    the question when the answer arrives.

    Two safety rules are baked in, because "nobody clicked the button" is a
    realistic failure mode for an unattended HTTP service:

    * an approval that times out is **denied**, never waved through;
    * a question that times out answers **empty**, which the prompt treats as "no
      information" so the agent abstains instead of inventing a value.
    """

    def __init__(self, *, default_answer: str = "", max_questions: int = 12,
                 timeout_seconds: float = 900.0,
                 on_pending: Callable[[PendingQuestion], None] | None = None) -> None:
        super().__init__(auto_approve=False, default_answer=default_answer,
                         max_questions=max_questions)
        self.timeout_seconds = timeout_seconds
        self.on_pending = on_pending
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._pending: dict[str, PendingQuestion] = {}
        #: Total wall-clock spent parked. The loop credits this back against its
        #: deadline — a run is not late because a person is thinking.
        self.paused_seconds: float = 0.0

    # -- what the orchestrator calls --------------------------------------
    def pending(self) -> list[dict[str, Any]]:
        """Questions currently blocking the run, oldest first."""
        return [q.to_dict() for q in
                sorted(self._pending.values(), key=lambda q: q.created_at)]

    def respond(self, question_id: str, answer: str) -> bool:
        """Unblock one question. Returns False if it is not awaiting an answer."""
        waiter = self._waiters.get(question_id)
        if waiter is None or waiter.done():
            return False
        waiter.set_result(answer)
        return True

    def release_all(self, answer: str = "") -> None:
        """Unblock everything, e.g. when a run is being cancelled."""
        for question_id in list(self._waiters):
            self.respond(question_id, answer)

    # -- internals ---------------------------------------------------------
    async def _park(self, question: PendingQuestion) -> str | None:
        """Suspend until answered, or return None if the human never showed up."""
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[question.id] = waiter
        self._pending[question.id] = question
        if self.on_pending is not None:
            try:
                self.on_pending(question)
            except Exception:            # a broken UI hook must not kill the run
                log.exception("on_pending callback failed for %s", question.id)
        log.info("%s awaiting human: %s", question.kind, question.question[:110])
        parked_at = time.monotonic()
        try:
            return await asyncio.wait_for(waiter, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("%s %s timed out after %.0fs",
                        question.kind, question.id, self.timeout_seconds)
            return None
        finally:
            self.paused_seconds += time.monotonic() - parked_at
            self._waiters.pop(question.id, None)
            self._pending.pop(question.id, None)

    async def ask(self, ctx: ToolContext, question: str, options: list[str],
                  context: str = "") -> str:
        pending = PendingQuestion(
            kind="ask", id=f"q-{secrets.token_hex(3)}", question=question,
            options=list(options), context=context, created_at=time.time())
        answer = await self._park(pending)
        if answer is None:
            answer = ""
        pending.answered = answer
        self.record(pending)
        if answer:
            self.answers.append(answer)
        log.info("ask_user: %s -> %s", question[:110], (answer or "<no answer>")[:60])
        return answer

    async def ask_approval(self, ctx: ToolContext, *, action: str, fingerprint: str,
                           justification: str) -> ApprovalAnswer:
        pending = PendingQuestion(
            kind="approval", id=f"a-{secrets.token_hex(3)}", question=action,
            fingerprint=fingerprint, justification=justification,
            created_at=time.time())
        choice = await self._park(pending)
        # Timeout => no answer => deny. An unattended service must never be able
        # to approve itself by accident.
        granted = bool(choice) and choice.strip().lower() in _APPROVAL_YES
        pending.answered = choice or ""
        pending.approved = granted
        self.record(pending)
        if choice:
            self.answers.append(choice)
        result = ApprovalAnswer(
            granted=granted, by="live user",
            reason=(choice or "no response; approval timed out and was denied"))
        log.info("approval %s -> %s", action[:80], granted)
        return result


# --------------------------------------------------------------------------
# finish payload holder
# --------------------------------------------------------------------------

class FinishRecorder:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def set(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)

    def get(self) -> dict[str, Any] | None:
        return self.payload


# --------------------------------------------------------------------------
# run config
# --------------------------------------------------------------------------

#: How a run reaches a human.
#:
#: ``live``     — a person is watching and answers (the UI, or a terminal run).
#: ``scripted`` — headless, answers come from the run's script or canned list.
#: ``strict``   — nobody answers: every question and every write is refused.
INTERACTION_MODES = ("live", "scripted", "strict")


@dataclass
class RunConfig:
    goal: str
    run_id: str = ""
    #: live | scripted | strict
    interaction_mode: str = "live"
    headless: bool = True
    max_steps: int = 0            # 0 = use settings.agent_max_steps
    max_repairs: int = 2
    timeout_seconds: float = 600.0
    auto_approve: bool = True
    scripted_answers: list[str] = field(default_factory=list)
    settings: Settings = field(default_factory=get_settings)
    extra_services: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunHandle:
    run_id: str
    run_dir: Path
    tracer: Tracer
    interaction: Interaction
    browser: BrowserSession
    loop: AgentLoop
    verifier: Verifier

    async def aclose(self) -> None:
        await self.browser.close()

    @property
    def questions(self) -> list[PendingQuestion]:
        return self.interaction.questions


# --------------------------------------------------------------------------
# builder
# --------------------------------------------------------------------------

async def build_run(
    config: RunConfig,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    settings: Settings | None = None,
    interaction: Interaction | None = None,
) -> RunHandle:
    settings = settings or config.settings
    run_id = config.run_id or new_run_id()

    tracer = Tracer(
        run_id=run_id,
        goal=config.goal,
        run_dir=Path(settings.runs_dir) / run_id,
        db_path=settings.runs_db_path,
        provider=settings.llm_provider,
        model=settings.anthropic_model,
    )

    registry = build_registry()
    memory = WorkingMemory()
    planner = Planner(goal=config.goal)
    recovery = RecoveryManager(RecoveryConfig())
    policy = Policy(load_policy(), registry=registry)
    finish = FinishRecorder()

    mode = config.interaction_mode
    if mode not in INTERACTION_MODES:
        raise ValueError(f"interaction_mode must be one of {INTERACTION_MODES}, got {mode!r}")
    if interaction is None:
        # Only `strict` blocks the question tools outright; a scripted run still
        # needs ask_user and request_approval to work, they just answer themselves.
        interaction_cls = NullInteraction if mode == "strict" else Interaction
        interaction = interaction_cls(auto_approve=config.auto_approve)
        for answer in config.scripted_answers:
            interaction.queue_answer(answer)
    approvals = ApprovalGateway(interaction)

    browser = BrowserSession(
        headless=config.headless,
        timeout_ms=settings.browser_timeout_ms,
        run_dir=tracer.run_dir,
        label="agent",
    )
    await browser.start()

    ctx = ToolContext(
        run_id=tracer.run_id,
        workspace_dir=settings.workspace_dir,
        run_dir=tracer.run_dir,
        settings=settings,
        services={
            "browser": browser,
            "memory": memory,
            "planner": planner,
            "policy": policy,
            "approvals": approvals,
            "interaction": interaction,
            "finish": finish,
            "tracer": tracer,
            "portal_login_url": f"{settings.vendor_portal_url}/login",
            **config.extra_services,
        },
        trace=tracer,
        interactive=mode != "strict",
        emit=emit,
    )

    ground_truth = SqliteGroundTruth(
        ap_db_path=settings.ap_db_path,
        portal_db_path=settings.vendor_db_path,
    )
    # Give the verifier the interaction layer's own record of what a human was
    # asked and answered. This is an audit log held by the harness, not a claim
    # from the model, so it is fair game for independent checks. Read lazily:
    # questions are still being asked while the run is in progress.
    ground_truth.authorizations = lambda: [
        {
            "invoice": q.question,
            "question": q.question,
            "options": list(q.options),
            "answer": q.answered,
        }
        for q in interaction.questions
        if q.kind == "ask"
    ]
    verifier = Verifier(ground_truth_provider=ground_truth)

    loop = AgentLoop(
        goal=config.goal,
        llm=build_llm_client(settings),
        registry=registry,
        context=ctx,
        tracer=tracer,
        policy=policy,
        approvals=approvals,
        planner=planner,
        memory=memory,
        verifier=verifier,
        recovery=recovery,
        system_prompt=SYSTEM_PROMPT,
        max_steps=config.max_steps or settings.agent_max_steps,
        max_repairs=config.max_repairs,
        timeout_seconds=config.timeout_seconds,
        emit=emit,
    )
    loop.briefing = build_briefing(config.goal, settings)
    loop.ground_truth = ground_truth

    return RunHandle(
        run_id=tracer.run_id,
        run_dir=tracer.run_dir,
        tracer=tracer,
        interaction=interaction,
        browser=browser,
        loop=loop,
        verifier=verifier,
    )


async def execute(
    config: RunConfig,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    settings: Settings | None = None,
) -> tuple[LoopOutcome, RunHandle]:
    handle = await build_run(config, emit=emit, settings=settings)
    try:
        outcome = await handle.loop.run(handle.loop.briefing)
        return outcome, handle
    finally:
        await handle.aclose()


async def confirm_with_browser(handle: RunHandle, invoice_number: str,
                              vendor_name: str = "",
                              settings: Settings | None = None) -> dict[str, Any]:
    """Second opinion for the UI: does a person see the bill? Run after the loop."""
    settings = settings or get_settings()
    async with BrowserConfirmer(settings=settings, run_dir=handle.run_dir) as confirmer:
        return await confirmer.confirm_bill_visible(invoice_number, vendor_name)