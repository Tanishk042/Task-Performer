"""The agent loop.

PLAN -> ACT -> OBSERVE -> REFLECT -> VERIFY, run repeatedly, with every call
funnelled through the safety gate first and every failure classified after.

Three things are load-bearing here:

1. **One action at a time, gated.** The model proposes a single tool call; the
   policy classifies it; the gate decides whether it may run. There is no path
   from the model to a side effect that skips the gate.

2. **`finish` is a claim, not an outcome.** When the model finishes, the loop
   does not end the run. It hands the success criteria and the claimed values to
   a verifier that re-derives the truth independently, and if that fails the
   model gets to repair — up to a bounded number of times.

3. **Failure is classified, not retried blindly.** The recovery ladder decides
   between retrying, backing off, re-snapshotting, changing approach, re-logging
   in, replanning, escalating or giving up. Blindly repeating a failing call is
   how agents burn their budget and lie about the result.
"""

from __future__ import annotations

import asyncio
import traceback
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from agent.llm import AssistantTurn, LLMClient, Message, ToolCall
from agent.planner import Planner, StepStatus
from agent.recovery import (
    FailureKind,
    RecoveryManager,
    Strategy,
    sleep,
)
from agent.safety import ApprovalStore, Policy
from agent.tools.base import ToolContext, ToolResult
from agent.trace import TraceEvent, Tracer
from agent.verifier import VerificationReport, Verifier
from common.logging_setup import get_logger

log = get_logger(__name__)


class LoopStatus(str, Enum):
    COMPLETED = "completed"
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


@dataclass
class StepRecord:
    index: int
    thought: str
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str
    error_code: str | None = None
    failure_kind: str = "none"
    strategy: str = ""
    duration_ms: int = 0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "at": self.at,
            "thought": self.thought[:600],
            "tool": self.tool,
            "args": _redact(self.args),
            "ok": self.ok,
            "summary": self.summary[:900],
            "error_code": self.error_code,
            "failure_kind": self.failure_kind,
            "strategy": self.strategy,
            "duration_ms": self.duration_ms,
        }


@dataclass
class LoopOutcome:
    status: str
    agent_status: str = ""
    summary: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    report: VerificationReport | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    repairs: int = 0
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in {LoopStatus.VERIFIED.value, LoopStatus.COMPLETED.value}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "agent_status": self.agent_status,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
            "verification": self.report.to_dict() if self.report else None,
            "tokens": self.tokens,
            "recovery": self.recovery,
            "plan": self.plan,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "repairs": self.repairs,
            "error": self.error,
        }


class AgentLoop:
    """Runs one task. One instance per run."""

    def __init__(
        self,
        *,
        goal: str,
        llm: LLMClient,
        registry: Any,
        context: ToolContext,
        tracer: Tracer,
        policy: Policy,
        approvals: ApprovalStore,
        planner: Planner,
        memory: Any,
        verifier: Verifier,
        recovery: RecoveryManager,
        system_prompt: str,
        max_steps: int = 40,
        max_repairs: int = 2,
        timeout_seconds: float = 600.0,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.goal = goal
        self.llm = llm
        self.registry = registry
        self.ctx = context
        self.tracer = tracer
        self.policy = policy
        self.approvals = approvals
        self.planner = planner
        self.memory = memory
        self.verifier = verifier
        self.recovery = recovery
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_repairs = max_repairs
        self.timeout_seconds = timeout_seconds
        self.emit = emit or (lambda *_: None)

        self.messages: list[Message] = []
        self.steps: list[StepRecord] = []
        self.finish_payload: dict[str, Any] | None = None
        self.pending_call: ToolCall | None = None
        self.tokens = {"input": 0, "output": 0}
        self._rejected_call: ToolCall | None = None

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    async def run(self, briefing: str) -> LoopOutcome:
        started = time.perf_counter()
        self.messages = [Message(role="user", content=briefing)]
        self.emit("run.started", {"goal": self.goal})

        status = LoopStatus.ERROR
        error = ""
        try:
            status = await self._drive_within_budget()
        except asyncio.TimeoutError:
            error = f"exceeded the {self.timeout_seconds:.0f}s time budget"
            self.tracer.emit(TraceEvent(kind="timeout", error=error))
            self.emit("run.timeout", {"error": error})
        except Exception as exc:  # noqa: BLE001 - surface, never swallow
            log.exception("agent loop crashed")
            error = f"{type(exc).__name__}: {exc}"
            self.tracer.emit(TraceEvent(kind="error", error=error))

        outcome = self._outcome(status, error, started)
        self.tracer.finish(
            status=outcome.status,
            steps=len(outcome.steps),
            repairs=len([s for s in outcome.steps if s.strategy]),
            report=outcome.report.to_dict() if outcome.report else None,
            criteria=[c.to_dict() for c in outcome.report.results]
            if outcome.report else None,
        )
        self.emit("run.finished", outcome.to_dict())
        return outcome

    async def _drive_within_budget(self) -> LoopStatus:
        """Run the loop on a wall-clock budget that ignores human thinking time.

        A plain `wait_for` would count every second a person spends staring at an
        approval prompt against the agent's deadline, which is exactly backwards:
        the budget exists to bound the *agent's* work, not to punish a slow
        reader. So the deadline is extended by whatever the interaction layer
        reports as parked. With a scripted interaction nothing parks and this is
        identical to `wait_for`.
        """
        interaction = (self.ctx.services or {}).get("interaction")
        drive = asyncio.ensure_future(self._drive())
        deadline = time.perf_counter() + self.timeout_seconds
        credited = 0.0
        try:
            while True:
                paused = float(getattr(interaction, "paused_seconds", 0.0) or 0.0)
                deadline += paused - credited
                credited = paused
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait({drive}, timeout=min(remaining, 0.25))
                if drive in done:
                    return drive.result()
        finally:
            if not drive.done():
                drive.cancel()
                try:
                    await drive
                except (asyncio.CancelledError, Exception):  # noqa: B014
                    pass

    async def _drive(self) -> LoopStatus:
        """One step at a time until the model finishes and verification agrees.

        Invariant: every assistant tool call is followed by exactly one user
        tool-result message. Anthropic rejects a transcript where a `tool_use`
        has no `tool_result`, so the pairing is structural here, not incidental.
        """
        repairs = 0
        self._repairs = 0

        while True:
            if len(self.steps) >= self.max_steps:
                return LoopStatus.BUDGET_EXHAUSTED

            turn = await self._think(self.max_steps - len(self.steps))
            if turn is None:
                return LoopStatus.ERROR

            if not turn.tool_calls:
                self.emit("step.note", {"text": turn.content[:400]})
                self.messages.append(Message(role="assistant", content=turn.content))
                self.messages.append(
                    Message(
                        role="user",
                        content=(
                            "You did not call a tool. Call exactly one tool, or call "
                            "finish if the work is done or genuinely impossible."
                        ),
                    )
                )
                if repairs >= self.max_repairs + 1:
                    return LoopStatus.FAILED
                repairs += 1
                continue

            call = turn.tool_calls[0]
            result = await self._act(turn, call)

            # Every tool_use gets exactly one tool_result, before any branching.
            # `finish` is no exception: skipping it here would leave a dangling
            # tool_use and the next turn would be rejected outright.
            if result is None:
                return LoopStatus.ERROR
            if result.ok and not result.error_code:
                self._record(turn, call, result, FailureKind.NONE, "")
                self._reply(call, result.content)
            else:
                await self._observe_failure(turn, call, result)
                continue

            if call.name != "finish":
                self.recovery.record_success()
                continue

            status = await self._verify(repairs)
            if status is not None:
                return status
            repairs += 1
            self._repairs = repairs
            continue

    def _reply(self, call: ToolCall, content: str, *, is_error: bool = False) -> None:
        """The mandatory tool_result turn."""
        self.messages.append(
            Message(role="user", tool_use_id=call.id, content=content, is_error=is_error)
        )

    # ------------------------------------------------------------------
    # think
    # ------------------------------------------------------------------
    async def _think(self, budget: int) -> AssistantTurn | None:
        context_text = self._render_context()
        tools = self.registry.schemas()
        try:
            turn = await self.llm.complete(
                system=f"{self.system_prompt}\n\n{context_text}",
                messages=self.messages,
                tools=tools,
            )
        except Exception as exc:  # noqa: BLE001
            self.tracer.bump_retry()
            detail = f"{type(exc).__name__}: {exc}"
            log.exception("model call failed")
            self.emit("llm.error", {"error": detail,
                                    "traceback": traceback.format_exc()[-2000:]})
            self.tracer.emit(TraceEvent(kind="llm_error", error=detail,
                                        text=traceback.format_exc()[-2000:]))
            self.messages.append(
                Message(role="user",
                        content=f"The model call failed ({exc}). Try again, more simply.")
            )
            return None

        self.tokens["input"] += turn.input_tokens
        self.tokens["output"] += turn.output_tokens
        self.tracer.add_tokens(turn.input_tokens, turn.output_tokens)

        self.messages.append(
            Message(role="assistant", content=turn.content, tool_calls=turn.tool_calls)
        )
        self.tracer.emit(
            TraceEvent(
                kind="thought",
                text=turn.content or "(no reasoning text)",
                tool=turn.tool_calls[0].name if turn.tool_calls else "",
            )
        )
        if turn.content:
            self.emit("thought", {"text": turn.content})
        return turn

    def _render_context(self) -> str:
        parts = [
            f"Steps used: {len(self.steps)} of {self.max_steps}.",
            f"Repairs used: {getattr(self, '_repairs', 0)} of {self.max_repairs}.",
        ]
        conflicts = self.memory.conflicts
        if conflicts:
            parts.append(
                "Your recorded facts disagree with each other. Resolve before writing: "
                + "; ".join(c.key for c in conflicts)
            )
        unsourced = self.memory.unsourced_keys
        if unsourced:
            parts.append("Recorded without provenance: " + ", ".join(unsourced))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # act
    # ------------------------------------------------------------------
    async def _act(self, turn: AssistantTurn, call: ToolCall) -> ToolResult | None:
        self.ctx.step = len(self.steps) + 1
        spec = self.registry.get(call.name) if self.registry.has(call.name) else None
        if spec is None:
            result = ToolResult(
                ok=False,
                content=(
                    f"There is no tool called {call.name!r}. Available tools: "
                    f"{', '.join(self.registry.names())}."
                ),
                error_code="unknown_tool",
            )
            self._record(turn, call, result, FailureKind.TOOL_EXCEPTION, "")
            return result

        # --- the gate ---------------------------------------------------
        snapshot = getattr(self.ctx.service("browser"), "last_snapshot", None)
        decision = self.policy.classify(
            call.name, call.input, snapshot=snapshot
        )
        allowed, reason = self.policy.enforce(decision, self.approvals)
        self.tracer.emit(
            TraceEvent(
                kind="safety",
                text=f"{call.name} risk={decision.risk.value}"
                        + (f" rules={','.join(r.value for r in decision.rules_fired)}"
                           if decision.rules_fired else ""),
                tool=call.name,
                payload={"allowed": allowed, "reason": reason,
                         "fingerprint": decision.fingerprint},
            )
        )
        self.emit(
            "safety.decision",
            {
                "tool": call.name,
                "risk": decision.risk.value,
                "allowed": allowed,
                "reason": reason,
                "rules": [r.value for r in decision.rules_fired],
            },
        )
        if not allowed:
            result = ToolResult(
                ok=False, content=reason, data={"fingerprint": decision.fingerprint},
                error_code="approval_required", approval_required=True,
            )
            self._rejected_call = call
            return result

        # --- execute ----------------------------------------------------
        result = await self._execute(spec, call)

        # The terminal tool's payload is what verification reads as the agent's
        # claims. Capture it here: the recorder service is the durable copy, but
        # the loop needs the values it just received.
        if spec.terminal and result.ok and result.data:
            self.finish_payload = dict(result.data)

        # A click that changed nothing is a failure the model must not ignore.
        if result.ok and not result.data.get("changed", True) and call.name in {
            "browser_click", "browser_type", "browser_set_value"
        }:
            if self.recovery.is_looping(call.name, call.input, result.content):
                result = ToolResult(
                    ok=True,
                    content=(
                        result.content
                        + "\n\nNOTE: that action has now produced this exact result "
                          "three times. Repeating it is not working — change approach "
                          "or ask the user."
                    ),
                    data=result.data,
                    error_code="loop_detected",
                    duration_ms=result.duration_ms,
                )
        return result

    async def _execute(self, spec: Any, call: ToolCall) -> ToolResult:
        start = time.perf_counter()
        try:
            result = await spec.handler(self.ctx, call.input)
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(
                ok=False,
                content=f"{type(exc).__name__}: {exc}",
                error_code="tool_exception",
            )
        if not result.duration_ms:
            result.duration_ms = int((time.perf_counter() - start) * 1000)
        return result

    # ------------------------------------------------------------------
    # record + observe failure
    # ------------------------------------------------------------------
    def _record(
        self,
        turn: AssistantTurn,
        call: ToolCall,
        result: ToolResult,
        failure_kind: FailureKind,
        strategy: str,
    ) -> StepRecord:
        record = StepRecord(
            index=len(self.steps) + 1,
            thought=turn.content,
            tool=call.name,
            args=dict(call.input),
            ok=result.ok and not result.error_code,
            summary=result.content,
            error_code=result.error_code,
            failure_kind=failure_kind.value,
            strategy=strategy,
            duration_ms=result.duration_ms,
        )
        self.steps.append(record)
        self.memory.record_step(
            record.index,
            goal=turn.content or "",
            outcome=record.summary[:280],
            tools=[call.name],
            error=record.error_code,
        )
        self.tracer.emit(
            TraceEvent(
                kind="action",
                text=result.content[:1200],
                tool=call.name,
                payload={"ok": record.ok, "error_code": result.error_code,
                         "failure_kind": failure_kind.value, "strategy": strategy,
                         "args": _redact(call.input)},
                duration_ms=result.duration_ms,
            )
        )
        self.emit("step", record.to_dict())
        return record

    async def _observe_failure(
        self, turn: AssistantTurn, call: ToolCall, result: ToolResult
    ) -> None:
        """Classify the failure, pick a strategy, and reply exactly once."""
        self.recovery.note_action(call.name, call.input)
        failure = self.recovery.classify(
            result, tool=call.name, args=call.input, page_text=result.content or ""
        )
        strategy = self.recovery.choose_strategy(
            failure, call.name, call.input, has_alternate=True
        )
        self.recovery.record_strategy(strategy)
        record = self._record(turn, call, result, failure.kind, strategy.value)
        self.emit("failure", {"kind": failure.kind.value, "message": failure.message,
                              "strategy": strategy.value})

        # -- retry family: re-drive the same call once, then answer ----------
        if strategy in {Strategy.BACKOFF, Strategy.RETRY_ONCE, Strategy.REFRESH_AND_RETRY}:
            if strategy is Strategy.REFRESH_AND_RETRY:
                await sleep(self.recovery.backoff_delay(record.index))
                snap = await self.ctx.service("browser").snapshot()
                self._reply(
                    call,
                    f"{result.content}\n\nThe page was re-read. "
                    "Element handles may have changed.",
                )
                self.emit("recovery", {"strategy": strategy.value,
                                       "detail": "re-read the page", "snapshot": snap.url})
                return

            await sleep(self.recovery.backoff_delay(record.index))
            replay = await self._act(turn, call)
            if replay is not None and replay.ok and not replay.error_code:
                self._record(turn, call, replay, FailureKind.NONE, "replayed")
                self._reply(call, replay.content)
                self.recovery.record_success()
                self.recovery.note_recovery()
                self.emit("recovery", {"strategy": strategy.value,
                                       "detail": "the retry worked"})
                return

            detail = (replay.content if replay is not None else result.content)
            self._reply(
                call,
                f"{detail}\n\nRetrying did not help. Change approach, or find another "
                "route to the same goal.",
                is_error=True,
            )
            self.emit("recovery", {"strategy": strategy.value, "detail": "retry failed"})
            return

        # -- session expired: the loop reopens the portal and tells the agent --
        if strategy is Strategy.RELOGIN:
            self.recovery.note_recovery()
            try:
                await self.ctx.service("browser").goto(self.ctx.service("portal_login_url"))
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("could not navigate back to sign-in: %s", exc)
            self._reply(
                call,
                "Your session expired and you are back at a sign-in page. Sign in "
                "again, then redo the step that failed.",
                is_error=True,
            )
            self.emit("recovery", {"strategy": strategy.value, "detail": "session expired"})
            return

        if strategy is Strategy.FORCE_REPLAN:
            self.recovery.note_replan()
            self._reply(
                call,
                f"That approach is not working ({failure.message}). Call update_plan "
                "with a different approach before continuing.",
                is_error=True,
            )
            self.emit("recovery", {"strategy": strategy.value, "detail": failure.message})
            return

        if strategy is Strategy.ALTERNATE_APPROACH:
            self.recovery.note_replan()
            self._reply(
                call,
                f"Stop repeating that ({failure.message}). Reach the same goal another "
                "way — a different page, control, or tool.",
                is_error=True,
            )
            self.emit("recovery", {"strategy": strategy.value, "detail": failure.message})
            return

        # escalate / give up
        self._reply(
            call,
            f"Stop that approach: {failure.message}. Either ask the user with ask_user, "
            "or call finish with status 'blocked' or 'failed' and say plainly what you "
            "did and what stopped you.",
            is_error=True,
        )
        self.emit("recovery", {"strategy": strategy.value, "detail": failure.message})

    # ------------------------------------------------------------------
    # verify + repair
    # ------------------------------------------------------------------
    async def _verify(self, repairs: int) -> LoopStatus | None:
        """Independent verification. Returns None to trigger a repair cycle."""
        payload = self.finish_payload or {}
        self.tracer.emit(
            TraceEvent(kind="finish", text=payload.get("summary", ""),
                       payload={"status": payload.get("status")})
        )
        self.emit("verify.started", {"criteria": len(self.planner.criteria)})

        claims = dict(payload.get("values") or {})
        claims["agent_status"] = payload.get("status", "")
        claims["agent_summary"] = payload.get("summary", "")

        report = await self.verifier.verify(
            self.planner.criteria, claims, repairs_used=repairs
        )
        self.tracer.emit(
            TraceEvent(
                kind="verification",
                text=report.summary,
                payload=report.to_dict(),
            )
        )
        self.emit("verify.finished", report.to_dict())
        self._report = report

        if report.status == "success":
            return LoopStatus.VERIFIED

        if repairs >= self.max_repairs:
            return {
                "partial": LoopStatus.PARTIAL,
                "failed": LoopStatus.FAILED,
                "blocked": LoopStatus.BLOCKED,
            }.get(report.status, LoopStatus.FAILED)

        self.emit("verify.repair", {"attempt": repairs + 1, "detail": report.summary})
        self.finish_payload = None
        self.messages.append(
            Message(
                role="user",
                content=_repair_prompt(report),
            )
        )
        return None

    # ------------------------------------------------------------------
    def _outcome(self, status: LoopStatus, error: str, started: float) -> LoopOutcome:
        payload = self.finish_payload or {}
        report = getattr(self, "_report", None)
        summary = payload.get("summary", "")
        if error:
            summary = f"{summary} ({error})".strip(" ()") or error
        elif report is not None and report.status != "success":
            summary = report.summary

        return LoopOutcome(
            status=status.value,
            agent_status=payload.get("status", ""),
            summary=summary,
            steps=self.steps,
            report=report,
            tokens=dict(self.tokens),
            recovery=self.recovery.snapshot(),
            plan=self.planner.plan.to_dict(),
            elapsed_seconds=time.perf_counter() - started,
            repairs=len([s for s in self.steps if s.tool == "finish"]),
            error=error,
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

SECRET_KEYS = {"password", "api_key", "token", "secret", "authorization"}


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Never let a password reach the trace or the UI."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if str(key).lower() in SECRET_KEYS:
            out[key] = "***"
        elif isinstance(value, str) and len(value) > 400:
            out[key] = value[:400] + "…"
        else:
            out[key] = value
    return out


def _repair_prompt(report: VerificationReport) -> str:
    lines = [
        "Independent verification did not confirm your result. This check does not "
        "trust your summary — it re-read the systems itself.",
        "",
        f"Verification result: {report.status}",
        f"{report.summary}",
        "",
        "Unconfirmed criteria:",
    ]
    for failure in report.failures():
        lines.append(
            f"  - {failure.criterion_id}: {failure.description}"
            + (f" | expected {failure.expected!r}, found {failure.actual!r}"
               if failure.expected is not None or failure.actual is not None else "")
            + (f" | {failure.detail}" if failure.detail else "")
        )
    lines += [
        "",
        "Go and check for yourself. Re-read the records, find out what actually "
        "happened, and either fix it or report honestly that it could not be done. "
        "Do not simply repeat the same action, and do not claim success again without "
        "reading the result.",
    ]
    return "\n".join(lines)