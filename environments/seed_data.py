"""Deterministic seed data for both simulated company apps.

Everything here is derived from `Settings.seed` and `Settings.world_today` so
that two runs on different days produce byte-identical data, and relative tasks
("due in the next 7 days") have a stable answer.

Deliberate traps baked in (each maps to an eval scenario):
  * `Acme Corp` vs `Acme Corporation Ltd`            -> ambiguity / ask_user
  * newest invoice for one vendor is `void`          -> "latest" must skip it
  * newest for another is `draft`                    -> "latest" must skip it
  * `Hooli-2291` PDF amount != page summary amount   -> cross-check conflict
  * four date display formats across vendors         -> extraction
  * USD / EUR / GBP                                   -> currency handling
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

InvoiceStatus = Literal["open", "paid", "draft", "void"]
DateStyle = Literal["iso", "us", "long", "dotted"]


@dataclass(frozen=True)
class SeedVendor:
    key: str
    name: str
    legal_name: str
    country: str
    currency: str
    date_style: DateStyle
    terms_days: int
    portal_id: str


@dataclass
class SeedInvoice:
    invoice_number: str
    vendor_key: str
    issued: date
    amount: Decimal
    currency: str
    status: InvoiceStatus
    description: str
    po_number: str | None = None
    #: Amount printed on the PDF. Differs from `amount` for the conflict case.
    pdf_amount: Decimal | None = None
    note: str = ""


# --------------------------------------------------------------------------
# Vendors
# --------------------------------------------------------------------------

VENDORS: list[SeedVendor] = [
    SeedVendor("acme_corp", "Acme Corp", "Acme Corp (North America) Inc.", "United States",
               "USD", "iso", 30, "V-1001"),
    SeedVendor("acme_ltd", "Acme Corporation Ltd", "Acme Corporation Limited", "United Kingdom",
               "GBP", "long", 45, "V-1002"),
    SeedVendor("globex", "Globex Industrial", "Globex Industrial GmbH", "Germany",
               "EUR", "dotted", 30, "V-1003"),
    SeedVendor("initech", "Initech Systems", "Initech Systems LLC", "United States",
               "USD", "us", 30, "V-1004"),
    SeedVendor("hooli", "Hooli Cloud Services", "Hooli Cloud Services Inc.", "United States",
               "USD", "iso", 21, "V-1005"),
    SeedVendor("stark", "Stark Logistics", "Stark Logistics B.V.", "Netherlands",
               "EUR", "iso", 14, "V-1006"),
    SeedVendor("wayne", "Wayne Security Supply", "Wayne Security Supply LLC", "United States",
               "USD", "long", 30, "V-1007"),
    SeedVendor("cyberdyne", "Cyberdyne Robotics", "Cyberdyne Robotics KK", "Japan",
               "USD", "us", 60, "V-1008"),
]

VENDOR_BY_KEY = {v.key: v for v in VENDORS}


@dataclass
class SeedApState:
    """Pre-existing bills in the AP system."""

    bills: list[dict] = field(default_factory=list)


def _today() -> date:
    return datetime.strptime(_TODAY_STR, "%Y-%m-%d").date()


_TODAY_STR = "2026-04-01"


def build_invoices(seed: int = 20260401) -> list[SeedInvoice]:
    """Return the full invoice set. Pure function of `seed`."""
    rng = random.Random(seed)
    today = _today()

    def plus(days: int) -> date:
        return today + timedelta(days=days)

    lines: list[SeedInvoice] = [
        # ---- Acme Corp: the happy path. Latest OPEN invoice is AC-2291. ----
        SeedInvoice("AC-2277", "acme_corp", plus(-96), Decimal("2410.00"), "USD", "paid",
                    "Warehouse consumables - Q1", po_number="PO-4471"),
        SeedInvoice("AC-2288", "acme_corp", plus(-58), Decimal("3180.50"), "USD", "open",
                    "Pallet racking hardware", po_number="PO-4519"),
        SeedInvoice("AC-2291", "acme_corp", plus(-12), Decimal("4820.00"), "USD", "open",
                    "Forklift service contract Q2", po_number="PO-4602"),
        SeedInvoice("AC-2295", "acme_corp", plus(-3), Decimal("960.00"), "USD", "draft",
                    "Pending: calibration visit", note="Draft - awaiting signed change order."),
        SeedInvoice("AC-2296", "acme_corp", plus(-1), Decimal("150.00"), "USD", "void",
                    "Duplicate submission - cancelled", note="Voided at vendor request."),

        # ---- Acme Corporation Ltd: the ambiguous sibling. -----------------
        SeedInvoice("ACL-88", "acme_ltd", plus(-70), Decimal("5120.00"), "GBP", "open",
                    "Consulting retainer", po_number="PO-7731"),
        SeedInvoice("ACL-91", "acme_ltd", plus(-20), Decimal("2740.00"), "GBP", "open",
                    "Localisation services", po_number="PO-7802"),
        SeedInvoice("ACL-92", "acme_ltd", plus(-6), Decimal("1150.00"), "GBP", "paid",
                    "Archive migration"),

        # ---- Globex: dotted dates, EUR, one invoice over the $10k threshold.
        SeedInvoice("GX-1042", "globex", plus(-140), Decimal("8300.00"), "EUR", "paid",
                    "CNC calibration rig", po_number="PO-2210"),
        SeedInvoice("GX-1088", "globex", plus(-75), Decimal("12450.00"), "EUR", "open",
                    "Robotic arm retrofit line 3", po_number="PO-2288"),
        SeedInvoice("GX-1121", "globex", plus(-33), Decimal("6890.00"), "EUR", "open",
                    "Annual maintenance contract", po_number="PO-2340"),
        SeedInvoice("GX-1140", "globex", plus(-9), Decimal("3420.00"), "EUR", "open",
                    "Safety certification audit", po_number="PO-2391"),

        # ---- Initech: already fully in AP (duplicate-detection scenario). --
        SeedInvoice("IN-5501", "initech", plus(-64), Decimal("2990.00"), "USD", "open",
                    "Payroll module licences", po_number="PO-3312"),
        SeedInvoice("IN-5540", "initech", plus(-27), Decimal("4150.00"), "USD", "open",
                    "Workflow automation tier 2", po_number="PO-3388"),
        SeedInvoice("IN-5566", "initech", plus(-14), Decimal("1880.00"), "USD", "open",
                    "Developer seat expansion", po_number="PO-3411"),
        SeedInvoice("IN-5570", "initech", plus(-5), Decimal("2250.00"), "USD", "open",
                    "Onboarding workshop", po_number="PO-3430"),
        SeedInvoice("IN-5581", "initech", plus(-26), Decimal("3640.00"), "USD", "open",
                    "Compliance audit support", po_number="PO-3455"),

        # ---- Hooli: latest invoice is over the $10k approval threshold. -----
        SeedInvoice("HL-2201", "hooli", plus(-88), Decimal("5600.00"), "USD", "paid",
                    "Compute reserved instances", po_number="PO-9001"),
        SeedInvoice("HL-2260", "hooli", plus(-44), Decimal("7275.00"), "USD", "open",
                    "Object storage - March", po_number="PO-9088",
                    pdf_amount=Decimal("7995.00"),
                    note="Revised amount per amendment 2. Portal summary may lag."),
        SeedInvoice("HL-2291", "hooli", plus(-11), Decimal("14500.00"), "USD", "open",
                    "Annual enterprise commitment", po_number="PO-9140"),

        # ---- Stark: nearest due date, used by the 'next 7 days' scenario. ---
        SeedInvoice("ST-300", "stark", plus(-61), Decimal("1150.00"), "EUR", "paid",
                    "Freight - Rotterdam lane"),
        SeedInvoice("ST-311", "stark", plus(-40), Decimal("2380.00"), "EUR", "open",
                    "Freight - intra EU blockhaul"),
        SeedInvoice("ST-322", "stark", plus(-24), Decimal("990.00"), "EUR", "open",
                    "Warehouse handling fees"),
        SeedInvoice("ST-330", "stark", plus(-9), Decimal("3100.00"), "EUR", "open",
                    "Customs brokerage retainer"),

        # ---- Wayne ---------------------------------------------------------
        SeedInvoice("WN-7001", "wayne", plus(-52), Decimal("725.00"), "USD", "open",
                    "Access control hardware", po_number="PO-5501"),
        SeedInvoice("WN-7042", "wayne", plus(-23), Decimal("1980.00"), "USD", "open",
                    "Perimeter camera refresh"),
        SeedInvoice("WN-7088", "wayne", plus(-8), Decimal("2640.00"), "USD", "open",
                    "Guard services - March"),
        SeedInvoice("WN-7104", "wayne", plus(-25), Decimal("3320.00"), "USD", "open",
                    "Key management system", po_number="PO-5540"),

        # ---- Cyberdyne: 60-day terms, so its newest invoice is due in ~52 days.
        SeedInvoice("CY-8801", "cyberdyne", plus(-55), Decimal("18750.00"), "USD", "open",
                    "Kinetic actuator prototyping", po_number="PO-6601"),
        SeedInvoice("CY-8830", "cyberdyne", plus(-30), Decimal("9400.00"), "USD", "open",
                    "Control firmware licence"),
        SeedInvoice("CY-8855", "cyberdyne", plus(-16), Decimal("6150.00"), "USD", "open",
                    "Field test harness rental"),
    ]

    # A little deterministic jitter so the data does not look synthetic, but keep
    # amounts exactly 2dp and dates inside a sane window.
    for invoice in lines:
        if invoice.status == "draft":
            continue
        jitter = Decimal(rng.randint(-3, 3)) * Decimal("10")
        invoice.amount = (invoice.amount + jitter).quantize(Decimal("0.01"))

    return lines


def due_date_for(invoice: SeedInvoice, vendor: SeedVendor) -> date:
    return invoice.issued + timedelta(days=vendor.terms_days)


# --------------------------------------------------------------------------
# Pre-existing AP state
# --------------------------------------------------------------------------

def build_ap_seed_bills(invoices: list[SeedInvoice]) -> list[dict]:
    """Bills already sitting in the AP system.

    `IN-5501` is deliberately pre-entered so the "check whether it was already
    entered" scenario has a correct *no-op* answer, and `ST-322`/`WN-7042` are
    pre-entered so the "due in the next 7 days" scenario has a realistic mix of
    already-done and outstanding work.
    """
    pre_entered = {
        "IN-5501": ("2026-02-20", "mwilson", "Imported from legacy AP during migration."),
        "IN-5540": ("2026-03-10", "mwilson", "Imported from legacy AP during migration."),
        # The *newest* Initech invoice is already in AP — this is the duplicate
        # detection scenario: a correct agent must not create a second bill.
        "IN-5570": ("2026-03-30", "dpatel", "Entered from vendor portal by dpatel."),
        "ST-322": ("2026-03-14", "dpatel", "Imported from legacy AP during migration."),
        "WN-7042": ("2026-03-15", "dpatel", "Imported from legacy AP during migration."),
        "GX-1042": ("2026-01-12", "dpatel", "Imported from legacy AP during migration."),
        "HL-2201": ("2026-01-20", "mwilson", "Imported from legacy AP during migration."),
    }

    bills: list[dict] = []
    for invoice in invoices:
        meta = pre_entered.get(invoice.invoice_number)
        if meta is None:
            continue
        vendor = VENDOR_BY_KEY[invoice.vendor_key]
        entered_on, entered_by, notes = meta
        bill = {
            "vendor_name": vendor.name,
            "invoice_number": invoice.invoice_number,
            "amount": float(invoice.amount),
            "currency": invoice.currency,
            "due_date": due_date_for(invoice, vendor).isoformat(),
            "notes": notes,
            "entered_by": entered_by,
            "entered_on": entered_on,
            "source": "migration",
        }
        if float(invoice.amount) > 10000.0:
            bill["needs_manager_approval"] = True
            bill["approved_by"] = "a.okafor"
        bills.append(bill)
    return bills


def format_money(amount: Decimal | float, currency: str) -> str:
    symbols = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}
    symbol = symbols.get(currency, "")
    return f"{symbol}{Decimal(str(amount)):,.2f}"


def format_date(value: date, style: DateStyle) -> str:
    if style == "iso":
        return value.isoformat()
    if style == "us":
        return value.strftime("%m/%d/%Y")
    if style == "dotted":
        return value.strftime("%d.%m.%Y")
    return value.strftime("%b %-d, %Y")


def expected_latest_open(vendor_key: str, invoices: list[SeedInvoice] | None = None) -> SeedInvoice:
    """Ground-truth helper for evals: newest non-draft, non-void invoice."""
    invoices = invoices if invoices is not None else build_invoices()
    vendor = VENDOR_BY_KEY[vendor_key]
    candidates = [
        inv for inv in invoices
        if inv.vendor_key == vendor_key and inv.status in {"open", "paid"}
    ]
    if not candidates:
        raise LookupError(f"no billable invoice for {vendor.name}")
    return max(candidates, key=lambda inv: inv.issued)