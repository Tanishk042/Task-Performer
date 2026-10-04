"""Internal AP System persistence (SQLite)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from common.logging_setup import get_logger
from environments.seed_data import build_ap_seed_bills, build_invoices

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    bill_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name             TEXT NOT NULL,
    invoice_number          TEXT NOT NULL,
    amount                  REAL NOT NULL,
    currency                TEXT NOT NULL,
    due_date                TEXT NOT NULL,
    notes                   TEXT NOT NULL DEFAULT '',
    entered_by              TEXT NOT NULL DEFAULT '',
    entered_on              TEXT NOT NULL DEFAULT '',
    needs_manager_approval  INTEGER NOT NULL DEFAULT 0,
    approved_by             TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_vendor_invoice
    ON bills(vendor_name, invoice_number);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()
    return bool(row and row["n"] > 0)


def bootstrap(db_path: Path, seed: int, reset: bool = False) -> dict[str, int]:
    if reset and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    if is_seeded(conn):
        conn.close()
        return {"bills": 0, "skipped": 1}

    invoices = build_invoices(seed)
    seed_bills = build_ap_seed_bills(invoices)
    conn.executemany(
        "INSERT OR IGNORE INTO bills (vendor_name, invoice_number, amount, currency, due_date,"
        " notes, entered_by, entered_on, needs_manager_approval, approved_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                b["vendor_name"], b["invoice_number"], b["amount"], b["currency"],
                b["due_date"], b["notes"], b["entered_by"], b["entered_on"],
                int(b.get("needs_manager_approval", False)), b.get("approved_by"),
            )
            for b in seed_bills
        ],
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) n FROM bills").fetchone()["n"]
    conn.close()
    log.info("ap system seeded: %s pre-existing bills", count)
    return {"bills": count}


def list_bills(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    vendor: str = "",
    sort: str = "entered",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[sqlite3.Row], int]:
    where: list[str] = []
    params: list[Any] = []
    if q.strip():
        where.append("(invoice_number LIKE ? OR vendor_name LIKE ? OR notes LIKE ?)")
        like = f"%{q.strip()}%"
        params += [like, like, like]
    if vendor.strip():
        where.append("vendor_name = ?")
        params.append(vendor.strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    allowed = {
        "entered": "entered_on", "due": "due_date", "amount": "amount",
        "vendor": "vendor_name", "invoice": "invoice_number",
    }
    column = allowed.get(sort, "entered_on")
    order = "ASC" if direction.lower() == "asc" else "DESC"

    total = conn.execute(f"SELECT COUNT(*) n FROM bills{clause}", params).fetchone()["n"]
    offset = max(0, (page - 1) * per_page)
    rows = conn.execute(
        f"SELECT * FROM bills{clause} ORDER BY {column} {order}, bill_id {order}"
        " LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    return rows, total


def get_bill(conn: sqlite3.Connection, bill_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()


def find_by_invoice(conn: sqlite3.Connection, vendor: str, number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bills WHERE vendor_name = ? AND invoice_number = ?", (vendor, number)
    ).fetchone()


def find_by_number(conn: sqlite3.Connection, number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bills WHERE invoice_number = ? ORDER BY bill_id", (number,)
    ).fetchall()


def insert_bill(
    conn: sqlite3.Connection,
    *,
    vendor_name: str,
    invoice_number: str,
    amount: float,
    currency: str,
    due_date: str,
    notes: str = "",
    entered_by: str = "",
    entered_on: str = "",
    needs_manager_approval: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO bills (vendor_name, invoice_number, amount, currency, due_date, notes,"
        " entered_by, entered_on, needs_manager_approval)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (vendor_name, invoice_number, amount, currency, due_date, notes,
         entered_by, entered_on, int(needs_manager_approval)),
    )
    conn.commit()
    return int(cur.lastrowid)


def count_bills(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) n FROM bills").fetchone()["n"]


def all_bills(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bills ORDER BY bill_id").fetchall()