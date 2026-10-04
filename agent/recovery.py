"""Failure detection and recovery strategies.

The hard part of an agent loop is not acting, it is *noticing* that the last
action did nothing. This module owns that noticing:

* failures classified from tool exceptions, HTTP status, error codes,
  validation text on the page, unchanged page state, and session-expired
  redirects;
* a strategy ladder — retry once after a fresh snapshot, then backoff for
  transient errors, then alternate approach, then force a replan, then escalate;
* loop detection on (action fingerprint, observation fingerprint) so the agent
  cannot burn its whole budget re-clicking a dead button.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.tools.base import ToolResult
from common.logging_setup import get_logger

log = get_logger(__name__)


class FailureKind(str, Enum):
    NONE = "none"
    TOOL_EXCEPTION = "tool_exception"
    TRANSIENT = "transient"
    HTTP_CLIENT = "http_client_error"
    HTTP_SERVER = "http_server_error"
    VALIDATION = "validation_error"
    SESSION_EXPIRED = "session_expired"
    NO_STATE_CHANGE = "no_state_change"
    STALE_REF = "stale_ref"
    UNKNOWN_REF = "unknown_ref"
    TIMEOUT = "timeout"
    LOOP = "loop"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_REQUIRED = "approval_required"
    AMBIGUITY = "ambiguity"


class Strategy(str, Enum):
    RETRY_ONCE = "retry_once"
    BACKOFF = "backoff"
    REFRESH_AND_RETRY = "refresh_and_retry"
    ALTERNATE_APPROACH = "alternate_approach"
    RELOGIN = "relogin"
    FORCE_REPLAN = "force_replan"
    ESCALATE = "escalate"
    GIVE_UP = "give_up"


#: Transient HTTP statuses worth retrying.
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}

SESSION_EXPIRED_HINTS = re.compile(
    r"(session (?:has )?(?:expired|ended)|please (?:sign|log) ?in|unauthori[sz]ed|"
    r"sign in to continue|your session timed out)",
    re.IGNORECASE,
)

VALIDATION_HINTS = re.compile(
    r"(already entered|already exists|duplicate|must be|is required|required\.|"
    r"must be in|invalid|not recognised|was not saved|cannot be)",
    re.IGNORECASE,
)

TRANSIENT_HINTS = re.compile(
    r"(temporarily unavailable|service unavailable|try again|timeout|timed out|"
    r"rate limit|502|503|bad gateway|connection reset|net::err)",
    re.IGNORECASE,
)


@dataclass
class Failure:
    kind: FailureKind
    message: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    observation: Any = None
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message[:400],
            "tool": self.tool,
            "retryable": self.retryable,
            "hints": self.hints,
        }


@dataclass
class RecoveryConfig:
    max_same_action_repeats: int = 3
    max_consecutive_failures: int = 5
    backoff_seconds: tuple[float, ...] = (0.4, 1.2, 3.0)
    max_replans: int = 3


@dataclass
class RecoveryState:
    consecutive_failures: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    loop_counts: dict[str, int] = field(default_factory=dict)
    replans: int = 0
    strategies_used: list[str] = field(default_factory=list)
    recoveries: int = 0


class RecoveryManager:
    """Stateful per-run recovery brain."""

    def __init__(self, config: RecoveryConfig | None = None) -> None:
        self.config = config or RecoveryConfig()
        self.state = RecoveryState()

    # -- detection --------------------------------------------------------
    @staticmethod
    def classify(
        result: ToolResult,
        *,
        tool: str = "",
        args: dict[str, Any] | None = None,
        page_text: str = "",
        page_url: str = "",
    ) -> Failure:
        """Turn a tool result (and page context) into a classified failure."""
        args = args or {}
        obs = result.data or {}
        url = page_url or obs.get("url", "")

        if result.error_code == "approval_required":
            return Failure(FailureKind.APPROVAL_REQUIRED, result.content, tool, args,
                           retryable=False)
        if result.error_code == "approval_denied":
            return Failure(FailureKind.APPROVAL_DENIED, result.content, tool, args,
                           retryable=False)
        if result.error_code in {"unknown_ref", "stale_ref"}:
            kind = (FailureKind.UNKNOWN_REF if result.error_code == "unknown_ref"
                    else FailureKind.STALE_REF)
            return Failure(kind, result.content, tool, args, retryable=True)

        if not result.ok:
            status = obs.get("status")
            if isinstance(status, int):
                if status in TRANSIENT_STATUS:
                    return Failure(FailureKind.TRANSIENT,
                                   f"HTTP {status}: {result.content[:200]}", tool, args)
                if 500 <= status < 600:
                    return Failure(FailureKind.HTTP_SERVER,
                                   f"HTTP {status}: {result.content[:200]}", tool, args,
                                   retryable=False)
                if 400 <= status < 500:
                    kind = (FailureKind.SESSION_EXPIRED
                            if status in {401, 403} else FailureKind.HTTP_CLIENT)
                    return Failure(kind, f"HTTP {status}: {result.content[:200]}", tool, args,
                                   retryable=status in {401, 403, 409, 422})
            if TRANSIENT_HINTS.search(result.content):
                return Failure(FailureKind.TRANSIENT, result.content[:300], tool, args)
            if result.error_code in {"timeout", "navigation_failed", "wait_timeout"}:
                return Failure(FailureKind.TIMEOUT, result.content[:300], tool, args)
            return Failure(FailureKind.TOOL_EXCEPTION, result.content[:300], tool, args,
                           retryable=False)

        # The call "succeeded" — but did it actually do anything?
        if result.error_code == "unchanged_state":
            return Failure(FailureKind.NO_STATE_CHANGE,
                           obs.get("detail", "page state did not change"), tool, args)

        if url and "/login" in url.lower():
            return Failure(FailureKind.SESSION_EXPIRED,
                           f"redirected to the login page ({url})", tool, args)
        if page_text and SESSION_EXPIRED_HINTS.search(page_text[:1500]):
            return Failure(FailureKind.SESSION_EXPIRED, "session-expired message on page",
                           tool, args)
        if page_text and VALIDATION_HINTS.search(page_text[-1800:]):
            return Failure(FailureKind.VALIDATION,
                           _extract_validation(page_text) or "form validation error",
                           tool, args, retryable=False)
        return Failure(FailureKind.NONE, "")

    # -- loop detection ---------------------------------------------------
    def note_action(self, tool: str, args: dict[str, Any]) -> str:
        key = _action_key(tool, args)
        self.state.action_counts[key] = self.state.action_counts.get(key, 0) + 1
        return key

    def is_looping(self, tool: str, args: dict[str, Any], observation: str = "") -> bool:
        """True once the same action has produced the same observation N times."""
        combo = f"{_action_key(tool, args)}::{observation[:400]}"
        count = self.state.loop_counts.get(combo, 0) + 1
        self.state.loop_counts[combo] = count
        return count >= self.config.max_same_action_repeats

    def repeated(self, tool: str, args: dict[str, Any]) -> int:
        return self.state.action_counts.get(_action_key(tool, args), 0)

    # -- strategy selection ----------------------------------------------
    def choose_strategy(
        self,
        failure: Failure,
        tool: str,
        args: dict[str, Any],
        *,
        has_alternate: bool = False,
    ) -> Strategy:
        """The ladder, in order. Deliberately explicit rather than clever."""
        s = self.state

        if failure.kind is FailureKind.APPROVAL_DENIED:
            return Strategy.GIVE_UP
        if failure.kind is FailureKind.APPROVAL_REQUIRED:
            return Strategy.ESCALATE
        if failure.kind in {FailureKind.UNKNOWN_REF, FailureKind.STALE_REF}:
            return (Strategy.REFRESH_AND_RETRY
                    if self.repeated(tool, args) <= 1 else Strategy.FORCE_REPLAN)
        if failure.kind is FailureKind.SESSION_EXPIRED:
            return Strategy.RELOGIN
        if failure.kind is FailureKind.LOOP:
            return Strategy.FORCE_REPLAN
        if failure.kind is FailureKind.VALIDATION:
            return Strategy.ESCALATE
        if failure.kind is FailureKind.NO_STATE_CHANGE:
            if self.repeated(tool, args) == 1:
                return Strategy.REFRESH_AND_RETRY
            return Strategy.ALTERNATE_APPROACH if has_alternate else Strategy.FORCE_REPLAN
        if failure.kind in {FailureKind.TRANSIENT, FailureKind.HTTP_SERVER,
                            FailureKind.TIMEOUT}:
            attempts = self.repeated(tool, args)
            if attempts == 1:
                return Strategy.BACKOFF
            return Strategy.ALTERNATE_APPROACH if has_alternate else Strategy.FORCE_REPLAN
        if failure.kind is FailureKind.HTTP_CLIENT:
            return Strategy.ESCALATE

        s.consecutive_failures += 1
        if s.consecutive_failures >= self.config.max_consecutive_failures:
            return Strategy.ESCALATE
        if s.replans >= self.config.max_replans:
            return Strategy.GIVE_UP
        return Strategy.FORCE_REPLAN

    def record_strategy(self, strategy: Strategy) -> None:
        self.state.strategies_used.append(strategy.value)

    def record_success(self) -> None:
        self.state.consecutive_failures = 0

    def note_replan(self) -> None:
        self.state.replans += 1

    def backoff_delay(self, attempt: int) -> float:
        seq = self.config.backoff_seconds
        if not seq:
            return 0.0
        return seq[min(attempt, len(seq) - 1)]

    def note_recovery(self) -> None:
        self.state.recoveries += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.state.consecutive_failures,
            "replans": self.state.replans,
            "recoveries": self.state.recoveries,
            "strategies": list(self.state.strategies_used[-12:]),
            "top_actions": sorted(self.state.action_counts.items(), key=lambda kv: -kv[1])[:5],
        }


def _action_key(tool: str, args: dict[str, Any]) -> str:
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # pragma: no cover
        payload = str(args)
    return f"{tool}:{payload[:200]}"


def _extract_validation(text: str) -> str:
    """Pull the most informative validation line out of a page snapshot."""
    candidates = [line.strip() for line in text.splitlines() if VALIDATION_HINTS.search(line)]
    if not candidates:
        return ""
    return max(candidates, key=len)[:300]


async def sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)