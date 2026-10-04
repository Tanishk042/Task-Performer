"""Browser session over Playwright/Chromium.

Deliberately thin: navigation + ref-addressed actions + snapshots + screenshots.
It knows nothing about any particular website.

Element addressing
------------------
`browser_snapshot()` tags every actionable element with `data-aiw-ref="e12"`
inside the page. Actions take that ref and resolve it at action time, so a ref
is a *handle*, not a positional selector — which is what makes the agent immune
to DOM reordering and to the `ui_rename` fault.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.tools.snapshot import (
    DEFAULT_CHAR_BUDGET,
    REF_ATTR,
    Snapshot,
    parse_snapshot,
    snapshot_config,
)
from common.logging_setup import get_logger

log = get_logger(__name__)

DOWNLOAD_DIRNAME = "downloads"


def _fingerprint(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass
class ActionOutcome:
    """Result of one browser action, including a state fingerprint.

    The fingerprint is what `recovery` uses to detect "the click did nothing".
    """

    ok: bool
    kind: str
    detail: str = ""
    url_before: str = ""
    url_after: str = ""
    fingerprint_before: str = ""
    fingerprint_after: str = ""
    changed: bool = False
    duration_ms: int = 0
    evidence_path: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "detail": self.detail,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "changed": self.changed,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }


class BrowserSession:
    """Owns one Playwright browser context (one 'logged in employee')."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 15000,
        run_dir: Path,
        viewport: tuple[int, int] = (1440, 900),
        user_agent: str | None = None,
        label: str = "agent",
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.run_dir = Path(run_dir)
        self.viewport = viewport
        self.user_agent = user_agent
        self.label = label
        self.download_dir = self.run_dir / DOWNLOAD_DIRNAME

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._last_snapshot: Snapshot | None = None
        self._screenshot_seq = 0
        self._action_log: list[ActionOutcome] = []
        self._console_errors: list[str] = []

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            accept_downloads=True,
            user_agent=self.user_agent,
            ignore_https_errors=True,
        )
        self._context.set_default_timeout(self.timeout_ms)
        self._context.set_default_navigation_timeout(self.timeout_ms)
        self._page = await self._context.new_page()
        self._page.on(
            "console",
            lambda msg: self._console_errors.append(f"{msg.type}: {msg.text}")
            if msg.type == "error" else None,
        )
        log.info("browser session started (label=%s headless=%s)", self.label, self.headless)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.debug("browser close error: %s", exc)
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # pragma: no cover
            log.debug("playwright stop error: %s", exc)
        log.info("browser session closed (label=%s)", self.label)

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("browser session not started")
        return self._page

    @property
    def context(self) -> Any:
        if self._context is None:
            raise RuntimeError("browser session not started")
        return self._context

    @property
    def last_snapshot(self) -> Snapshot | None:
        return self._last_snapshot

    @property
    def action_log(self) -> list[ActionOutcome]:
        return list(self._action_log)

    @property
    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    def clear_cookies(self) -> None:
        """Used by the verifier so it starts from a clean, independent state."""
        try:
            self._context.clear_cookies() if False else None
        except Exception:
            pass

    # -- navigation -------------------------------------------------------
    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> ActionOutcome:
        start = time.perf_counter()
        before_url = self.current_url
        before_fp = await self._fingerprint()
        try:
            await self.page.goto(url, wait_until=wait_until)
        except Exception as exc:
            outcome = ActionOutcome(
                ok=False, kind="goto", detail=str(exc)[:300], url_before=before_url,
                url_after=self.current_url, error_code="navigation_failed",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome
        await self._settle()
        outcome = ActionOutcome(
            ok=True, kind="goto", detail=f"opened {url}", url_before=before_url,
            url_after=self.current_url, fingerprint_before=before_fp,
            fingerprint_after=await self._fingerprint(),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        outcome.changed = outcome.url_after != before_url
        self._action_log.append(outcome)
        return outcome

    async def _settle(self, ms: int = 250) -> None:
        """Let deferred JS renders land before snapshotting.

        Without this, the `flaky_render` fault would be indistinguishable from a
        broken page. With it, a genuinely slow render still shows up as an
        unchanged fingerprint, which is what recovery keys off.
        """
        try:
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        await self.page.wait_for_timeout(ms)

    # -- snapshots --------------------------------------------------------
    async def snapshot(self, char_budget: int = DEFAULT_CHAR_BUDGET) -> Snapshot:
        raw = await self.page.evaluate(SNAPSHOT_JS_SOURCE, snapshot_config())
        snap = parse_snapshot(raw, char_budget=char_budget)
        self._last_snapshot = snap
        return snap

    async def _fingerprint(self) -> str:
        """Cheap structural signature used for no-op detection."""
        try:
            sig = await self.page.evaluate(
                """() => {
                    const main = document.querySelector('main') || document.body;
                    const text = (main.innerText || '').replace(/\\s+/g, ' ').trim();
                    return location.pathname + '|' + location.search + '|' +
                           text.slice(0, 4000) + '|' + document.querySelectorAll('input,select,textarea,button,a').length;
                }"""
            )
        except Exception:
            return ""
        return _fingerprint(sig)

    # -- element resolution ----------------------------------------------
    def _selector(self, ref: str) -> str:
        return f'[{REF_ATTR}="{ref}"]'

    async def _require_ref(self, ref: str) -> dict[str, Any]:
        snap = self._last_snapshot
        if snap is None:
            snap = await self.snapshot()
        node = snap.find_by_ref(ref)
        if node is None:
            available = sorted(snap.refs)[:40]
            return {
                "ok": False,
                "error_code": "unknown_ref",
                "detail": (
                    f"ref {ref!r} is not on the current page. Take a fresh "
                    f"browser_snapshot() before acting. Refs currently available: {available}"
                ),
            }
        if node.disabled:
            return {
                "ok": False,
                "error_code": "element_disabled",
                "detail": f"ref {ref} ({node.label or node.tag}) is disabled",
            }
        return {"ok": True, "node": node}

    async def _resolve_node(self, ref: str) -> Any:
        return await self.page.query_selector(self._selector(ref))

    # -- actions ----------------------------------------------------------
    async def click(self, ref: str, *, expect_navigation: bool | None = None) -> ActionOutcome:
        start = time.perf_counter()
        check = await self._require_ref(ref)
        if not check["ok"]:
            outcome = ActionOutcome(
                ok=False, kind="click", detail=check["detail"], url_before=self.current_url,
                url_after=self.current_url, error_code=check["error_code"],
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome

        node = check["node"]
        handle = await self._resolve_node(ref)
        if handle is None:
            outcome = ActionOutcome(
                ok=False, kind="click", detail=f"element {ref} vanished from the DOM",
                error_code="stale_ref", duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome

        before_url = self.current_url
        before_fp = await self._fingerprint()
        tag = (await handle.evaluate("el => el.tagName.toLowerCase()")) or ""
        try:
            if tag == "select":
                await handle.select_option(index=0)
            elif tag in {"input", "textarea"}:
                checkboxes = {"checkbox", "radio"}
                if node.type in checkboxes:
                    await handle.set_checked(not bool(node.checked))
                else:
                    await handle.click()
            else:
                async with self.page.expect_navigation(
                    timeout=2500, wait_until="domcontentloaded"
                ) if expect_navigation is not False else _nullcontext():
                    await handle.click()
                    if expect_navigation is None:
                        pass
        except Exception as exc:
            # A navigation timeout after a successful click is common; fall back.
            if "expect_navigation" in str(exc) or "Timeout" in str(exc):
                await self._settle()
            else:
                outcome = ActionOutcome(
                    ok=False, kind="click", detail=str(exc)[:300], url_before=before_url,
                    url_after=self.current_url, error_code="click_failed",
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
                self._action_log.append(outcome)
                return outcome
        await self._settle()
        after_fp = await self._fingerprint()
        outcome = ActionOutcome(
            ok=True, kind="click", detail=f"clicked {ref} ({node.label or node.tag})",
            url_before=before_url, url_after=self.current_url,
            fingerprint_before=before_fp, fingerprint_after=after_fp,
            changed=(after_fp != before_fp),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        self._action_log.append(outcome)
        return outcome

    async def type_text(self, ref: str, text: str, *, clear: bool = True,
                        submit: bool = False) -> ActionOutcome:
        start = time.perf_counter()
        check = await self._require_ref(ref)
        if not check["ok"]:
            outcome = ActionOutcome(
                ok=False, kind="type", detail=check["detail"], error_code=check["error_code"],
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome
        node = check["node"]
        handle = await self._resolve_node(ref)
        if handle is None:
            outcome = ActionOutcome(
                ok=False, kind="type", detail=f"element {ref} vanished",
                error_code="stale_ref", duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome

        before_url = self.current_url
        before_fp = await self._fingerprint()
        try:
            await handle.scroll_into_view_if_needed(timeout=3000)
            if clear:
                await handle.fill("")
            await handle.type(str(text), delay=8)
            if submit:
                await handle.press("Enter")
        except Exception as exc:
            outcome = ActionOutcome(
                ok=False, kind="type", detail=str(exc)[:300], url_before=before_url,
                url_after=self.current_url, error_code="type_failed",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome
        await self._settle()
        after_fp = await self._fingerprint()
        outcome = ActionOutcome(
            ok=True, kind="type",
            detail=f"typed into {ref} ({node.label or node.tag})",
            url_before=before_url, url_after=self.current_url,
            fingerprint_before=before_fp, fingerprint_after=after_fp,
            changed=(after_fp != before_fp),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        self._action_log.append(outcome)
        return outcome

    async def set_value(self, ref: str, value: str) -> ActionOutcome:
        """Set a form control's value directly (select option, checkbox, input)."""
        start = time.perf_counter()
        check = await self._require_ref(ref)
        if not check["ok"]:
            outcome = ActionOutcome(
                ok=False, kind="set_value", detail=check["detail"],
                error_code=check["error_code"],
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome
        node = check["node"]
        handle = await self._resolve_node(ref)
        if handle is None:
            outcome = ActionOutcome(
                ok=False, kind="set_value", detail=f"element {ref} vanished",
                error_code="stale_ref", duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome

        tag = await handle.evaluate("el => el.tagName.toLowerCase()")
        before_fp = await self._fingerprint()
        before_url = self.current_url
        try:
            if tag == "select":
                matched = await self._select_matching_option(handle, str(value))
                if not matched:
                    outcome = ActionOutcome(
                        ok=False, kind="set_value",
                        detail=(
                            f"no option matching {value!r} in {node.label or 'select'}; "
                            f"available: {node.options}"
                        ),
                        error_code="no_matching_option",
                        duration_ms=int((time.perf_counter() - start) * 1000),
                    )
                    self._action_log.append(outcome)
                    return outcome
            elif node.type in {"checkbox", "radio"}:
                await handle.set_checked(str(value).lower() in {"1", "true", "yes", "on"})
            else:
                await handle.fill(str(value))
        except Exception as exc:
            outcome = ActionOutcome(
                ok=False, kind="set_value", detail=str(exc)[:300],
                error_code="set_value_failed",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome

        await self._settle()
        after_fp = await self._fingerprint()
        outcome = ActionOutcome(
            ok=True, kind="set_value",
            detail=f"set {node.label or node.tag} = {value!r}",
            url_before=before_url, url_after=self.current_url,
            fingerprint_before=before_fp, fingerprint_after=after_fp,
            changed=(after_fp != before_fp),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        self._action_log.append(outcome)
        return outcome

    async def _select_matching_option(self, handle: Any, value: str) -> bool:
        """Match an option by value, then by exact label, then by substring."""
        for strategy in ("value", "label", "contains"):
            try:
                if strategy == "value":
                    return await handle.select_option(value=value)
                if strategy == "label":
                    return await handle.select_option(label=value)
                return await handle.select_option(label=re_compile_contains(value))
            except Exception:
                continue
        return False

    async def press(self, key: str) -> ActionOutcome:
        start = time.perf_counter()
        before_url = self.current_url
        before_fp = await self._fingerprint()
        try:
            await self.page.keyboard.press(key)
        except Exception as exc:
            outcome = ActionOutcome(
                ok=False, kind="press", detail=str(exc)[:200], error_code="press_failed",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            self._action_log.append(outcome)
            return outcome
        await self._settle()
        after_fp = await self._fingerprint()
        outcome = ActionOutcome(
            ok=True, kind="press", detail=f"pressed {key}", url_before=before_url,
            url_after=self.current_url, fingerprint_before=before_fp,
            fingerprint_after=after_fp, changed=(after_fp != before_fp),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        self._action_log.append(outcome)
        return outcome

    async def wait_for(self, target: str, *, timeout_ms: int = 8000,
                       state: str = "visible") -> ActionOutcome:
        """Wait for visible text, a CSS selector, or a ref to appear.

        Used to beat slow renders. Text alone is not always enough: a table can
        render its rows with no new words in them, so the caller needs to be
        able to wait for structure instead.
        """
        start = time.perf_counter()
        before_fp = await self._fingerprint()
        detail = f"waiting for {target!r}"
        error_code: str | None = None
        ok = True
        try:
            if re_fullmatch_ref(target):
                await self.page.wait_for_selector(self._selector(target), timeout=timeout_ms)
                detail = f"ref {target} appeared"
            elif _looks_like_selector(target):
                await self.page.wait_for_selector(target, state=state,
                                                  timeout=timeout_ms)
                detail = f"selector {target!r} appeared"
            else:
                await self.page.wait_for_function(
                    """needle => {
                        const hay = (document.body.innerText || '').toLowerCase();
                        return hay.includes(needle.toLowerCase());
                    }""",
                    arg=target,
                    timeout=timeout_ms,
                )
                detail = f"text {target!r} appeared"
        except Exception as exc:
            ok = False
            error_code = "wait_timeout"
            detail = f"timed out after {timeout_ms}ms waiting for {target!r}"
        await self._settle()
        after_fp = await self._fingerprint()
        outcome = ActionOutcome(
            ok=ok, kind="wait_for", detail=detail, url_before=self.current_url,
            url_after=self.current_url, fingerprint_before=before_fp,
            fingerprint_after=after_fp, changed=(after_fp != before_fp),
            error_code=error_code, duration_ms=int((time.perf_counter() - start) * 1000),
        )
        self._action_log.append(outcome)
        return outcome

    # -- evidence ---------------------------------------------------------
    async def screenshot(self, label: str, *, full_page: bool = False) -> str:
        self._screenshot_seq += 1
        safe = _slug(label)
        shots_dir = self.run_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self._screenshot_seq:03d}-{safe}.png"
        path = shots_dir / name
        try:
            await self.page.screenshot(path=str(path), full_page=full_page)
        except Exception as exc:
            log.warning("screenshot failed: %s", exc)
            return ""
        return str(path.relative_to(self.run_dir.parent)) if self.run_dir.parent in path.parents \
            else str(path)

    async def download(self, ref: str) -> dict[str, Any]:
        """Click a link and capture the download into the run directory."""
        check = await self._require_ref(ref)
        if not check["ok"]:
            return {"ok": False, "error_code": check["error_code"], "detail": check["detail"]}
        node = check["node"]
        handle = await self._resolve_node(ref)
        if handle is None:
            return {"ok": False, "error_code": "stale_ref", "detail": f"element {ref} vanished"}
        self.download_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with self.page.expect_download(timeout=self.timeout_ms) as info:
                await handle.click()
            download = await info.value
            target = self.download_dir / download.suggested_filename
            await download.save_as(str(target))
        except Exception as exc:
            # Some "Download PDF" links are plain navigations, not downloads.
            log.info("download via click failed (%s); trying direct navigation", exc)
            return {"ok": False, "error_code": "download_failed", "detail": str(exc)[:300],
                    "href": node.href}
        return {
            "ok": True,
            "path": str(target),
            "filename": target.name,
            "bytes": target.stat().st_size,
            "href": node.href,
        }

    # -- introspection ----------------------------------------------------
    def console_errors(self) -> list[str]:
        return list(self._console_errors[-20:])

    def is_login_page(self, snap: Snapshot | None = None) -> bool:
        snap = snap or self._last_snapshot
        if snap is None:
            return False
        if "/login" in snap.url.lower():
            return True
        return any(
            n.tag == "input" and n.type == "password" for n in snap.nodes
        ) and any(
            n.tag == "input" and n.type in {"email", "text"} for n in snap.nodes
        )


class _nullcontext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


def re_fullmatch_ref(target: str) -> bool:
    import re

    return bool(re.fullmatch(r"e\d+", target.strip()))


def _looks_like_selector(target: str) -> bool:
    """True when the target reads as CSS rather than as prose.

    Plain words are almost always text someone wants to read on the page, so
    only treat a target as a selector when it carries selector punctuation.
    """
    text = target.strip()
    if not text or " " in text:
        return False
    return any(token in text for token in ("[", "]", "=", "#", ".", ":")) and not text.endswith((".", ","))


def re_compile_contains(value: str) -> Any:
    import re

    return re.compile(re.escape(value), re.I)


def _slug(text: str, limit: int = 48) -> str:
    import re

    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (cleaned or "shot")[:limit]


# The JS source lives in the snapshot module; import it under a private alias so
# this file has a single obvious dependency.
from agent.tools.snapshot import SNAPSHOT_JS as SNAPSHOT_JS_SOURCE  # noqa: E402