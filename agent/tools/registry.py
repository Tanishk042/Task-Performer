"""The tool surface the model actually sees.

One registry, built per run, wrapping the generic primitives. Every tool returns
a *snapshot* after acting (rather than only a status line), because that is what
makes the loop work: the next decision is always made against the page as it
actually is, not against a memory of what it was.

Risk classes are set here, not inferred. `REVIEW_TOOL` decides which calls the
safety layer will interrogate, and which calls need a human to approve.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from agent.tools.base import (
    Risk,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    elapsed_ms,
)
from common.logging_setup import get_logger

log = get_logger(__name__)

MONEY_PATTERN = r"^-?\d{1,3}(,\d{3})*(\.\d{1,2})?$|^-?\d+(\.\d{1,2})?$"
ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class RefArg(BaseModel):
    ref: str = Field(description="Element handle from the current snapshot, e.g. 'e12'.")


class OpenArg(BaseModel):
    url: str = Field(description="Absolute URL to open.")


class TypeArg(BaseModel):
    ref: str = Field(description="Element handle of the input to type into.")
    text: str = Field(description="Text to enter.")
    submit: bool = Field(
        default=False,
        description="Press Enter afterwards to submit the surrounding form.",
    )


class SetValueArg(BaseModel):
    ref: str = Field(description="Element handle of the control to set.")
    value: str = Field(description="Option label for a dropdown, 'true'/'false' for a box.")


class WaitArg(BaseModel):
    target: str = Field(
        description="Text to wait for, or an element handle to wait for."
    )
    timeout_ms: int = Field(default=8000, description="Give up after this long.")


class ShotArg(BaseModel):
    label: str = Field(description="Short human-readable name for the screenshot.")


class HttpArg(BaseModel):
    method: str = Field(default="GET", description="HTTP method.")
    url: str = Field(description="Absolute URL.")
    json_body: dict[str, Any] | None = Field(
        default=None, description="JSON body, if any."
    )
    params: dict[str, str] | None = Field(default=None, description="Query parameters.")


class PdfArg(BaseModel):
    path: str = Field(description="Path of a downloaded PDF file.")


class RememberArg(BaseModel):
    key: str = Field(description="Short stable key, e.g. 'invoice:AC-2291'.")
    value: Any = Field(description="The value found.")
    source: str = Field(
        description="Where it came from — a page, a document, or 'user'."
    )


class PlanArg(BaseModel):
    steps: list[str] = Field(description="Revised ordered checklist.")
    criteria: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Checkable success criteria, each with id, description and a 'check' "
            "hint. These are re-checked independently after you finish."
        ),
    )
    notes: str = Field(default="", description="Anything worth remembering while working.")


class ApproveArg(BaseModel):
    action: str = Field(description="What you are about to do, in plain language.")
    fingerprint: str = Field(
        description=(
            "Stable id for exactly this action, e.g. 'ap-bill-submit:AC-2291'. "
            "Approval is granted per fingerprint, so it must not be reused for a "
            "different action."
        )
    )
    justification: str = Field(description="Why it needs approval.")


class AskArg(BaseModel):
    question: str = Field(description="The question to put to the user.")
    options: list[str] = Field(
        default_factory=list, description="Choices, if the question is a choice."
    )
    context: str = Field(default="", description="What you already know.")


class FinishArg(BaseModel):
    status: str = Field(
        description="One of: success, partial, failed, blocked. Be accurate, not optimistic."
    )
    summary: str = Field(description="What you did and what you found, in plain language.")
    evidence: list[str] = Field(
        default_factory=list, description="Specific facts or artefacts that back the summary."
    )
    values: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured record of what you believe you wrote or found: "
            "{'bills': [{'invoice_number','vendor','amount','currency','due_date','source'}], "
            "'extracted': [{'key','value','source'}]}"
        ),
    )


def _json_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


# --------------------------------------------------------------------------
# browser tools
# --------------------------------------------------------------------------

def build_browser_tools(registry: ToolRegistry) -> None:
    """Register the browser primitives. They read the session off the context."""

    def session(ctx: ToolContext) -> Any:
        return ctx.service("browser")

    async def snapshot_result(ctx: ToolContext, start: float, extra: str = "",
                              evidence: list[str] | None = None) -> ToolResult:
        """After every mutating action, hand back the page as it now is.

        The browser's `ActionOutcome` is merged into `data` so the loop can see
        whether the action actually changed anything — that is what separates
        "clicked submit and it saved" from "clicked submit and nothing happened".
        """
        browser = session(ctx)
        snap = await browser.snapshot()
        outcome = browser.action_log[-1].to_dict() if browser.action_log else {}
        header = extra or ""
        return ToolResult(
            ok=True,
            content=f"{header}\n{snap.text}".strip(),
            data={"url": snap.url, "title": snap.title, **outcome},
            duration_ms=elapsed_ms(start),
            evidence=evidence or [],
        )

    @registry.tool(
        "browser_open",
        description="Open a URL and return the page snapshot.",
        parameters=_json_schema(OpenArg),
    )
    async def browser_open(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = OpenArg(**args)
        outcome = await session(ctx).goto(parsed.url)
        if not outcome.ok:
            return ToolResult(
                ok=False,
                content=f"Could not open {parsed.url}: {outcome.detail}",
                data=outcome.to_dict(),
                error_code=outcome.error_code or "navigation_failed",
                duration_ms=elapsed_ms(start),
            )
        return await snapshot_result(ctx, start, f"Opened {parsed.url}.")

    @registry.tool(
        "browser_snapshot",
        description="Re-read the current page. Use this before acting on anything "
                    "not from the latest action.",
        parameters={"type": "object", "properties": {}},
    )
    async def browser_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        snap = await session(ctx).snapshot()
        return ToolResult(
            ok=True, content=snap.text, data={"url": snap.url, "title": snap.title},
            duration_ms=elapsed_ms(time.perf_counter()),
        )

    @registry.tool(
        "browser_click",
        description="Click the element with the given handle.",
        parameters=_json_schema(RefArg),
    )
    async def browser_click(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = RefArg(**args)
        outcome = await session(ctx).click(parsed.ref)
        if not outcome.ok:
            return ToolResult(
                ok=False, content=outcome.detail or "click failed",
                data=outcome.to_dict(), error_code=outcome.error_code,
                duration_ms=elapsed_ms(start),
            )
        return await snapshot_result(ctx, start, f"Clicked {parsed.ref}.")

    @registry.tool(
        "browser_type",
        description="Type text into the element with the given handle.",
        parameters=_json_schema(TypeArg),
    )
    async def browser_type(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = TypeArg(**args)
        outcome = await session(ctx).type_text(
            parsed.ref, parsed.text, submit=parsed.submit
        )
        if not outcome.ok:
            return ToolResult(
                ok=False, content=outcome.detail or "typing failed",
                data=outcome.to_dict(), error_code=outcome.error_code,
                duration_ms=elapsed_ms(start),
            )
        return await snapshot_result(ctx, start, f"Typed into {parsed.ref}.")

    @registry.tool(
        "browser_set_value",
        description="Set a dropdown or checkbox by value/label.",
        parameters=_json_schema(SetValueArg),
    )
    async def browser_set_value(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = SetValueArg(**args)
        outcome = await session(ctx).set_value(parsed.ref, parsed.value)
        if not outcome.ok:
            return ToolResult(
                ok=False, content=outcome.detail or "could not set the value",
                data=outcome.to_dict(), error_code=outcome.error_code,
                duration_ms=elapsed_ms(start),
            )
        return await snapshot_result(ctx, start, f"Set {parsed.ref}.")

    @registry.tool(
        "browser_wait_for",
        description="Wait for text to appear or for an element to show up. Use this "
                    "when a page renders late instead of re-clicking.",
        parameters=_json_schema(WaitArg),
    )
    async def browser_wait_for(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = WaitArg(**args)
        outcome = await session(ctx).wait_for(
            parsed.target, timeout_ms=parsed.timeout_ms
        )
        if not outcome.ok:
            return ToolResult(
                ok=False, content=outcome.detail or "wait timed out",
                data=outcome.to_dict(), error_code=outcome.error_code,
                duration_ms=elapsed_ms(start),
            )
        return await snapshot_result(ctx, start, f"Waited for {parsed.target!r}.")

    @registry.tool(
        "browser_screenshot",
        description="Capture the current page as evidence.",
        parameters=_json_schema(ShotArg),
        risk=Risk.READ,
    )
    async def browser_screenshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = ShotArg(**args)
        path = await session(ctx).screenshot(parsed.label)
        return ToolResult(
            ok=bool(path),
            content=f"Saved screenshot: {path}" if path else "Screenshot failed.",
            data={"path": path},
            duration_ms=elapsed_ms(start),
            evidence=[path] if path else [],
        )

    @registry.tool(
        "browser_download",
        description="Download the file behind a link (for example an invoice PDF).",
        parameters=_json_schema(RefArg),
    )
    async def browser_download(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = RefArg(**args)
        info = await session(ctx).download(parsed.ref)
        if not info.get("ok"):
            return ToolResult(
                ok=False, content=info.get("detail", "download failed"),
                data=info, error_code=info.get("error_code", "download_failed"),
                duration_ms=elapsed_ms(start),
            )
        return ToolResult(
            ok=True,
            content=f"Downloaded {info['filename']} ({info['bytes']} bytes) to {info['path']}",
            data=info, duration_ms=elapsed_ms(start), evidence=[info["path"]],
        )


# --------------------------------------------------------------------------
# pdf + http
# --------------------------------------------------------------------------

def build_document_tools(registry: ToolRegistry) -> None:
    @registry.tool(
        "extract_pdf_text",
        description="Extract the text of a downloaded PDF so you can check the figures "
                    "on the document itself rather than only the web page.",
        parameters=_json_schema(PdfArg),
    )
    async def extract_pdf_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = PdfArg(**args)
        path = Path(parsed.path)
        if not path.exists():
            return ToolResult(
                ok=False, content=f"No such file: {path}",
                error_code="file_not_found", duration_ms=elapsed_ms(start),
            )
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            return ToolResult(
                ok=False, content=f"Could not read the PDF: {exc}",
                error_code="pdf_read_failed", duration_ms=elapsed_ms(start),
            )
        return ToolResult(
            ok=True, content=text.strip() or "(the PDF contains no extractable text)",
            data={"path": str(path), "pages": len(reader.pages), "chars": len(text)},
            duration_ms=elapsed_ms(start), evidence=[str(path)],
        )

    @registry.tool(
        "http_request",
        description="Make a direct HTTP request. Useful for reading a read-only JSON "
                    "API or re-checking a record after writing it.",
        parameters=_json_schema(HttpArg),
    )
    async def http_request(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = HttpArg(**args)
        timeout = float(ctx.settings.browser_timeout_ms / 1000) + 5
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(
                    parsed.method.upper(), parsed.url, json=parsed.json_body,
                    params=parsed.params,
                )
        except Exception as exc:
            return ToolResult(
                ok=False, content=f"Request failed: {exc}", error_code="request_failed",
                duration_ms=elapsed_ms(start),
            )
        body = response.text
        try:
            rendered = json.dumps(response.json(), indent=2, sort_keys=True)
        except Exception:
            rendered = body[:4000]
        return ToolResult(
            ok=response.is_success,
            content=f"HTTP {response.status_code}\n{rendered[:4000]}",
            data={"status": response.status_code, "url": str(response.url),
                  "bytes": len(body)},
            error_code=None if response.is_success else f"http_{response.status_code}",
            duration_ms=elapsed_ms(start),
        )


# --------------------------------------------------------------------------
# agent-internal tools
# --------------------------------------------------------------------------

def build_agent_tools(registry: ToolRegistry) -> None:
    """Plan/memory/finish/interaction. These are the loop's own verbs."""

    @registry.tool(
        "update_plan",
        description="Replace the plan with a revised checklist and the success criteria "
                    "you will be held to. Call this once at the start, and again if "
                    "what you are doing changes.",
        parameters=_json_schema(PlanArg),
    )
    async def update_plan(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = PlanArg(**args)
        planner = ctx.service("planner")
        planner.revise(parsed.model_dump())
        return ToolResult(
            ok=True, content=planner.plan.render(),
            data={"revision": planner.plan.revision},
            duration_ms=elapsed_ms(start),
        )

    @registry.tool(
        "remember",
        description="Record a value together with where it came from, so it can be "
                    "traced back to a specific page or document.",
        parameters=_json_schema(RememberArg),
    )
    async def remember(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = RememberArg(**args)
        memory = ctx.service("memory")
        entry = memory.remember(parsed.key, parsed.value, source=parsed.source,
                                step=getattr(ctx, "step", 0) or 0)
        return ToolResult(
            ok=True,
            content=f"Noted {parsed.key} = {json.dumps(parsed.value, default=str)[:200]} "
                    f"(from {parsed.source}).",
            data={"key": parsed.key},
            duration_ms=elapsed_ms(start),
        )

    @registry.tool(
        "request_approval",
        description="Ask a human to approve a risky or irreversible action. You will "
                    "not be able to perform it until they say yes.",
        parameters=_json_schema(ApproveArg),
        risk=Risk.IRREVERSIBLE_WRITE,
        requires_interaction=True,
    )
    async def request_approval(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = ApproveArg(**args)
        approvals = ctx.service("approvals")
        decision = await approvals.request(ctx, parsed.action, parsed.fingerprint,
                                            parsed.justification)
        if decision.granted:
            return ToolResult(
                ok=True, content=f"Approved by {decision.by or 'user'}: {decision.reason}",
                data={"granted": True, "fingerprint": parsed.fingerprint},
                duration_ms=elapsed_ms(start),
            )
        return ToolResult(
            ok=False,
            content=f"Not approved: {decision.reason or 'the request was declined'}",
            data={"granted": False, "fingerprint": parsed.fingerprint},
            error_code="approval_denied", duration_ms=elapsed_ms(start),
        )

    @registry.tool(
        "ask_user",
        description="Ask the user a question when the request is ambiguous or two "
                    "real possibilities match. Prefer this to guessing.",
        parameters=_json_schema(AskArg),
        risk=Risk.READ,
        requires_interaction=True,
    )
    async def ask_user(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = AskArg(**args)
        if not ctx.interactive:
            return ToolResult(
                ok=False,
                content=(
                    "This run is not interactive, so there is nobody to ask. Treat the "
                    "ambiguity as a blocker and finish with status 'blocked' rather "
                    "than choosing one of the possibilities."
                ),
                data={"question": parsed.question, "options": parsed.options},
                error_code="not_interactive", duration_ms=elapsed_ms(start),
            )
        interaction = ctx.service("interaction")
        answer = await interaction.ask(ctx, parsed.question, parsed.options, parsed.context)
        return ToolResult(
            ok=True, content=f"User answered: {answer}",
            data={"question": parsed.question, "answer": answer},
            duration_ms=elapsed_ms(start),
        )

    @registry.tool(
        "finish",
        description="End the run and report what happened. This does not make the task "
                    "successful — the result is verified independently afterwards.",
        parameters=_json_schema(FinishArg),
        risk=Risk.READ,
        terminal=True,
    )
    async def finish(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = FinishArg(**args)
        allowed = {"success", "partial", "failed", "blocked"}
        status = parsed.status if parsed.status in allowed else "failed"
        payload = {
            "status": status,
            "summary": parsed.summary,
            "evidence": parsed.evidence,
            "values": parsed.values,
        }
        await ctx.service("finish").set(payload)
        return ToolResult(
            ok=True,
            content=f"Run finished with status '{status}'. It will now be verified "
                    f"independently.",
            data=payload, duration_ms=elapsed_ms(start),
        )


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    build_browser_tools(registry)
    build_document_tools(registry)
    build_agent_tools(registry)
    return registry


#: Words in a description that hint at a state change, used as a cheap extra
#: signal by the safety layer.
WRITE_HINTS = ("save", "submit", "create", "delete", "post", "commit", "insert")