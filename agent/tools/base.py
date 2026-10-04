"""Tool registry: JSON schemas in, typed results out.

A tool is a `ToolSpec` (metadata + risk class + JSON schema) plus a callable.
The registry is what the LLM sees and what the safety layer inspects before
anything executes — there is no path from the model to a side effect that skips
`registry.execute`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable

from pydantic import BaseModel, Field


class Risk(str, Enum):
    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE_WRITE = "irreversible_write"


RISK_ORDER = {Risk.READ: 0, Risk.REVERSIBLE_WRITE: 1, Risk.IRREVERSIBLE_WRITE: 2}


@dataclass
class ToolResult:
    """What the LLM sees after a tool call."""

    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    duration_ms: int = 0
    evidence: list[str] = field(default_factory=list)
    #: Set by interactive tools so the loop knows to suspend.
    suspend: str | None = None
    #: Filled by the executor: whether safety approved this call.
    approval_required: bool = False

    def to_observation(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "result": self.content}
        if self.error_code:
            payload["error"] = self.error_code
        if self.data:
            payload["data"] = self.data
        if self.evidence:
            payload["evidence"] = self.evidence
        if self.approval_required:
            payload["note"] = "approval was required for this call"
        return payload


ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable[ToolResult] | ToolResult]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk: Risk = Risk.READ
    #: Words in the tool description that hint at writes, used by the safety
    #: layer when it needs a cheap textual signal.
    tags: tuple[str, ...] = ()
    #: When set, this tool terminates the run (handled by the loop, not here).
    terminal: bool = False
    #: Refuse to run while the run is not interactive (headless eval mode).
    requires_interaction: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass
class ToolContext:
    """Everything a tool implementation is allowed to reach."""

    run_id: str
    workspace_dir: Any
    run_dir: Any
    settings: Any
    #: Shared mutable services (browser, memory, safety, approvals...).
    services: dict[str, Any] = field(default_factory=dict)
    trace: Any = None
    #: Headless mode: ask_user / request_approval are answered by scripts.
    interactive: bool = True
    #: Which step of the loop is running, so tools can stamp provenance.
    step: int = 0
    emit: Callable[[str, dict[str, Any]], None] | None = None

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:  # pragma: no cover - wiring bug
            raise KeyError(f"service {name!r} not registered on this run") from exc


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        name: str,
        *,
        description: str,
        parameters: dict[str, Any],
        risk: Risk = Risk.READ,
        tags: Iterable[str] = (),
        terminal: bool = False,
        requires_interaction: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorate(fn: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    parameters=parameters,
                    handler=fn,
                    risk=risk,
                    tags=tuple(tags),
                    terminal=terminal,
                    requires_interaction=requires_interaction,
                )
            )
            return fn

        return decorate

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, include: Iterable[str] | None = None) -> list[dict[str, Any]]:
        selected = list(include) if include is not None else self.names()
        return [self._tools[n].schema() for n in selected if n in self._tools]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


class ToolError(Exception):
    """Raised by tools for expected, recoverable problems."""

    def __init__(self, message: str, code: str = "tool_error", data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


class Schema(BaseModel):
    """Convenience base for validating tool arguments."""

    model_config = {"extra": "forbid"}


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)