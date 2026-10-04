"""Fault injection shared by both simulated apps.

Off by default. Enabled either by `FAULT_INJECTION=true` at boot or by
`POST /admin/faults` at runtime (that is what the UI toggle and the eval harness
use). Every fault is *armed* explicitly so "faults enabled" never accidentally
means "all faults firing".

Each app runs in its own process, so the controller is a per-process singleton
that the orchestrator pokes over HTTP.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from common.logging_setup import get_logger

log = get_logger(__name__)

ALL_FAULTS: tuple[str, ...] = (
    "session_expiry",
    "transient_500_on_submit",
    "slow_page",
    "flaky_render",
    "ui_rename",
    "silent_save_failure",
)

FAULT_DESCRIPTIONS: dict[str, str] = {
    "session_expiry": "Invalidate the session mid-run and bounce the user to the login page.",
    "transient_500_on_submit": "Return HTTP 503 on the first bill-submit attempt only.",
    "slow_page": "Add a multi-second delay to list/detail page loads.",
    "flaky_render": "Populate invoice rows via JS after a delay, so snapshots can race the render.",
    "ui_rename": "Rename AP form controls (ids + labels) to defeat memorised selectors.",
    "silent_save_failure": "Show a success toast while silently discarding the write.",
}


@dataclass
class FaultController:
    """Thread-safe armed-fault registry for one app process."""

    enabled: bool = False
    armed: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    fired_log: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    DEFAULT_PARAMS: dict[str, Any] = field(
        default_factory=lambda: {
            # Fire the expiry on the Nth authenticated page view.
            "session_expiry_after_requests": 5,
            "slow_page_seconds": 3.0,
            "flaky_render_ms": 1400,
        },
        repr=False,
    )

    def __post_init__(self) -> None:
        self.params.update(self.DEFAULT_PARAMS)

    # -- configuration ----------------------------------------------------
    def configure(
        self,
        enabled: bool | None = None,
        faults: list[str] | None = None,
        params: dict[str, Any] | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if reset:
                self.armed.clear()
                self.counters.clear()
                self.fired_log.clear()
                self.params.update(self.DEFAULT_PARAMS)
            if enabled is not None:
                self.enabled = enabled
                if not enabled:
                    self.armed.clear()
            if faults is not None:
                unknown = [f for f in faults if f not in ALL_FAULTS]
                if unknown:
                    raise ValueError(f"unknown faults: {unknown}")
                self.armed = set(faults)
                # One-shot faults need a clean slate each time they are armed.
                for name in self.armed:
                    self.counters.setdefault(name, 0)
            if params:
                self.params.update(params)
            return self.state()

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "armed": sorted(self.armed),
            "available": list(ALL_FAULTS),
            "descriptions": FAULT_DESCRIPTIONS,
            "params": dict(self.params),
            "fired": list(self.fired_log[-50:]),
        }

    # -- consumption ------------------------------------------------------
    def is_armed(self, name: str) -> bool:
        return self.enabled and name in self.armed

    def note_fired(self, name: str, detail: str = "") -> None:
        with self._lock:
            self.fired_log.append({"fault": name, "detail": detail})
            if len(self.fired_log) > 200:
                del self.fired_log[:-200]
        log.warning("fault fired: %s %s", name, detail)

    def consume_once(self, name: str) -> bool:
        """True exactly once per arming of a one-shot fault."""
        if not self.is_armed(name):
            return False
        with self._lock:
            used = self.counters.get(name, 0)
            if used >= 1:
                return False
            self.counters[name] = used + 1
        self.note_fired(name, "one-shot consumed")
        return True

    def bump(self, name: str) -> int:
        """Increment and return a counter (e.g. authenticated request count)."""
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + 1
            return self.counters[name]

    def should_expire_session(self, app: str) -> bool:
        if not self.is_armed("session_expiry"):
            return False
        n = int(self.params.get("session_expiry_after_requests", 5))
        count = self.bump(f"auth_requests:{app}")
        if count == n:
            self.note_fired("session_expiry", f"{app} auth request #{count}")
            return True
        return False

    def param(self, name: str, default: Any) -> Any:
        return self.params.get(name, default)


class _Proxy:
    """Lazily-initialised module-level controller."""

    def __init__(self) -> None:
        self._impl: FaultController | None = None

    def get(self) -> FaultController:
        if self._impl is None:
            from common.config import get_settings

            self._impl = FaultController(enabled=get_settings().fault_injection)
        return self._impl

    def configure(self, **kwargs: Any) -> dict[str, Any]:
        return self.get().configure(**kwargs)

    def state(self) -> dict[str, Any]:
        return self.get().state()

    def is_armed(self, name: str) -> bool:
        return self.get().is_armed(name)

    def consume_once(self, name: str) -> bool:
        return self.get().consume_once(name)

    def note_fired(self, name: str, detail: str = "") -> None:
        self.get().note_fired(name, detail)

    def should_expire_session(self, app: str) -> bool:
        return self.get().should_expire_session(app)

    def param(self, name: str, default: Any) -> Any:
        return self.get().param(name, default)

    def slow_page_pause(self) -> float:
        if self.is_armed("slow_page"):
            return float(self.param("slow_page_seconds", 3.0))
        return 0.0

    def wrap(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run `fn`, swallowing an injected transient error once."""
        if self.consume_once(name):
            raise TransientInjectedFault(name)
        return fn()


class TransientInjectedFault(RuntimeError):
    def __init__(self, fault: str) -> None:
        super().__init__(f"injected transient fault: {fault}")
        self.fault = fault


FAULTS = _Proxy()