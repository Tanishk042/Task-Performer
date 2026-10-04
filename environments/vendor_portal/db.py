"""Vendor Portal persistence (SQLite) + deterministic bootstrap."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from common.logging_setup import get_logger
from environments.seed_data import (
    VENDORS,
    SeedInvoice,
    SeedVendor,
    build_invoices,
    due_date_for,
)
from environments.seed_data import format_date

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    legal_name     TEXT NOT NULL,
    country        TEXT NOT NULL,
    currency       TEXT NOT NULL,
    date_style     TEXT NOT NULL,
    terms_days     INTEGER NOT NULL,
    address        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT NOT NULL,
    vendor_id      TEXT NOT NULL REFERENCES vendors(vendor_id),
    issued_on      TEXT NOT NULL,
    due_on         TEXT NOT NULL,
    amount         TEXT NOT NULL,
    pdf_amount     TEXT,
    currency       TEXT NOT NULL,
    status         TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    po_number      TEXT,
    note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    invoice_number TEXT PRIMARY KEY,
    count         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_number_vendor
    ON invoices(vendor_id, invoice_number);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()
    return bool(row and row["n"] > 0)


def bootstrap(db_path: Path, seed: int, reset: bool = False) -> dict[str, int]:
    """Create schema and load deterministic data. Idempotent unless reset."""
    if reset and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    if is_seeded(conn):
        conn.close()
        return {"vendors": 0, "invoices": 0, "skipped": 1}

    invoices = build_invoices(seed)
    vendor_ids: dict[str, str] = {}
    for vendor in VENDORS:
        conn.execute(
            "INSERT OR REPLACE INTO vendors"
            " (vendor_id,name,legal_name,country,currency,date_style,terms_days,address)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (vendor.portal_id, vendor.name, vendor.legal_name, vendor.country,
             vendor.currency, vendor.date_style, vendor.terms_days,
             _address_for(vendor)),
        )
        vendor_ids[vendor.key] = vendor.portal_id

    rows: list[tuple[Any, ...]] = []
    for inv in invoices:
        vendor = _vendor_for_key(inv.vendor_key)
        rows.append(
            (
                inv.invoice_number,
                vendor_ids[inv.vendor_key],
                inv.issued.isoformat(),
                due_date_for(inv, vendor).isoformat(),
                str(inv.amount),
                str(inv.pdf_amount) if inv.pdf_amount is not None else None,
                inv.currency,
                inv.status,
                inv.description,
                inv.po_number,
                inv.note,
            )
        )
    conn.executemany(
        "INSERT INTO invoices (invoice_number,vendor_id,issued_on,due_on,amount,pdf_amount,"
        "currency,status,description,po_number,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    counts = {
        "vendors": conn.execute("SELECT COUNT(*) n FROM vendors").fetchone()["n"],
        "invoices": conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"],
    }
    conn.close()
    log.info("vendor portal seeded: %s invoices across %s vendors", counts["invoices"], counts["vendors"])
    return counts


def _address_for(vendor: SeedVendor) -> str:
    return {
        "United States": "1200 Harbor Point Drive, Suite 400, Oakland, CA 94607",
        "United Kingdom": "18 Bishopsgate, London EC2N 4BQ",
        "Germany": "Hafenstraße 44, 20457 Hamburg",
        "Netherlands": "Wibautstraat 131-D, 1091 GL Amsterdam",
        "Japan": "2-4-1 Marunouchi, Chiyoda-ku, Tokyo 100-6390",
    }.get(vendor.country, "—")


_VENDOR_INDEX = {v.key: v for v in VENDORS}


def _vendor_for_key(key: str) -> SeedVendor:
    return _VENDOR_INDEX[key]


def date_style_for(row: sqlite3.Row | dict) -> str:
    return str(row["date_style"])


def render_date(iso: str, style: str) -> str:
    return format_date(datetime.strptime(iso, "%Y-%m-%d").date(), style)  # type: ignore[arg-type]


def list_vendors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM vendors ORDER BY name COLLATE NOCASE").fetchall()


def get_vendor(conn: sqlite3.Connection, vendor_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()


def find_vendors(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """Case-insensitive substring search over the display name and legal name."""
    like = f"%{query.strip()}%"
    return conn.execute(
        "SELECT * FROM vendors WHERE name LIKE ? COLLATE NOCASE"
        " OR legal_name LIKE ? COLLATE NOCASE ORDER BY name COLLATE NOCASE",
        (like, like),
    ).fetchall()


def count_invoices_for(conn: sqlite3.Connection, vendor_id: str, include_void: bool = True) -> int:
    sql = "SELECT COUNT(*) n FROM invoices WHERE vendor_id = ?"
    if not include_void:
        sql += " AND status != 'void'"
    return conn.execute(sql, (vendor_id,)).fetchone()["n"]


def list_invoices(
    conn: sqlite3.Connection,
    vendor_id: str,
    *,
    sort: str = "issued",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 10,
) -> tuple[list[sqlite3.Row], int]:
    allowed = {"issued": "issued_on", "due": "due_on", "amount": "amount", "number": "invoice_number"}
    column = allowed.get(sort, "issued_on")
    order = "ASC" if direction.lower() == "asc" else "DESC"
    # amount is stored as TEXT so numeric ordering has to be explicit.
    order_expr = "CAST(amount AS REAL)" if column == "amount" else column

    total = conn.execute(
        "SELECT COUNT(*) n FROM invoices WHERE vendor_id = ?", (vendor_id,)
    ).fetchone()["n"]
    offset = max(0, (page - 1) * per_page)
    rows = conn.execute(
        f"SELECT * FROM invoices WHERE vendor_id = ? ORDER BY {order_expr} {order},"
        f" invoice_number {order} LIMIT ? OFFSET ?",
        (vendor_id, per_page, offset),
    ).fetchall()
    return rows, total


def get_invoice(conn: sqlite3.Connection, vendor_id: str, number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE vendor_id = ? AND invoice_number = ?",
        (vendor_id, number),
    ).fetchone()


def record_download(conn: sqlite3.Connection, number: str) -> int:
    conn.execute(
        "INSERT INTO downloads (invoice_number, count) VALUES (?, 1)"
        " ON CONFLICT(invoice_number) DO UPDATE SET count = count + 1",
        (number,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT count FROM downloads WHERE invoice_number = ?", (number,)
    ).fetchone()
    return int(row["count"]) if row else 1


def next_due_invoices(conn: sqlite3.Connection, days: int, reference_iso: str) -> list[sqlite3.Row]:
    """Open invoices due within `days` of `reference_iso`, soonest first."""
    return conn.execute(
        "SELECT i.*, v.name AS vendor_name, v.date_style AS date_style, v.currency AS v_currency"
        " FROM invoices i JOIN vendors v ON v.vendor_id = i.vendor_id"
        " WHERE i.status = 'open' AND julianday(i.due_on) - julianday(?) BETWEEN 0 AND ?"
        " ORDER BY i.due_on ASC",
        (reference_iso, days),
    ).fetchall()


def to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))