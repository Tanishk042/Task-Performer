"""Small helpers shared by the two simulated company apps."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from environments.faults import FAULTS

PAGE_SIZE_DEFAULT = 10

STATUS_LABELS = {
    "open": "Open",
    "paid": "Paid",
    "draft": "Draft",
    "void": "Void",
}


def session_cookie_name(app: str) -> str:
    return f"{app}_session"


def make_templates(app_dir: Path) -> Jinja2Templates:
    """Jinja env spanning the shared base templates and one app's own templates."""
    from environments import jinja_filters

    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(str(app_dir.parent / "templates")),
                FileSystemLoader(str(app_dir / "templates")),
            ]
        ),
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    jinja_filters.register(env)
    templates = Jinja2Templates(env=env)
    templates.env = env
    return templates


def status_tag(status: str) -> str:
    """Map an invoice status to a CSS modifier used by the templates."""
    return {
        "open": "tag",
        "paid": "tag tag--muted",
        "draft": "tag tag--warn",
        "void": "tag tag--muted",
    }.get(status, "tag")


def require_auth(
    request: Request,
    conn: Any,
    session_user: Callable[[Any, Request], str | None],
    app: str,
) -> str | Response:
    """Return the username, or a redirect to the login page."""
    user = session_user(conn, request)
    if user is not None:
        return user
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?redirect={target}", status_code=303)


def admin_faults_router(app_name: str) -> APIRouter:
    """Fault-injection admin endpoints, namespaced per app."""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/faults")
    def get_faults() -> dict[str, Any]:
        return {"app": app_name, **FAULTS.state()}

    @router.post("/faults")
    async def set_faults(request: Request) -> dict[str, Any]:
        payload = await request.json() if await request.body() else {}
        enabled = payload.get("enabled")
        faults = payload.get("faults")
        params = payload.get("params")
        reset = bool(payload.get("reset", True))
        try:
            state = FAULTS.configure(
                enabled=None if enabled is None else bool(enabled),
                faults=list(faults) if faults is not None else None,
                params=params or None,
                reset=reset,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return {"app": app_name, **state}

    @router.post("/reset")
    def reset_faults() -> dict[str, Any]:
        return {"app": app_name, **FAULTS.configure(enabled=False, faults=[], reset=True)}

    return router