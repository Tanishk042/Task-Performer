"""LLM interface + two implementations.

`AnthropicClient` is the real thing: Claude via the Anthropic SDK with tool use,
retry/backoff on transient API errors, and token accounting.

`ScriptedClient` (in `agent/llm_scripted.py`) is a deterministic policy engine
that speaks the *same* message protocol and emits the *same* tool calls, so the
loop, safety gate, recovery ladder, verifier and trace are all exercised for real
without an API key.

Both are driven purely by what the loop feeds them. Neither knows anything about
invoices, vendors or ports — the environment brief arrives as part of the goal
context, exactly as it would from a human briefing an employee.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from common.logging_setup import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"

    @property
    def used_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class Message:
    role: str
    content: Any = None
    tool_calls: list[ToolCall] | None = None
    tool_use_id: str | None = None
    is_error: bool = False

    def to_anthropic(self) -> dict[str, Any]:
        if self.role == "assistant" and self.tool_calls:
            blocks: list[dict[str, Any]] = []
            if self.content:
                blocks.append({"type": "text", "text": self.content})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                for c in self.tool_calls
            ]
            return {"role": "assistant", "content": blocks}
        if self.role == "user" and self.tool_use_id:
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": self.tool_use_id,
                    "is_error": self.is_error,
                    "content": str(self.content),
                }],
            }
        return {"role": self.role, "content": str(self.content)}


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AssistantTurn:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class AnthropicClient:
    """Claude via the Anthropic SDK, with bounded exponential backoff."""

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_retries: int = 5,
        backoff: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0),
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Either set it, or run with "
                "LLM_PROVIDER=scripted for the offline policy engine."
            )
        from anthropic import AsyncAnthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self.model = model
        self._max_retries = max_retries
        self._backoff = backoff

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AssistantTurn:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [m.to_anthropic() for m in messages],
            "tools": tools,
        }
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = await self._client.messages.create(**payload)
                return self._parse(response)
            except Exception as exc:  # noqa: BLE001 - SDK raises a broad family
                last_error = exc
                retryable = _is_retryable(exc)
                if not retryable or attempt == self._max_retries - 1:
                    break
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                delay *= 0.7 + random.random() * 0.6  # jitter
                log.warning(
                    "anthropic API error (%s); retry %s/%s in %.1fs",
                    _short(exc), attempt + 1, self._max_retries, delay,
                )
                await asyncio.sleep(delay)

        raise LLMError(f"anthropic API call failed after retries: {_short(last_error)}")

    def _parse(self, response: Any) -> AssistantTurn:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, input=dict(block.input or {}))
                )
        usage = getattr(response, "usage", None)
        return AssistantTurn(
            content="\n".join(p for p in text_parts if p).strip(),
            tool_calls=calls,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            stop_reason=str(getattr(response, "stop_reason", "") or "end_turn"),
        )


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and status in RETRYABLE_STATUS:
        return True
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "InternalServerError",
                "RateLimitError", "ServiceUnavailableError", "OverloadedError"}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("overloaded", "rate limit", "timeout", "connection", "503", "529")
    )


def _short(exc: Exception | None, limit: int = 220) -> str:
    if exc is None:
        return "unknown error"
    return f"{type(exc).__name__}: {exc}"[:limit]


# --------------------------------------------------------------------------
# test double
# --------------------------------------------------------------------------

class FakeLLM:
    """Canned turns for unit tests. Pops from a queue, then repeats the last.

    Deliberately dumb: unit tests assert on the *loop's* behaviour, not on any
    reasoning quality.
    """

    provider = "fake"

    def __init__(
        self,
        turns: list[AssistantTurn | Callable[[list[Message]], AssistantTurn]] | None = None,
        *,
        model: str = "fake-1",
        fail_times: int = 0,
    ) -> None:
        self.model = model
        self._turns = list(turns or [])
        self._fail_times = fail_times
        self._calls = 0
        self.seen_messages: list[list[Message]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AssistantTurn:
        self._calls += 1
        self.seen_messages.append(list(messages))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise LLMError("fake transient failure", retryable=True)
        if not self._turns:
            return AssistantTurn(content="nothing left to do", stop_reason="end_turn")
        item = self._turns.pop(0) if len(self._turns) > 1 else self._turns[0]
        return item(messages) if callable(item) else item

    @property
    def calls(self) -> int:
        return self._calls


def build_llm_client(settings: Any) -> LLMClient:
    """Factory driven by `LLM_PROVIDER`."""
    provider = (settings.llm_provider or "scripted").lower()
    if provider == "anthropic":
        client = AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
        )
        log.info("LLM provider: anthropic (%s)", settings.anthropic_model)
        return client
    if provider == "scripted":
        from agent.llm_scripted import ScriptedClient

        log.info("LLM provider: scripted (offline policy engine)")
        return ScriptedClient(model=getattr(settings, "anthropic_model", "scripted"))
    raise LLMError(f"unknown LLM_PROVIDER {provider!r}; use 'anthropic' or 'scripted'")