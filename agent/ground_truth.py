"""Independent ground truth for the verifier.

The verifier's job is to disagree with the agent when it should. That only works
if it reads the world through a channel the agent cannot influence, so this
module deliberately does *not* use the agent's browser context, cookies or tool
results.

Two channels, in order of trust:

1. **The databases themselves.** A read-only SQLite connection is the strongest
   check available inside this repository: it cannot be fooled by a success
   toast, a stale page, a cached snapshot or a lying agent. It is also honest
   about its limits, which `channel` records on every result — this proves what
   the system stored, not what a person would see on screen.

2. **A fresh browser context.** `BrowserConfirmer` signs in from scratch, with no
   cookies, and re-reads the pages. This is what answers "would a person looking
   at AP right now see the bill?". It is slower, so it runs after the database
   check and only for criteria the database could not settle.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.planner import SuccessCriterion
from common.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Truth:
    passed: bool | None = None          # None = "could not check"
    expected: Any = None
    actual: Any = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    channel: str = "none"


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().lower()


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[^0-9.\-]", "", str(value if value is not None else "0")) or "0")
    except Exception:
        return None


def _norm_date(value: Any) -> str:
    text = str(value or "").strip()
    for pattern, fmt in (
        (r"^(\d{4})-(\d{2})-(\d{2})$", "%Y-%m-%d"),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "%m/%d/%Y"),
        (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", "%d.%m.%Y"),
        (r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})$", "%b %d, %Y"),
    ):
        match = re.match(pattern, text)
        if not match:
            continue
        from datetime import datetime

        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            return text
    return text


class SqliteGroundTruth:
    """Answers criteria by reading the systems' own storage."""

    def __init__(self, *, ap_db_path: Path, portal_db_path: Path) -> None:
        self.ap_db_path = Path(ap_db_path)
        self.portal_db_path = Path(portal_db_path)
        # Discrepancies a human was shown and ruled on, as recorded by the
        # interaction layer: [{invoice, question, options, answer}, ...].
        # Supplied by the runtime from its own audit log, never by the model.
        # A callable, because the log keeps growing as the run proceeds.
        self.authorizations: Callable[[], list[dict[str, Any]]] = list

    def authorised_amounts(self, invoice_number: str) -> set[float]:
        """Amounts a human explicitly picked for this invoice after a conflict.

        A page that disagrees with its own PDF is a real-world situation, not a
        bug: the right outcome is to surface it and enter the figure the human
        chose, not to refuse forever. Anything entered without such a ruling is
        still treated as a guess.
        """
        amounts: set[float] = set()
        for record in self.authorizations():
            blob = f"{record.get('invoice', '')} {record.get('question', '')}"
            if invoice_number.lower() not in blob.lower():
                continue
            answer = str(record.get("answer") or "")
            if not answer:
                continue
            for found in re.findall(r"\(?\$?([0-9][0-9,]*(?:\.[0-9]{1,2})?)\)?", answer):
                amounts.add(_money(found) or 0.0)
        return {a for a in amounts if a > 0}

    # -- connections ------------------------------------------------------
    def _read(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # -- lookups ----------------------------------------------------------
    def ap_bills(self, invoice_number: str) -> list[dict[str, Any]]:
        if not self.ap_db_path.exists():
            return []
        with self._read(self.ap_db_path) as conn:
            rows = conn.execute(
                "SELECT bill_id, vendor_name, invoice_number, amount, currency, "
                "due_date, notes, entered_by, entered_on, needs_manager_approval "
                "FROM bills WHERE UPPER(invoice_number) = UPPER(?)",
                (invoice_number,),
            ).fetchall()
        return [dict(r) for r in rows]

    def portal_invoices(self, invoice_number: str) -> list[dict[str, Any]]:
        if not self.portal_db_path.exists():
            return []
        with self._read(self.portal_db_path) as conn:
            rows = conn.execute(
                "SELECT i.invoice_number, i.amount, i.pdf_amount, i.currency, "
                "i.issued_on, i.due_on, i.status, v.name AS vendor_name, "
                "v.vendor_id FROM invoices i JOIN vendors v ON v.vendor_id = i.vendor_id "
                "WHERE UPPER(i.invoice_number) = UPPER(?)",
                (invoice_number,),
            ).fetchall()
        return [dict(r) for r in rows]

    def duplicate_invoice_numbers(self) -> dict[str, int]:
        if not self.ap_db_path.exists():
            return {}
        with self._read(self.ap_db_path) as conn:
            rows = conn.execute(
                "SELECT vendor_name, invoice_number, COUNT(*) AS n FROM bills "
                "GROUP BY UPPER(vendor_name), UPPER(invoice_number) HAVING n > 1"
            ).fetchall()
        return {f"{r['vendor_name']} / {r['invoice_number']}": r["n"] for r in rows}

    def ap_bill_count(self) -> int:
        if not self.ap_db_path.exists():
            return 0
        with self._read(self.ap_db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0])

    # -- the provider -----------------------------------------------------
    async def __call__(
        self, criterion: SuccessCriterion, claims: dict[str, Any]
    ) -> Truth:
        check = criterion.check
        if check == "bill_exists":
            return self._bill_exists(criterion, claims)
        if check == "bill_field":
            return self._bill_field(criterion, claims)
        if check == "no_duplicate_bill":
            return self._no_duplicates(criterion, claims)
        if check in {"source_values_match", "pdf_matches_portal", "cross_source"}:
            return self._cross_source(criterion, claims)
        if check in {"generic", "portal_reading_found", "invoice_identified"}:
            return self._portal_reading(criterion, claims)
        return Truth(detail=f"no independent check for check={check!r}")

    # -- individual checks -------------------------------------------------
    @staticmethod
    def _claimed(claims: dict[str, Any], number: str) -> dict[str, Any]:
        for bill in claims.get("bills") or []:
            if str(bill.get("invoice_number", "")).upper() == number.upper():
                return bill
        return {}

    def _target_numbers(self, claims: dict[str, Any]) -> list[str]:
        """Invoice numbers to check: whatever the run claims it wrote."""
        numbers = [
            str(b["invoice_number"]) for b in (claims.get("bills") or [])
            if isinstance(b, dict) and b.get("invoice_number")
        ]
        return list(dict.fromkeys(numbers))

    def _bill_exists(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        wanted = str(criterion.expected or "")
        numbers = [wanted] if wanted else self._target_numbers(claims)
        if not numbers:
            return Truth(detail="the run named no invoice number to check",
                         channel="ap_db")
        found = {n: self.ap_bills(n) for n in numbers}
        for number, bills in found.items():
            if bills:
                return Truth(
                    passed=True,
                    expected=f"at least one AP bill for {number}",
                    actual=[{k: b[k] for k in ("bill_id", "vendor_name", "amount",
                                                 "currency", "due_date")} for b in bills],
                    detail=f"AP holds {len(bills)} bill(s) for {number}",
                    evidence=[f"ap_db:bills[{number}]"],
                    channel="ap_db",
                )
        return Truth(
            passed=False,
            expected=f"an AP bill for {', '.join(numbers)}",
            actual="none",
            detail=(
                f"AP contains no bill for {', '.join(numbers)}. The run claimed to have "
                "written one, so the write did not persist."
            ),
            evidence=["ap_db:bills=[]"],
            channel="ap_db",
        )

    def _bill_field(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        """Does AP hold exactly the bill that was claimed, with the claimed values?

        Works with or without an explicit expectation: a criterion that only
        describes the shape ("AP shows exactly one bill for that invoice with the
        expected amount") is checked against the run's own claim, which is the
        only thing worth comparing when nothing else was specified.
        """
        expected = criterion.expected if isinstance(criterion.expected, dict) else {}
        numbers = [str(expected["invoice_number"])] if expected.get("invoice_number") else (
            self._target_numbers(claims)
        )
        if not numbers:
            return Truth(detail="the run named no invoice to check", channel="ap_db")

        number = numbers[0]
        bills = self.ap_bills(number)
        if not bills:
            return Truth(passed=False, expected=expected or number, actual=[],
                         detail=f"the run claims it entered {number}, but AP holds no "
                                f"such bill",
                         channel="ap_db")
        if len(bills) > 1:
            return Truth(passed=False, expected="exactly one bill", actual=len(bills),
                         detail=f"AP holds {len(bills)} bills for {number}; the write was "
                                f"duplicated",
                         channel="ap_db")

        bill = bills[0]
        if expected.get("field"):
            field, wanted = str(expected["field"]), expected.get("value")
            actual = bill.get(field)
            ok = (_money(actual) == _money(wanted) if field in {"amount", "value", "total"}
                  else _norm_date(actual) == _norm_date(wanted)
                  if field in {"due_date", "due"} else _norm(actual) == _norm(wanted))
            return Truth(passed=ok, expected=wanted, actual=actual,
                         detail=f"AP bill #{bill['bill_id']} {field} = {actual!r} "
                                f"(wanted {wanted!r})",
                         channel="ap_db")

        claim = self._claimed(claims, number)
        if not claim:
            return Truth(passed=True,
                         detail=f"AP holds one bill for {number} and no specific value was "
                                f"claimed to compare against",
                         actual={k: bill[k] for k in ("amount", "currency", "due_date")},
                         channel="ap_db")

        mismatches = []
        for field, wanted, actual in (
            ("amount", claim.get("amount"), bill["amount"]),
            ("currency", claim.get("currency"), bill["currency"]),
            ("due_date", claim.get("due_date"), bill["due_date"]),
            ("vendor", claim.get("vendor"), bill["vendor_name"]),
        ):
            if wanted in (None, ""):
                continue
            same = (_money(wanted) == _money(actual) if field == "amount"
                    else _norm_date(wanted) == _norm_date(actual) if field == "due_date"
                    else _norm(wanted) == _norm(actual))
            if not same:
                mismatches.append(f"{field}: wrote {actual!r}, intended {wanted!r}")

        return Truth(
            passed=not mismatches,
            expected=claim,
            actual={k: bill[k] for k in ("amount", "currency", "due_date", "vendor_name")},
            detail=("AP holds exactly the bill that was intended"
                    if not mismatches
                    else "AP bill #%d differs from what was intended: %s"
                         % (bill["bill_id"], "; ".join(mismatches))),
            evidence=[f"ap_db:bills[{number}]#{bill['bill_id']}"],
            channel="ap_db",
        )

    def _no_duplicates(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        duplicates = self.duplicate_invoice_numbers()
        if duplicates:
            return Truth(passed=False, expected="no invoice entered twice",
                         actual=duplicates,
                         detail=f"duplicate invoice numbers in AP: {duplicates}",
                         evidence=["ap_db:duplicate_groups"], channel="ap_db")
        return Truth(passed=True, expected="no invoice entered twice", actual={},
                     detail="every invoice number appears once in AP",
                     evidence=["ap_db:duplicate_groups=[]"], channel="ap_db")

    def _cross_source(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        """Do the two systems and the document agree on the amount?

        Reads the vendor portal's stored invoice and AP's stored bill and compares
        them, so "the agent read 7,275 but the PDF says 7,995" cannot pass.
        """
        numbers = self._target_numbers(claims) or [str(criterion.expected or "")]
        checked: list[str] = []
        for number in numbers:
            invoices = self.portal_invoices(number)
            if not invoices:
                continue
            invoice = invoices[0]
            portal_amount = _money(invoice["amount"])
            # pdf_amount is an *override*: the generator prints it when present
            # and otherwise prints the page amount. A blank column therefore
            # means the PDF agrees with the page, not that the PDF says zero.
            pdf_amount = _money(invoice["pdf_amount"])
            printed = pdf_amount or portal_amount
            bills = self.ap_bills(number)
            checked.append(
                f"{number}: portal page={invoice['amount']} pdf={invoice['pdf_amount']} "
                f"ap={[b['amount'] for b in bills] or 'no bill'}"
            )

            if portal_amount != printed:
                # The portal contradicts itself. That is only acceptable if the
                # contradiction was put to a human and the figure that landed in
                # AP is the one they picked.
                ruled = self.authorised_amounts(number)
                entered = _money(bills[0]["amount"]) if bills else None
                if ruled and entered is not None and entered in ruled:
                    checked.append(
                        f"{number}: discrepancy escalated to the user; authorised "
                        f"{entered}, AP has {entered}"
                    )
                else:
                    return Truth(
                        passed=False,
                        expected="the portal page and its PDF agree",
                        actual={"page": str(portal_amount), "pdf": str(printed)},
                        detail=(f"Invoice {number} is inconsistent in the vendor portal: the "
                                f"page shows {portal_amount} and the PDF shows {printed}. "
                                + (f"AP holds {entered}, which was not the figure a human "
                                   "authorised." if entered is not None
                                   else "No figure was authorised for it.")
                                + " Entering one of them without flagging the other is a "
                                "wrong bill."),
                        evidence=[f"portal_db:invoices[{number}]"], channel="portal_db")

            if not bills:
                continue
            ap_amount = _money(bills[0]["amount"])
            # Once a human has ruled on the portal's own contradiction, the
            # figure that counts is the one they authorised, not the page.
            authoritative = (
                ap_amount if ap_amount in self.authorised_amounts(number)
                else portal_amount
            )
            if ap_amount != authoritative:
                return Truth(
                    passed=False,
                    expected=f"the amount stored in the portal ({authoritative})",
                    actual=f"the amount stored in AP ({ap_amount})",
                    detail=(f"AP holds {ap_amount} for {number}, but the vendor portal says "
                            f"{authoritative}. The bill was entered from the wrong figure."),
                    evidence=[f"portal_db:{number}", f"ap_db:bills[{number}]"],
                    channel="portal_db+ap_db")

        if not checked:
            return Truth(detail="nothing to cross-check", channel="portal_db")
        return Truth(passed=True, expected="portal, PDF and AP agree", actual=checked,
                     detail="the amount in AP matches the vendor portal, and the portal's own "
                            "PDF agrees with its page",
                     evidence=checked, channel="portal_db+ap_db")

    def _pdf_agrees(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        numbers = self._target_numbers(claims) or [str(criterion.expected or "")]
        checked: list[str] = []
        for number in numbers:
            invoices = self.portal_invoices(number)
            for invoice in invoices:
                checked.append(
                    f"{number}: page={invoice['amount']} "
                    f"pdf={invoice['pdf_amount'] or invoice['amount']}"
                )
                page_amount = _money(invoice["amount"])
                pdf_amount = _money(invoice["pdf_amount"]) or page_amount
                if page_amount != pdf_amount and not self.authorised_amounts(number):
                    return Truth(
                        passed=False,
                        expected=f"page amount == pdf amount for {number}",
                        actual={"page": str(page_amount), "pdf": str(pdf_amount)},
                        detail=(
                            f"Invoice {number} is internally inconsistent in the portal: "
                            f"the page shows {page_amount} and the PDF shows {pdf_amount}, "
                            "and no human was asked which one to trust. Whatever was "
                            "entered, one of those two numbers is a guess."
                        ),
                        evidence=[f"portal_db:invoices[{number}]"],
                        channel="portal_db",
                    )
        if not checked:
            return Truth(detail="no portal invoice was found to cross-check",
                         channel="portal_db")
        return Truth(passed=True, expected="page and PDF agree", actual=checked,
                     detail="page amount and PDF amount agree for every invoice checked",
                     evidence=checked, channel="portal_db")

    def _portal_reading(self, criterion: SuccessCriterion, claims: dict[str, Any]) -> Truth:
        """The invoice the agent claimed to have read really exists and is payable."""
        numbers = self._target_numbers(claims) or [str(criterion.expected or "")]
        for number in numbers:
            invoices = self.portal_invoices(number)
            if not invoices:
                continue
            invoice = invoices[0]
            if invoice["status"] in {"draft", "void"}:
                return Truth(passed=False, expected="a payable invoice",
                             actual=invoice["status"],
                             detail=(
                                 f"{number} has status '{invoice['status']}', which is not "
                                 "payable, so it should not have been entered."
                             ),
                             channel="portal_db")
            return Truth(passed=True, expected="a payable invoice",
                         actual={k: invoice[k] for k in ("invoice_number", "status",
                                                         "amount", "currency", "due_on")},
                         detail=f"{number} exists and is {invoice['status']}",
                         channel="portal_db")
        return Truth(passed=False, expected="an invoice in the portal", actual=None,
                     detail=f"no portal invoice found for {', '.join(numbers)}",
                     channel="portal_db")


# --------------------------------------------------------------------------
# second channel: a fresh browser
# --------------------------------------------------------------------------

class BrowserConfirmer:
    """Re-reads the systems from a brand new, cookie-less browser context.

    This is the channel that catches the difference between "the database has the
    row" and "a person opening AP would see the bill". It is also the channel
    that survives the agent having crashed.
    """

    def __init__(
        self,
        *,
        settings: Any,
        run_dir: Path,
        headless: bool = True,
    ) -> None:
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.headless = headless
        self._session: Any = None
        self.checked: list[dict[str, Any]] = []

    async def __aenter__(self) -> "BrowserConfirmer":
        from agent.tools.browser import BrowserSession

        self._session = BrowserSession(
            headless=self.headless,
            timeout_ms=self.settings.browser_timeout_ms,
            run_dir=self.run_dir,
            label="verifier",
        )
        await self._session.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def confirm_bill_visible(self, invoice_number: str,
                                   vendor_name: str = "") -> dict[str, Any]:
        """Sign in to AP from scratch and look for the bill on screen."""
        if self._session is None:
            return {"ok": False, "detail": "confirmer not started"}
        result: dict[str, Any] = {"invoice_number": invoice_number, "ok": False}
        try:
            await self._login(self.settings.ap_system_url,
                              self.settings.ap_system_credentials)
            snap = await self._session.snapshot()
            result["url"] = snap.url
            text = snap.text or ""
            result["visible"] = invoice_number.lower() in text.lower()
            result["ok"] = result["visible"]
            if vendor_name:
                result["vendor_visible"] = vendor_name.lower() in text.lower()
            result["detail"] = (
                f"a fresh AP session searching for {invoice_number}: "
                + ("the invoice is listed" if result["visible"]
                   else "the invoice is NOT listed")
            )
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"the independent browser check failed: {exc}"
        self.checked.append(result)
        return result

    async def _login(self, base_url: str, credentials: Any) -> None:
        import agent.llm_scripted as scripted

        await self._session.goto(f"{base_url}/login")
        text = (await self._session.snapshot()).text
        node = scripted.find_control(text, "email", "username", "work email",
                                     kinds=("textbox",))
        if node and node.ref:
            await self._session.type_text(node.ref, credentials.username)
        text = (await self._session.snapshot()).text
        node = scripted.find_control(text, "password", kinds=("textbox",))
        if node and node.ref:
            await self._session.type_text(node.ref, credentials.password)
        text = (await self._session.snapshot()).text
        button = scripted.find_control(text, "sign in", "log in", kinds=("button",))
        if button and button.ref:
            await self._session.click(button.ref)