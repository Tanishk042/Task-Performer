"""Vendor Portal — a real (small) web app on port 8001.

Pages: login, vendor directory, per-vendor invoice list (paginated, sortable),
invoice detail, PDF download. Plus a read-only JSON API and a fault admin
endpoint, both used by the agent's `http_request` tool and the eval harness.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from common.config import get_settings
from common.logging_setup import get_logger
from environments.faults import FAULTS, TransientInjectedFault
from environments.seed_data import format_money
from environments.shared_web import (
    PAGE_SIZE_DEFAULT,
    admin_faults_router,
    make_templates,
    require_auth,
    session_cookie_name,
)
from environments.vendor_portal import db as vp_db
from environments.vendor_portal.pdf import build_invoice_pdf, invoice_filename

log = get_logger(__name__)
settings = get_settings()
APP_NAME = "Vendor Portal"
CONTEXT_LABEL = "Northwind Procurement"

BASE_DIR = Path(__file__).resolve().parent
templates = make_templates(BASE_DIR)

app = FastAPI(title=APP_NAME, docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(BASE_DIR.parent / "static")), name="static")
app.include_router(admin_faults_router("vendor_portal"))


def get_conn() -> sqlite3.Connection:
    return vp_db.connect(settings.vendor_db_path)


@app.on_event("startup")
def _startup() -> None:
    vp_db.bootstrap(settings.vendor_db_path, settings.seed)
    FAULTS.configure(enabled=settings.fault_injection)
    log.info("vendor portal ready on %s", settings.vendor_portal_url)


# --------------------------------------------------------------------------
# session helpers
# --------------------------------------------------------------------------

def _new_session(conn: sqlite3.Connection, username: str) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO sessions (token, username, created_at) VALUES (?,?,?)",
        (token, username, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    return token


def _session_user(conn: sqlite3.Connection, request: Request) -> str | None:
    token = request.cookies.get(session_cookie_name("vendor_portal"))
    if not token:
        return None
    row = conn.execute("SELECT username FROM sessions WHERE token = ?", (token,)).fetchone()
    return row["username"] if row else None


def _expire_session(conn: sqlite3.Connection, request: Request) -> None:
    token = request.cookies.get(session_cookie_name("vendor_portal"))
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


@app.middleware("http")
async def fault_middleware(request: Request, call_next: Any) -> Response:
    """Applies slow-page + session-expiry faults to authenticated page loads."""
    path = request.url.path
    if request.method == "GET" and FAULTS.is_armed("slow_page") and not path.startswith((
        "/static", "/api/health", "/admin"
    )):
        delay = FAULTS.slow_page_pause()
        if delay:
            log.info("slow_page fault: pausing %.1fs on %s", delay, path)
            time.sleep(delay)
    return await call_next(request)


def render(
    request: Request,
    template: str,
    *,
    conn: sqlite3.Connection,
    status_code: int = 200,
    toasts: list[dict[str, str]] | None = None,
    **ctx: Any,
) -> HTMLResponse:
    current_user = _session_user(conn, request)
    return templates.TemplateResponse(
        request,
        template,
        {
            "app_name": APP_NAME,
            "context_label": CONTEXT_LABEL,
            "current_user": current_user,
            "toasts": toasts or [],
            **ctx,
        },
        status_code=status_code,
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"app": "vendor_portal", "ok": True, "faults": FAULTS.state()}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    conn = get_conn()
    if _session_user(conn, request) is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/vendors", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, redirect: str = Query(default="/vendors")) -> Response:
    conn = get_conn()
    if _session_user(conn, request) is not None:
        return RedirectResponse(redirect, status_code=303)
    return render(request, "vendor_portal/login.html", conn=conn, redirect=redirect)


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    redirect: str = Form(default="/vendors"),
) -> Response:
    conn = get_conn()
    expected = settings.vendor_portal_credentials
    if username.strip() == expected.username and password == expected.password:
        token = _new_session(conn, expected.username)
        response = RedirectResponse(redirect or "/vendors", status_code=303)
        response.set_cookie(
            session_cookie_name("vendor_portal"), token, httponly=True, samesite="lax", path="/"
        )
        log.info("vendor portal login ok for %s", username)
        return response
    return render(
        request,
        "vendor_portal/login.html",
        conn=conn,
        redirect=redirect,
        status_code=401,
        error="Those credentials were not recognised.",
        username=username,
    )


@app.post("/logout")
def logout(request: Request) -> Response:
    conn = get_conn()
    _expire_session(conn, request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session_cookie_name("vendor_portal"), path="/")
    return response


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request, q: str = Query(default="")) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "vendor_portal")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    rows = vp_db.find_vendors(conn, q) if q.strip() else vp_db.list_vendors(conn)
    counts = {r["vendor_id"]: vp_db.count_invoices_for(conn, r["vendor_id"]) for r in rows}
    return render(
        request, "vendor_portal/vendors.html", conn=conn, vendors=rows, counts=counts, q=q
    )


@app.get("/vendors/{vendor_id}", response_class=HTMLResponse)
def vendor_invoices(
    request: Request,
    vendor_id: str,
    sort: str = Query(default="issued"),
    dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=50),
) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "vendor_portal")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    vendor = vp_db.get_vendor(conn, vendor_id)
    if vendor is None:
        return render(
            request, "vendor_portal/not_found.html", conn=conn,
            status_code=404, what=f"vendor {vendor_id}",
        )
    rows, total = vp_db.list_invoices(
        conn, vendor_id, sort=sort, direction=dir, page=page, per_page=per_page
    )
    pages = max(1, -(-total // per_page))
    style = vp_db.date_style_for(vendor)
    return render(
        request,
        "vendor_portal/invoices.html",
        conn=conn,
        vendor=vendor,
        invoices=rows,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        sort=sort,
        direction=dir,
        date_style=style,
        flaky_render=FAULTS.is_armed("flaky_render"),
        flaky_ms=int(FAULTS.param("flaky_render_ms", 1400)),
        show_rows_html=templates.env.get_template(
            "vendor_portal/_invoice_rows.html"
        ).module.inv_rows(rows, vendor, style),
    )


@app.get("/vendors/{vendor_id}/invoices/{number}", response_class=HTMLResponse)
def invoice_detail(request: Request, vendor_id: str, number: str) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "vendor_portal")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    vendor = vp_db.get_vendor(conn, vendor_id)
    invoice = vp_db.get_invoice(conn, vendor_id, number)
    if vendor is None or invoice is None:
        return render(
            request, "vendor_portal/not_found.html", conn=conn,
            status_code=404, what=f"invoice {number}",
        )
    style = vp_db.date_style_for(vendor)
    return render(
        request,
        "vendor_portal/invoice_detail.html",
        conn=conn,
        vendor=vendor,
        invoice=invoice,
        date_style=style,
        issued_display=vp_db.render_date(invoice["issued_on"], style),
        due_display=vp_db.render_date(invoice["due_on"], style),
        amount_display=format_money(invoice["amount"], invoice["currency"]),
        pdf_amount_display=(
            format_money(invoice["pdf_amount"], invoice["currency"])
            if invoice["pdf_amount"] else None
        ),
    )


@app.get("/vendors/{vendor_id}/invoices/{number}/download")
def invoice_pdf(request: Request, vendor_id: str, number: str) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "vendor_portal")
    if isinstance(user, Response):
        return user
    vendor = vp_db.get_vendor(conn, vendor_id)
    invoice = vp_db.get_invoice(conn, vendor_id, number)
    if vendor is None or invoice is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if FAULTS.is_armed("session_expiry") and FAULTS.should_expire_session("vendor_portal"):
        _expire_session(conn, request)
        return RedirectResponse(
            f"/login?redirect=/vendors/{vendor_id}/invoices/{number}", status_code=303
        )
    count = vp_db.record_download(conn, number)
    log.info("pdf download %s (%s)", number, count)
    return Response(
        content=build_invoice_pdf(invoice, vendor),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{invoice_filename(invoice)}"'
        },
    )


# --------------------------------------------------------------------------
# read-only JSON API
# --------------------------------------------------------------------------

@app.get("/api/vendors")
def api_vendors(request: Request, q: str = Query(default="")) -> JSONResponse:
    conn = get_conn()
    rows = vp_db.find_vendors(conn, q) if q.strip() else vp_db.list_vendors(conn)
    return JSONResponse(
        {
            "vendors": [
                {
                    "vendor_id": r["vendor_id"],
                    "name": r["name"],
                    "legal_name": r["legal_name"],
                    "country": r["country"],
                    "currency": r["currency"],
                    "invoice_count": vp_db.count_invoices_for(conn, r["vendor_id"]),
                }
                for r in rows
            ]
        }
    )


@app.get("/api/vendors/{vendor_id}/invoices")
def api_invoices(
    request: Request,
    vendor_id: str,
    sort: str = Query(default="issued"),
    dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=100),
) -> JSONResponse:
    conn = get_conn()
    vendor = vp_db.get_vendor(conn, vendor_id)
    if vendor is None:
        return JSONResponse({"error": "unknown vendor"}, status_code=404)
    rows, total = vp_db.list_invoices(
        conn, vendor_id, sort=sort, direction=dir, page=page, per_page=per_page
    )
    style = vp_db.date_style_for(vendor)
    return JSONResponse(
        {
            "vendor": {"vendor_id": vendor["vendor_id"], "name": vendor["name"]},
            "total": total,
            "page": page,
            "date_format": style,
            "invoices": [
                {
                    "invoice_number": r["invoice_number"],
                    "issued_on": r["issued_on"],
                    "issued_display": vp_db.render_date(r["issued_on"], style),
                    "due_on": r["due_on"],
                    "due_display": vp_db.render_date(r["due_on"], style),
                    "amount": str(r["amount"]),
                    "currency": r["currency"],
                    "status": r["status"],
                }
                for r in rows
            ],
        }
    )


@app.get("/api/invoices/{vendor_id}/{number}")
def api_invoice(request: Request, vendor_id: str, number: str) -> JSONResponse:
    conn = get_conn()
    vendor = vp_db.get_vendor(conn, vendor_id)
    invoice = vp_db.get_invoice(conn, vendor_id, number)
    if vendor is None or invoice is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    style = vp_db.date_style_for(vendor)
    return JSONResponse(
        {
            "invoice_number": invoice["invoice_number"],
            "vendor_id": vendor_id,
            "vendor_name": vendor["name"],
            "vendor_legal_name": vendor["legal_name"],
            "issued_on": invoice["issued_on"],
            "issued_display": vp_db.render_date(invoice["issued_on"], style),
            "due_on": invoice["due_on"],
            "due_display": vp_db.render_date(invoice["due_on"], style),
            "amount": str(invoice["amount"]),
            "currency": invoice["currency"],
            "status": invoice["status"],
            "description": invoice["description"],
            "po_number": invoice["po_number"],
            "note": invoice["note"],
            "date_format": style,
            "pdf_amount": str(invoice["pdf_amount"]) if invoice["pdf_amount"] else None,
        }
    )


@app.get("/api/invoices-due")
def api_invoices_due(
    request: Request, days: int = Query(default=7, ge=1, le=365),
    reference: str = Query(default=""),
) -> JSONResponse:
    conn = get_conn()
    ref = reference or settings.world_today
    try:
        date.fromisoformat(ref)
    except ValueError:
        return JSONResponse({"error": "reference must be YYYY-MM-DD"}, status_code=400)
    rows = vp_db.next_due_invoices(conn, days, ref)
    return JSONResponse(
        {
            "reference": ref,
            "days": days,
            "invoices": [
                {
                    "vendor_id": r["vendor_id"],
                    "vendor_name": r["vendor_name"],
                    "invoice_number": r["invoice_number"],
                    "amount": str(r["amount"]),
                    "currency": r["currency"],
                    "due_on": r["due_on"],
                    "due_display": vp_db.render_date(r["due_on"], r["date_style"]),
                    "status": r["status"],
                }
                for r in rows
            ],
        }
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _maybe_expire(conn: sqlite3.Connection, request: Request) -> None:
    """Session-expiry fault: after N authenticated loads, log the user out."""
    if FAULTS.should_expire_session("vendor_portal"):
        _expire_session(conn, request)
        # The page still renders; the *next* navigation bounces to /login.
        FAULTS.note_fired("vendor_portal", "session dropped; next navigation redirects to /login")