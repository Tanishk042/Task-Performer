"""HTTP + SSE around a run, plus the single page that drives it.

Routes are deliberately boring. The interesting behaviour is all in
`orchestrator.runs`; this module is transport.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                                StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common.config import Settings, get_settings
from common.logging_setup import get_logger
from orchestrator.runs import RunManager, RunSession

log = get_logger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

#: Example tasks offered in the UI. Kept short and aimed at the seeded world.
EXAMPLES = [
    "Enter the latest open invoice from Hooli Cloud Services into AP.",
    "Enter invoice HL-2260 for Hooli Cloud Services into AP.",
    "Enter every open payable from Globex Industrial into AP, skipping any already entered.",
    "Check whether invoice HL-2291 for Hooli Cloud Services was already entered in AP.",
]


class StartRun(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    headless: bool = True
    max_steps: int = Field(default=0, ge=0, le=1000)
    confirm_in_browser: bool = True


class Answer(BaseModel):
    question_id: str
    answer: str = Field(default="", max_length=4000)


class Faults(BaseModel):
    """A fault to arm on the mock apps. `reset` disarms everything."""

    name: str = "reset"
    enabled: bool = True
    reset: bool = False
    params: dict[str, Any] | None = None


def create_app(settings: Settings | None = None,
               manager: RunManager | None = None) -> FastAPI:
    settings = settings or get_settings()
    runs = manager or RunManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Parked runs would otherwise be left with a browser open and a task that
        # nobody will ever await.
        await runs.shutdown()

    app = FastAPI(title="AI Worker", version="1.0", docs_url="/api/docs",
                  lifespan=lifespan)
    app.state.runs = runs
    app.state.settings = settings

    if UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")

    # -- helpers ----------------------------------------------------------
    def session_or_404(run_id: str) -> RunSession:
        session = runs.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        return session

    # -- pages ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"))

    # -- runs -------------------------------------------------------------
    @app.post("/api/runs")
    async def start_run(body: StartRun) -> dict[str, Any]:
        session = await runs.start(
            body.goal, headless=body.headless, max_steps=body.max_steps,
            confirm_in_browser=body.confirm_in_browser)
        return session.summary()

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        live = [s.summary() for s in runs.sessions()]
        return sorted(live, key=lambda r: r.get("started_at") or 0, reverse=True)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return session_or_404(run_id).summary()

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        session = session_or_404(run_id)
        await session.cancel()
        return session.summary()

    # -- human in the loop ------------------------------------------------
    @app.post("/api/runs/{run_id}/answer")
    async def answer(run_id: str, body: Answer) -> dict[str, Any]:
        session = session_or_404(run_id)
        if not await session.answer(body.question_id, body.answer):
            # The question may already have been answered, or the run may have
            # moved on. Saying so plainly beats silently accepting a stale click.
            raise HTTPException(status_code=409,
                                detail="that question is not awaiting an answer")
        return {"ok": True, "status": session.status}

    @app.get("/api/runs/{run_id}/pending")
    async def pending(run_id: str) -> list[dict[str, Any]]:
        return session_or_404(run_id).interaction.pending()

    # -- live feed --------------------------------------------------------
    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, since: int = Query(default=0, ge=0)):
        session = session_or_404(run_id)
        bus = session.bus

        async def stream():
            queue = bus.subscribe()
            try:
                # Replay first so a tab opened mid-run is immediately correct,
                # then follow live. No gap: both happen without an await between.
                backlog = bus.history() if since == 0 else bus.replay_from(since)
                for payload in backlog:
                    yield _sse(payload)
                # Hand back the sequence we have reached so a dropped connection
                # can resume with ?since= instead of replaying from zero.
                yield _sse({"event": "ready", "seq": bus.seq,
                            "data": {"run_id": run_id, "seq": bus.seq,
                                     "status": session.status}})
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Keeps proxies from reaping an idle connection.
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(payload)
                    if payload["event"] == "run.state" and not session.running:
                        break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    # -- artefacts --------------------------------------------------------
    @app.get("/api/runs/{run_id}/screenshot")
    async def screenshot(run_id: str):
        """Newest screenshot on disk. Cheap enough to poll; it is just a file.

        204 rather than 404 while there is nothing yet: the UI polls this
        continuously from page load, and a stream of 404s shows up as a wall of
        red in the browser console for the first several seconds of every run.
        """
        session = session_or_404(run_id)
        shots = sorted((session.run_dir / "screenshots").glob("*.png"))
        if not shots:
            return Response(status_code=204)
        return FileResponse(shots[-1], media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/trace")
    async def trace(run_id: str) -> dict[str, Any]:
        """The append-only trace, for the replay panel."""
        session = session_or_404(run_id)
        path = session.run_dir / "trace.jsonl"
        if not path.exists():
            return {"events": []}
        events_ = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events_.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"events": events_}

    @app.get("/api/runs/{run_id}/artifact/{name}")
    async def artifact(run_id: str, name: str):
        session = session_or_404(run_id)
        if "/" in name or ".." in name:          # keep the sandbox honest
            raise HTTPException(status_code=400, detail="bad artifact name")
        target = (session.run_dir / name).resolve()
        if not str(target).startswith(str(session.run_dir.resolve())):
            raise HTTPException(status_code=400, detail="artifact escapes the run dir")
        if not target.exists():
            raise HTTPException(status_code=404, detail="no such artifact")
        return FileResponse(target)

    # -- fault injection --------------------------------------------------
    @app.post("/api/faults")
    async def set_faults(body: Faults) -> dict[str, Any]:
        """Proxy to both mock apps so the browser never makes a cross-origin call.

        The env apps take {enabled, faults[], params, reset}. Getting this shape
        wrong is silent — they return 200 and simply change nothing — so the
        result of each app is echoed back rather than swallowed.
        """
        targets = {
            "vendor_portal": settings.vendor_portal_url,
            "ap_system": settings.ap_system_url,
        }
        if body.reset or not body.enabled:
            payload = {"enabled": False, "faults": [], "reset": True}
        else:
            payload = {"enabled": True, "faults": [body.name], "reset": False,
                       "params": body.params}

        results: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            for app_name, base in targets.items():
                try:
                    response = await client.post(f"{base}/admin/faults", json=payload)
                    body_json = response.json() if response.content else {}
                    results[app_name] = {
                        "ok": response.status_code < 400 and "error" not in body_json,
                        "status": response.status_code,
                        "error": body_json.get("error", ""),
                        "armed": body_json.get("armed", []),
                    }
                except httpx.HTTPError as exc:
                    results[app_name] = {"ok": False, "error": str(exc)}
        return results

    @app.get("/api/faults")
    async def get_faults() -> dict[str, Any]:
        targets = {
            "vendor_portal": settings.vendor_portal_url,
            "ap_system": settings.ap_system_url,
        }
        state: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            for app_name, base in targets.items():
                try:
                    response = await client.get(f"{base}/admin/faults")
                    state[app_name] = response.json()
                except httpx.HTTPError as exc:
                    state[app_name] = {"error": str(exc)}
        return state

    @app.get("/api/config")
    async def public_config() -> dict[str, Any]:
        return {
            "examples": EXAMPLES,
            "vendor_portal_url": settings.vendor_portal_url,
            "ap_system_url": settings.ap_system_url,
            "approval_threshold": settings.approval_amount_threshold,
            "approval_currency": settings.approval_amount_currency,
            "world_today": settings.world_today,
            "max_steps": settings.agent_max_steps,
        }

    @app.get("/api/health")
    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "runs": len(runs.sessions())})

    return app


def _sse(payload: dict[str, Any]) -> str:
    """One SSE frame. The event name carries the type, data stays JSON."""
    return (f"id: {payload.get('seq', 0)}\n"
            f"event: {payload.get('event', 'message')}\n"
            f"data: {json.dumps(payload, default=str)}\n\n")


app = create_app()