"""Unit tests for the pure logic: parsers, safety, verification outcomes.

These run in milliseconds and need neither a browser nor the fixture apps, so
they are the first thing to check when something breaks. End-to-end behaviour
is covered by `evals/run_eval.py`.

    .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import llm_scripted as ls  # noqa: E402
from agent.ground_truth import SqliteGroundTruth, _money  # noqa: E402
from agent.safety import ApprovalStore  # noqa: E402
from agent.verifier import Verifier  # noqa: E402
from common.logging_setup import setup_logging  # noqa: E402

setup_logging(level="CRITICAL")


# --------------------------------------------------------------------------
# snapshot parsing
# --------------------------------------------------------------------------
# Shaped like the real accessibility snapshot the browser tool emits.
SNAPSHOT = """  url: http://127.0.0.1:8001/vendors/V-1005
  title: Hooli Cloud Services
  table
    caption "Newest first by default."
    rowgroup
      row
        columnheader
          link [e6] "Invoice #" href=/vendors/V-1005?sort=number
        columnheader "Issued"
        columnheader "Due"
        columnheader "Amount"
        columnheader "Status"
      row data-invoice-number=HL-2291 data-status=open
        cell 
          link [e10] "HL-2291" href=/vendors/V-1005/invoices/HL-2291
        cell "2026-03-21"
        cell "2026-04-11"
        cell "$14,500.00 USD"
        cell 
          text "Open"
      row data-invoice-number=HL-2260 data-status=open
        cell 
          link [e11] "HL-2260" href=/vendors/V-1005/invoices/HL-2260
        cell "2026-02-16"
        cell "2026-03-09"
        cell "$7,275.00 USD"
        cell 
          text "Open"
      row data-invoice-number=HL-2201 data-status=paid
        cell 
          link [e12] "HL-2201" href=/vendors/V-1005/invoices/HL-2201
        cell "2026-01-03"
        cell "2026-01-24"
        cell "$5,600.00 USD"
        cell 
          text "Paid"
"""


def test_invoice_rows_are_parsed_with_status_and_amount():
    rows = ls.invoice_rows(SNAPSHOT)
    assert [r["number"] for r in rows] == ["HL-2291", "HL-2260", "HL-2201"]
    assert rows[0]["status"] == "open"
    assert rows[0]["due"] == "2026-04-11"
    assert rows[0]["amount"] == 14500.0
    assert rows[0]["currency"] == "USD"
    assert rows[0]["vendor_id"] == "V-1005"
    assert rows[2]["status"] == "paid"


def test_rows_pending_distinguishes_empty_table_from_rendered_rows():
    header_only = '  table\n    rowgroup\n      row\n        columnheader\n          link [e6] "Invoice #"\n'
    assert ls.rows_pending(header_only) is True, "a header with no rows is still loading"
    assert ls.rows_pending(SNAPSHOT) is False
    assert ls.rows_pending("") is True


FORM_WITH_HINT = """  url: http://127.0.0.1:8002/bills/new
  title: New bill
  definition "Invoice number"
    text "Must be unique per vendor."
  definition "Amount"
    text "Enter a positive amount."
"""

FORM_WITH_ERRORS = """  url: http://127.0.0.1:8002/bills/new
  title: New bill
  text "The bill was not saved"
    listitem "Invoice HL-2201 for Hooli Cloud Services was already entered on 2026-01-20 (bill #5). Duplicate invoice numbers are rejected." data-error-for=invoice_number
  label "Vendor"
    combobox [e6] "Vendor" value='Hooli Cloud Services'
"""


def test_error_messages_ignores_the_form_hint():
    """The uniqueness hint reads like an error but is only guidance."""
    assert ls.error_messages(FORM_WITH_HINT) == [], \
        "a hint must not make the agent refuse a perfectly good bill"


def test_error_messages_reports_a_real_rejection():
    assert ls.error_messages(FORM_WITH_ERRORS) == [
        "Invoice HL-2201 for Hooli Cloud Services was already entered on "
        "2026-01-20 (bill #5). Duplicate invoice numbers are rejected."
    ]


def test_pdf_total_prefers_the_total_line():
    text = "Invoice\nTotal\n$7,995.00 USD\nAmount due 7995.00"
    assert ls.pdf_total(text) == 7995.0


def test_server_error_detection_ignores_money_amounts():
    """'$14,500.00' contains '500'; that is not an HTTP error."""
    assert ls._is_server_error('definition "$14,500.00 USD" data-amount=14500.00') is False
    assert ls._is_server_error('status "Bill saved successfully."') is False
    assert ls._is_server_error("503 Service Unavailable\nPlease retry.") is True
    assert ls._is_server_error("HTTP 500 Internal Server Error") is True


def test_login_page_is_recognised():
    assert ls.is_login_page('url: http://127.0.0.1:8001/login\n  heading "Sign in"')
    assert not ls.is_login_page(SNAPSHOT)


# --------------------------------------------------------------------------
# goal parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "goal, vendor",
    [
        ("Enter Hooli Cloud Services' latest payable invoice into AP.", "Hooli Cloud Services"),
        ("Add Acme Corp's invoice", "Acme Corp"),
        ("Log in to the vendor portal and enter Northwind Traders invoice", "Northwind Traders"),
        # Phrasings with no possessive and no adjacent "invoice". These reached
        # the UI as the example buttons and silently produced the wrong vendor,
        # because the run then opened whichever vendor sorted first.
        ("Enter the latest open invoice from Hooli Cloud Services into AP.",
         "Hooli Cloud Services"),
        ("Enter every open payable from Globex Industrial into AP, skipping any already entered.",
         "Globex Industrial"),
        ("Check whether invoice HL-2291 for Hooli Cloud Services was already entered in AP.",
         "Hooli Cloud Services"),
    ],
)
def test_vendor_extraction(goal, vendor):
    assert ls._extract_vendor(goal) == vendor


@pytest.mark.parametrize(
    "goal",
    [
        # These must stay empty: matching them would send the portal nonsense.
        "Enter the latest invoice into AP.",
        "Log in to the vendor portal and record today's bill.",
    ],
)
def test_vendor_extraction_declines_when_there_is_no_vendor(goal):
    assert ls._extract_vendor(goal) == ""


# --------------------------------------------------------------------------
# money in PDFs
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        # The seeded euro PDF extracts with a mangled symbol ("?3,420.00 EUR")
        # because the font has no glyph for it. Requiring a currency *symbol*
        # lost every non-dollar invoice, and the agent blocked itself.
        ("AMOUNT DUE (PDF)\n?3,420.00 EUR\ninterest at 1.5% per month", 3420.0),
        ("AMOUNT DUE\n$14,500.00 USD", 14500.0),
        ("AMOUNT DUE\n€3,420.00 EUR", 3420.0),
        ("TOTAL DUE\n3,420.00 EUR", 3420.0),      # code after the number
        ("TOTAL DUE\nEUR 3,420.00", 3420.0),      # code before the number
        ("AMOUNT DUE\n£1,234.56 GBP", 1234.56),
    ],
)
def test_pdf_total_reads_every_currency_form(text, expected):
    assert ls.pdf_total(text) == expected


def test_pdf_total_ignores_a_bare_number_with_no_currency():
    """No currency means no total — better to report nothing than to guess."""
    assert ls.pdf_total("Purchase order PO-2391\nRemit to account 12345678") is None


def test_claims_only_bills_actually_submitted():
    """A run must not claim an invoice it only read.

    This is the overclaiming the verifier exists to catch; the scripted client
    used to do it to itself, via a fallback to the invoice it was currently
    looking at.
    """
    client = ls.ScriptedClient()
    client.current = {"number": "GX-1140", "vendor": "Globex Industrial",
                      "amount": 3420.0, "currency": "EUR", "due": "2026-04-22"}
    assert client._claimed()["bills"] == [], \
        "reading an invoice is not the same as entering it"

    client.entered_bills.append(dict(client.current))
    claimed = client._claimed()["bills"]
    assert len(claimed) == 1 and claimed[0]["invoice_number"] == "GX-1140"


def parsed(goal: str) -> ls.GoalSpec:
    client = ls.ScriptedClient()
    client._parse_goal_text(goal)
    return client.spec


def test_explicit_invoice_number_overrides_latest():
    spec = parsed("Enter Hooli Cloud Services' latest invoice HL-2260 into AP.")
    assert spec.scope == "exact", "a named invoice beats an implied scope"
    assert spec.invoice == "HL-2260"


def test_latest_scope_when_no_number_named():
    spec = parsed("Enter Hooli Cloud Services' latest payable invoice into AP.")
    assert spec.scope == "latest"
    assert spec.invoice == ""


def test_plural_request_asks_for_every_invoice():
    spec = parsed(
        "Enter Hooli Cloud Services' invoices that have not already been entered into AP."
    )
    assert spec.scope == "all_payable"
    assert spec.skip_duplicates is True


def test_days_regex_reads_a_window():
    spec = parsed("Enter every Hooli Cloud Services invoice due in the next 30 days.")
    assert spec.scope == "due_window"
    assert spec.days == 30


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------
def test_approval_is_scoped_to_one_exact_action():
    store = ApprovalStore()
    store.grant("fp-1", "browser_click", "save bill", granted_by="tester")
    assert store.has("fp-1")
    assert not store.has("fp-2"), "a different action must not inherit the approval"


def test_denied_approval_does_not_grant():
    store = ApprovalStore()
    store.deny("fp-1", "declined")
    assert not store.has("fp-1")


def test_request_approval_is_never_gated():
    from agent.safety import APPROVAL_EXEMPT_TOOLS

    assert "request_approval" in APPROVAL_EXEMPT_TOOLS


# --------------------------------------------------------------------------
# ground truth
# --------------------------------------------------------------------------
def test_money_parsing_handles_commas_and_blanks():
    assert _money("14,500.00") == 14500.0
    assert _money("$7,995.00") == 7995.0
    assert _money(0) == 0.0
    assert _money("") == 0.0, "a blank override means zero, i.e. fall back to the page"


def test_authorised_amounts_reads_the_interaction_audit_log():
    truth = SqliteGroundTruth(ap_db_path=Path("/nonexistent"), portal_db_path=Path("/nonexistent"))
    truth.authorizations = lambda: [{
        "question": "Invoice HL-2260 shows 7275.0 on the page but 7995.0 in the PDF. Which?",
        "answer": "Use the PDF amount (7995.0)",
    }]
    assert truth.authorised_amounts("HL-2260") == {7995.0}
    assert truth.authorised_amounts("HL-2291") == set(), "ruling on one invoice is not a ruling on another"


def test_missing_database_is_not_a_crash():
    truth = SqliteGroundTruth(ap_db_path=Path("/nonexistent"), portal_db_path=Path("/nonexistent"))
    assert truth.ap_bills("HL-2291") == []
    assert truth.portal_invoices("HL-2291") == []


# --------------------------------------------------------------------------
# verifier
# --------------------------------------------------------------------------
class _Criterion:
    def __init__(self, cid="c1"):
        self.id = cid
        self.check = "generic"
        self.description = "a thing"
        self.expected = ""


def test_verifier_accepts_a_provider_returning_an_object():
    from agent.ground_truth import Truth

    async def provider(criterion, claims):
        return Truth(passed=True, detail="confirmed from storage")

    import asyncio

    report = asyncio.run(Verifier(ground_truth_provider=provider).verify([_Criterion()], {}))
    assert report.status == "success"
    assert report.results[0].passed is True


def test_verifier_survives_a_provider_returning_junk():
    async def provider(criterion, claims):
        return "not a result"

    import asyncio

    report = asyncio.run(Verifier(ground_truth_provider=provider).verify([_Criterion()], {}))
    assert report.status != "success"


def test_no_criteria_is_a_failure_not_a_pass():
    import asyncio

    async def provider(criterion, claims):
        return {"passed": True}

    report = asyncio.run(Verifier(ground_truth_provider=provider).verify([], {}))
    assert report.status == "failed"