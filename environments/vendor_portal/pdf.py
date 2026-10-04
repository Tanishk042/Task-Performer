"""Invoice PDF rendering for the vendor portal's 'Download PDF' link."""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from decimal import Decimal

from common.pdfwriter import PdfDocument, render
from environments.seed_data import format_date, format_money


def _fmt_date(iso: str, style: str) -> str:
    return format_date(datetime.strptime(iso, "%Y-%m-%d").date(), style)  # type: ignore[arg-type]


def build_invoice_pdf(invoice: sqlite3.Row, vendor: sqlite3.Row) -> bytes:
    """Render one invoice.

    Note: the amount printed here comes from `pdf_amount` when the vendor set
    one, which is how the seeded cross-check conflict is produced.
    """
    doc = PdfDocument(title=f"Invoice {invoice['invoice_number']}")
    doc.add_line(vendor["legal_name"], size=16, bold=True)
    doc.add_line(vendor["address"], size=9)
    doc.add_line(f"{vendor['country']}", size=9)
    doc.add_space(6)
    doc.add_line(f"INVOICE {invoice['invoice_number']}", size=13, bold=True)
    doc.add_space(14)

    doc.add_line("Issue date", size=8, bold=True)
    doc.add_line(_fmt_date(invoice["issued_on"], vendor["date_style"]), size=11)
    doc.add_space(6)
    doc.add_line("Payment due", size=8, bold=True)
    doc.add_line(_fmt_date(invoice["due_on"], vendor["date_style"]), size=11)
    doc.add_space(6)
    if invoice["po_number"]:
        doc.add_line("Purchase order", size=8, bold=True)
        doc.add_line(str(invoice["po_number"]), size=11)
        doc.add_space(6)
    doc.add_line("Status", size=8, bold=True)
    doc.add_line(str(invoice["status"]).upper(), size=11)
    doc.add_space(14)

    doc.add_rule()
    doc.add_space(6)
    doc.add_line("DESCRIPTION", size=8, bold=True)
    doc.add_paragraph(str(invoice["description"] or "-"), size=11)
    doc.add_space(10)

    shown = Decimal(str(invoice["pdf_amount"] or invoice["amount"]))
    total = format_money(shown, invoice["currency"])
    doc.add_line("AMOUNT DUE (PDF)", size=8, bold=True)
    doc.add_line(f"{total} {invoice['currency']}", size=15, bold=True)
    doc.add_space(14)

    if invoice["pdf_amount"] and Decimal(str(invoice["pdf_amount"])) != Decimal(str(invoice["amount"])):
        doc.add_paragraph(
            "This document reflects amendment 2 and supersedes the originally issued total.",
            size=9,
        )
    if invoice["note"]:
        doc.add_space(6)
        doc.add_paragraph(f"Note: {invoice['note']}", size=9)

    doc.add_space(20)
    doc.add_paragraph(
        "Remit within terms. Late balances accrue interest at 1.5% per month.",
        size=8,
    )
    return render(doc)


def invoice_filename(invoice: sqlite3.Row) -> str:
    return f"{invoice['vendor_id']}-{invoice['invoice_number']}.pdf"