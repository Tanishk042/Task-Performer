"""Internal AP System — a real (small) web app on port 8002.

Login, New Bill form, bills list + detail. Enforces required fields, duplicate
invoice-number rejection, ISO due-date format, and the >$10,000 manager-approval
rule.

The `ui_rename` fault rewrites every form control's id, name *and* visible label
(and the server accepts either the canonical or the renamed field names), so an
agent that memorised selectors or button text breaks loudly.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from common.config import get_settings
from common.logging_setup import get_logger
from environments import jinja_filters
from environments.faults import FAULTS
from environments.seed_data import VENDORS, format_money
from environments.shared_web import (
    PAGE_SIZE_DEFAULT,
    admin_faults_router,
    make_templates,
    require_auth,
    session_cookie_name,
)
from environments.ap_system import db as ap_db

log = get_logger(__name__)
settings = get_settings()
APP_NAME = "Accounts Payable"
CONTEXT_LABEL = "Northwind Financial"
APPROVAL_THRESHOLD = 10_000.0

BASE_DIR = Path(__file__).resolve().parent
templates = make_templates(BASE_DIR)

app = FastAPI(title=APP_NAME, docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(BASE_DIR.parent / "static")), name="static")
app.include_router(admin_faults_router("ap_system"))


def get_conn() -> sqlite3.Connection:
    return ap_db.connect(settings.ap_db_path)


@app.on_event("startup")
def _startup() -> None:
    ap_db.bootstrap(settings.ap_db_path, settings.seed)
    log.info("ap system ready on %s", settings.ap_system_url)


# --------------------------------------------------------------------------
# form field naming (mutated by the ui_rename fault)
# --------------------------------------------------------------------------

CANONICAL_FIELDS: dict[str, tuple[str, str, str]] = {
    # key: (id, name, label)
    "vendor": ("vendor", "vendor", "Vendor"),
    "invoice_number": ("invoice-number", "invoice_number", "Invoice number"),
    "amount": ("amount", "amount", "Amount"),
    "currency": ("currency", "currency", "Currency"),
    "due_date": ("due-date", "due_date", "Due date"),
    "notes": ("notes", "notes", "Notes"),
}

RENAMED_FIELDS: dict[str, tuple[str, str, str]] = {
    "vendor": ("vendor-account", "vendor_account", "Vendor account"),
    "invoice_number": ("document-ref", "document_ref", "Document reference"),
    "amount": ("total-value", "total_value", "Total value"),
    "currency": ("ccy-code", "ccy_code", "Currency code"),
    "due_date": ("payment-due-on", "payment_due_on", "Payment due on"),
    "notes": ("internal-notes", "internal_notes", "Internal notes"),
}

CANONICAL_SUBMIT = ("save-bill", "save_bill", "Save bill")
RENAMED_SUBMIT = ("commit-entry", "commit_entry", "Commit entry")


def fields() -> dict[str, tuple[str, str, str]]:
    return RENAMED_FIELDS if FAULTS.is_armed("ui_rename") else CANONICAL_FIELDS


def submit_button() -> tuple[str, str, str]:
    return RENAMED_SUBMIT if FAULTS.is_armed("ui_rename") else CANONICAL_SUBMIT


def _pick(form: dict[str, Any], key: str) -> str:
    """Read a form value under either the canonical or the renamed field name."""
    for table in (CANONICAL_FIELDS, RENAMED_FIELDS):
        _, name, _ = table[key]
        if name in form:
            return str(form[name]).strip()
    return ""


# --------------------------------------------------------------------------
# session
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
    token = request.cookies.get(session_cookie_name("ap_system"))
    if not token:
        return None
    row = conn.execute("SELECT username FROM sessions WHERE token = ?", (token,)).fetchone()
    return row["username"] if row else None


def _expire_session(conn: sqlite3.Connection, request: Request) -> None:
    token = request.cookies.get(session_cookie_name("ap_system"))
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


def render(
    request: Request,
    template: str,
    *,
    conn: sqlite3.Connection,
    status_code: int = 200,
    toasts: list[dict[str, str]] | None = None,
    **ctx: Any,
) -> HTMLResponse:
    """Render a page, surfacing any one-shot flash cookie as a toast."""
    flash = request.cookies.get("ap_flash")
    all_toasts = list(toasts or [])
    if flash:
        kind, _, message = flash.partition("|")
        all_toasts.append({"message": message, "kind": kind or "ok"})
    response = templates.TemplateResponse(
        request,
        template,
        {
            "app_name": APP_NAME,
            "context_label": CONTEXT_LABEL,
            "current_user": _session_user(conn, request),
            "toasts": all_toasts,
            "fields": fields(),
            "submit": submit_button(),
            "approval_threshold": APPROVAL_THRESHOLD,
            **ctx,
        },
        status_code=status_code,
    )
    if flash:
        response.delete_cookie("ap_flash", path="/")
    return response


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"app": "ap_system", "ok": True, "faults": FAULTS.state()}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    conn = get_conn()
    if _session_user(conn, request) is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/bills", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, redirect: str = Query(default="/bills")) -> Response:
    conn = get_conn()
    if _session_user(conn, request) is not None:
        return RedirectResponse(redirect, status_code=303)
    return render(request, "ap_system/login.html", conn=conn, redirect=redirect)


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    redirect: str = Form(default="/bills"),
) -> Response:
    conn = get_conn()
    expected = settings.ap_system_credentials
    if username.strip() == expected.username and password == expected.password:
        token = _new_session(conn, expected.username)
        response = RedirectResponse(redirect or "/bills", status_code=303)
        response.set_cookie(
            session_cookie_name("ap_system"), token, httponly=True, samesite="lax", path="/"
        )
        return response
    return render(
        request, "ap_system/login.html", conn=conn, redirect=redirect,
        status_code=401, error="Those credentials were not recognised.", username=username,
    )


@app.post("/logout")
def logout(request: Request) -> Response:
    conn = get_conn()
    _expire_session(conn, request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(session_cookie_name("ap_system"), path="/")
    return response


@app.get("/bills", response_class=HTMLResponse)
def bills_page(
    request: Request,
    q: str = Query(default=""),
    vendor: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="entered"),
    direction: str = Query(default="desc"),
) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "ap_system")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    rows, total = ap_db.list_bills(
        conn, q=q, vendor=vendor, sort=sort, direction=direction, page=page, per_page=per_page
    )
    pages = max(1, -(-total // per_page))
    return render(
        request, "ap_system/bills.html", conn=conn, bills=rows, total=total,
        page=page, pages=pages, q=q, vendor=vendor, sort=sort, direction=direction,
        vendors=VENDORS, status_tag=_status_tag,
    )


@app.get("/bills/new", response_class=HTMLResponse)
def new_bill_form(request: Request, vendor: str = Query(default="")) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "ap_system")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    return render(
        request, "ap_system/new_bill.html", conn=conn, vendors=VENDORS,
        form={}, errors={}, vendor_preselect=vendor,
    )


@app.post("/bills/new", response_class=HTMLResponse)
async def new_bill_submit(request: Request) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "ap_system")
    if isinstance(user, Response):
        return user

    form = await request.form()
    values = {key: _pick(dict(form), key) for key in CANONICAL_FIELDS}

    # Fault: first submit attempt returns 503 and stores nothing.
    if FAULTS.consume_once("transient_500_on_submit"):
        log.warning("transient_500_on_submit fault: returning 503 for a bill submit")
        return HTMLResponse(
            "<!doctype html><html><body style='font:14px system-ui;padding:3rem'>"
            "<h1>503 Service Unavailable</h1>"
            "<p>The AP service is temporarily unavailable. Please retry.</p></body></html>",
            status_code=503,
        )

    errors = validate_bill(conn, values)

    if not errors:
        if FAULTS.is_armed("silent_save_failure"):
            # The nasty one: report success, write nothing.
            FAULTS.note_fired("silent_save_failure", "suppressed write, showed success toast")
            log.error("silent_save_failure fault: discarding bill %s", values.get("invoice_number"))
            return _toast_redirect("/bills", "Bill saved successfully.", "ok")

        bill_id = ap_db.insert_bill(
            conn,
            vendor_name=values["vendor"],
            invoice_number=values["invoice_number"],
            amount=float(values["amount"]),
            currency=values["currency"],
            due_date=values["due_date"],
            notes=values["notes"],
            entered_by=settings.ap_system_credentials.username,
            entered_on=settings.world_today,
            needs_manager_approval=float(values["amount"]) > APPROVAL_THRESHOLD,
        )
        log.info("bill %s created (id=%s) needs_approval=%s",
                 values["invoice_number"], bill_id,
                 float(values["amount"]) > APPROVAL_THRESHOLD)
        note = " Manager approval required." if float(values["amount"]) > APPROVAL_THRESHOLD else ""
        return _toast_redirect(
            f"/bills/{bill_id}", f"Bill saved successfully.{note}", "ok"
        )

    return render(
        request, "ap_system/new_bill.html", conn=conn, vendors=VENDORS,
        form=values, errors=errors, vendor_preselect=values["vendor"], status_code=400,
    )


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: int) -> Response:
    conn = get_conn()
    user = require_auth(request, conn, _session_user, "ap_system")
    if isinstance(user, Response):
        return user
    _maybe_expire(conn, request)
    bill = ap_db.get_bill(conn, bill_id)
    if bill is None:
        return render(request, "ap_system/not_found.html", conn=conn, status_code=404,
                      what=f"bill {bill_id}")
    return render(
        request, "ap_system/bill_detail.html", conn=conn, bill=bill,
        amount_display=format_money(bill["amount"], bill["currency"]),
        status_tag=_status_tag,
    )


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate_bill(conn: sqlite3.Connection, values: dict[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    vendor = values.get("vendor", "")
    number = values.get("invoice_number", "")
    amount = values.get("amount", "")
    currency = values.get("currency", "")
    due = values.get("due_date", "")

    if not vendor:
        errors["vendor"] = "Select a vendor."
    if not number:
        errors["invoice_number"] = "Invoice number is required."
    if not amount:
        errors["amount"] = "Amount is required."
    else:
        try:
            parsed = float(amount)
            if parsed <= 0:
                errors["amount"] = "Amount must be greater than zero."
        except ValueError:
            errors["amount"] = "Amount must be a number, e.g. 4820.00"
    if not currency:
        errors["currency"] = "Currency is required."
    if not due:
        errors["due_date"] = "Due date is required."
    else:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            errors["due_date"] = "Due date must be in YYYY-MM-DD format."

    if not errors.get("invoice_number") and not errors.get("vendor"):
        existing = ap_db.find_by_invoice(conn, vendor, number)
        if existing is not None:
            errors["invoice_number"] = (
                f"Invoice {number} for {vendor} was already entered on "
                f"{existing['entered_on']} (bill #{existing['bill_id']}). "
                "Duplicate invoice numbers are rejected."
            )
    return errors


# --------------------------------------------------------------------------
# JSON API (read paths used by http_request + the verifier)
# --------------------------------------------------------------------------

@app.get("/api/vendors")
def api_vendors() -> JSONResponse:
    return JSONResponse({"vendors": [{"name": v.name, "vendor_id": v.portal_id} for v in VENDORS]})


@app.get("/api/bills")
def api_bills(
    request: Request,
    q: str = Query(default=""),
    vendor: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> JSONResponse:
    conn = get_conn()
    rows, total = ap_db.list_bills(conn, q=q, vendor=vendor, sort="entered",
                                   direction="desc", page=1, per_page=limit)
    return JSONResponse(
        {
            "total": total,
            "count": len(rows),
            "bills": [
                {
                    "bill_id": r["bill_id"],
                    "vendor_name": r["vendor_name"],
                    "invoice_number": r["invoice_number"],
                    "amount": f"{float(r['amount']):.2f}",
                    "currency": r["currency"],
                    "due_date": r["due_date"],
                    "needs_manager_approval": bool(r["needs_manager_approval"]),
                    "entered_by": r["entered_by"],
                    "entered_on": r["entered_on"],
                    "notes": r["notes"],
                }
                for r in rows
            ],
        }
    )


@app.get("/api/bills/{bill_id}")
def api_bill(request: Request, bill_id: int) -> JSONResponse:
    conn = get_conn()
    bill = ap_db.get_bill(conn, bill_id)
    if bill is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "bill_id": bill["bill_id"],
            "vendor_name": bill["vendor_name"],
            "invoice_number": bill["invoice_number"],
            "amount": f"{float(bill['amount']):.2f}",
            "currency": bill["currency"],
            "due_date": bill["due_date"],
            "needs_manager_approval": bool(bill["needs_manager_approval"]),
            "entered_by": bill["entered_by"],
            "entered_on": bill["entered_on"],
            "notes": bill["notes"],
        }
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _status_tag(status: str) -> str:
    return {"open": "tag", "paid": "tag tag--muted", "draft": "tag tag--warn",
            "void": "tag tag--muted"}.get(status, "tag")


def _toast_redirect(location: str, message: str, kind: str = "ok") -> Response:
    response = RedirectResponse(location, status_code=303)
    response.set_cookie(
        "ap_flash", f"{kind}|{message}", httponly=False, samesite="lax", path="/", max_age=30
    )
    return response


def _maybe_expire(conn: sqlite3.Connection, request: Request) -> None:
    if FAULTS.should_expire_session("ap_system"):
        _expire_session(conn, request)
        FAULTS.note_fired("ap_system", "session dropped; next navigation redirects to /login")