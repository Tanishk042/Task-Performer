"""Scripted offline provider: a deterministic policy engine, not a language model.

Why this exists
---------------
The interesting engineering here is not the model call — it is the loop, the
safety gate, the recovery ladder and the verifier. Those must be exercisable
without an API key and without non-determinism, so this module implements the
same `complete()` contract with an explicit procedure that reacts to
observations.

What it is not
--------------
It is not a stand-in for the agent's intelligence and it is not a mock of the
loop. It has no privileged access: it receives only the message list the loop
sends a real model (system prompt, goal briefing, snapshots, tool results) and
returns tool calls. It parses URLs and credentials out of the briefing the way it
would read them off a page, and finds form fields by visible label, so the
`ui_rename` fault is handled for free. Swap in `AnthropicClient` and the loop,
safety, recovery and verifier are untouched.

Honest limitation, stated here because it matters: this proves the harness
works end to end. It does not prove an LLM can generalise to unseen goals or
unseen page layouts.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agent.llm import AssistantTurn, Message, ToolCall
from common.logging_setup import get_logger

log = get_logger(__name__)

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")

_URL_RE = re.compile(r"https?://[^\s,)\]]+")
_CRED_RE = re.compile(r"(?:sign in|log ?in|login)\s+as\s+(\S+)\s*(?:/|with)\s*(\S+)", re.I)
_MONEY_RE = re.compile(
    r"(?:[$€£]\s?(?P<sym>[\d,]+(?:\.\d{1,2})?)"
    r"|\b(?P<code>[\d,]+(?:\.\d{1,2})?)\s?(?:USD|EUR|GBP)\b)",
    re.I,
)
#: ISO currency codes. Needed because a PDF's font encoding routinely mangles the
#: symbol into a replacement character — the euro invoice in this world extracts
#: as "?3,420.00 EUR" — so requiring a symbol loses every non-dollar invoice.
_CURRENCY_CODE = r"(?:USD|EUR|GBP|CHF|CAD|AUD|NZD|SEK|NOK|DKK|PLN|INR|JPY)"
_NUM = r"[\d,]+(?:\.\d{1,2})?"

#: Money, with the number captured in whichever of a/b/c matched.
_MONEY_IN_TEXT = re.compile(
    rf"(?:[$€£]\s*(?P<a>{_NUM}))"
    rf"|(?:{_CURRENCY_CODE}\s*(?P<b>{_NUM}))"
    rf"|(?:(?P<c>{_NUM})\s*{_CURRENCY_CODE}\b)"
)

#: What this agent is for. A request that mentions neither the AP system nor an
#: invoice is not a task, and starting a write path for it is how you enter a
#: bill nobody asked for.
_TASK_OBJECT = re.compile(
    r"\b(ap|accounts?\s+payable|bills?|invoices?|payables?|vendor\s+portal|portal)\b", re.I
)
#: Removing things is not a capability here. Without this, "delete every bill"
#: happily went looking for a bill to enter instead.
_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|erase|wipe|purge|undo|revoke|void\s+all|drop\s+all)\b", re.I
)
_TASK_VERB = re.compile(
    r"\b(enter|create|add|record|submit|upload|key|post|put|check|verify|"
    r"look\s*up|find|search|list|show|report|flag)\b",
    re.I,
)
_DAYS_RE = re.compile(r"(?:next|coming|within)\s+(\d+)\s+days?", re.I)
_REF_RE = re.compile(r"\[(e\d+)\]")
_INVOICE_NUMBER_RE = re.compile(r"\b[A-Z]{1,5}[-–][A-Z0-9]{2,10}\b")


# ==========================================================================
# observation parsing — operates on formatted snapshot text, exactly what a
# model sees. Nothing here touches the browser or the database.
# ==========================================================================

_NOT_VENDORS = {
    "today", "the", "please", "log in", "sign in", "ap", "manager", "latest",
    "next", "every", "each", "all", "invoice", "invoices", "bill", "bills",
    "vendor", "vendors", "manager", "approval", "system", "portal",
}

# An invoice number as people write it in a request: a short letter prefix, a
# dash, then digits (HL-2260, INV-1043, PO-77). Naming one explicitly overrides
# any "latest"/"most recent" scope in the same request.
_INVOICE_NO_RE = re.compile(r"\b([A-Z]{2,4}[-\u2010]\d{2,6})\b")

# Verbs a task starts with, which sit right next to the vendor name and would
# otherwise be swallowed by a capitalised-word run ("Add Acme Corp's").
_LEADING_VERBS = {
    "add", "enter", "log", "sign", "create", "open", "find", "upload", "submit",
    "record", "put", "please", "then", "also", "and", "for", "from", "take",
}

_PROPER = r"[A-Z][\w&.\-]*"


def _extract_vendor(goal: str) -> str:
    """Find the vendor a task is about, without guessing.

    Ordered from most to least specific: a possessive name, then a name after a
    preposition, then any multi-word capitalised phrase. Single capitalised
    words are ignored, because "latest" or "Invoice" would match those and
    sending a nonsense search to the portal teaches us nothing.
    """
    # "'s" before "s'", and a bare apostrophe last: a plural possessive
    # ("Traders'") would otherwise be read as the name ending in an s.
    possessive = r"(?:'s\b|s'|\u2019s\b|s\u2019|')"
    candidates = [
        rf"(?:from|for|at|with)\s+({_PROPER}(?:\s+{_PROPER}){{0,3}})\s*{possessive}",
        rf"({_PROPER}(?:\s+{_PROPER}){{1,3}})\s*{possessive}",
        rf"(?:from|for|at)\s+({_PROPER}(?:\s+{_PROPER}){{0,3}})"
        r"(?=\s+(?:is|was|has|had)\b|[,.;:]|$)",
        rf"({_PROPER}(?:\s+{_PROPER}){{1,3}})(?=\s+(?:invoice|invoices|bill|bills)\b)",
        # A capitalised run after a preposition, ending wherever the capitals
        # stop. Every pattern above needs a possessive, a copula or an adjacent
        # "invoice" to close the name, so the very ordinary "the latest invoice
        # from Hooli Cloud Services into AP" slips past all of them. Last in the
        # list, so the specific shapes keep priority.
        rf"(?:from|for|at|with)\s+({_PROPER}(?:\s+{_PROPER}){{1,3}})(?=\s|$|[,.;:])",
    ]
    for pattern in candidates:
        for match in re.finditer(pattern, goal):
            words = re.sub(r"\s+", " ", match.group(1)).strip(" .,;:").split()
            while words and words[0].lower() in _LEADING_VERBS:
                words.pop(0)
            name = " ".join(words)
            if not name or name.lower() in _NOT_VENDORS:
                continue
            if any(word.lower() in _NOT_VENDORS for word in name.split()):
                continue
            return name
    return ""
































# ==========================================================================
# value normalisation
# ==========================================================================













# ==========================================================================
# briefing + goal
# ==========================================================================

@dataclass
class Node:
    depth: int
    kind: str
    ref: str = ""
    label: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    @property
    def searchable(self) -> str:
        return f"{self.label} {self.raw}".lower()


def parse_nodes(snapshot_text: str) -> list[Node]:
    """Turn formatted snapshot text back into flat nodes."""
    nodes: list[Node] = []
    for raw in (snapshot_text or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("url:", "title:")):
            continue
        depth = (len(raw) - len(raw.lstrip())) // 2
        match = _REF_RE.search(stripped)
        ref = match.group(1) if match else ""
        rest = stripped.replace(f"[{ref}]", "", 1) if ref else stripped
        bits = rest.split(None, 1)
        kind = bits[0] if bits else ""
        tail = bits[1] if len(bits) > 1 else ""

        quoted = re.search(r'"([^"]*)"', tail)
        attrs = {k: v.strip('"') for k, v in
                 re.findall(r"(data-[a-z0-9-]+)=([^\s\"]+)", tail)}
        nodes.append(Node(depth=depth, kind=kind, ref=ref,
                          label=quoted.group(1) if quoted else "",
                          attrs=attrs, raw=raw))
    return nodes


def label_of(node: Node) -> str:
    """A node's printable identity: its quoted label, else its raw tail."""
    if node.label:
        return node.label
    bits = node.raw.strip().split(None, 2)
    return bits[2] if len(bits) > 2 else ""


def _subtree(nodes: list[Node], index: int) -> list[Node]:
    base = nodes[index].depth
    out = [nodes[index]]
    for node in nodes[index + 1:]:
        if node.depth <= base:
            break
        out.append(node)
    return out


def _is_server_error(snapshot_text: str) -> bool:
    """True when the page is a 5xx error rather than the app.

    A transient server failure says nothing about whether the bill is valid, so
    it must not be treated as a rejected form.

    The status code has to appear as a status, never as a bare number: bills
    routinely contain amounts like $14,500.00, and "500" inside an amount is
    not an HTTP error.
    """
    return bool(_SERVER_ERROR_RE.search(snapshot_text or ""))


_SERVER_ERROR_RE = re.compile(
    r"(?:http\s*)?50[0-9]\s*(?:error|service\s+unavailable)\b"
    r"|\b(?:service\s+unavailable|internal\s+server\s+error|bad\s+gateway"
    r"|gateway\s+time-?out|temporarily\s+unavailable)\b",
    re.I,
)


def find_control(snapshot_text: str, *needles: str,
                 kinds: tuple[str, ...] = (),
                 need_ref: bool = True) -> Node | None:
    """First actionable node whose text mentions any needle."""
    kinds = kinds or ("textbox", "button", "link", "combobox", "checkbox", "radio")
    lowered = [n.lower() for n in needles if n]
    for node in parse_nodes(snapshot_text):
        if node.kind not in kinds or (need_ref and not node.ref):
            continue
        if not lowered or any(needle in node.searchable for needle in lowered):
            return node
    return None


def _row_key(node: Node) -> tuple[str, str, str]:
    """Identity of a row that survives re-parsing the snapshot text.

    Rows carry no ref (`<tr>` is not actionable), so object identity is useless
    here: callers parse their own node list. The rendered line is unique.
    """
    return (
        node.attrs.get("data-invoice-number", ""),
        node.attrs.get("data-bill-id", ""),
        node.raw.strip(),
    )


def row_cells(snapshot_text: str, row: Node) -> list[str]:
    """Readable cell strings belonging to a row."""
    nodes = parse_nodes(snapshot_text)
    wanted = _row_key(row)
    for index, node in enumerate(nodes):
        if node.kind == "row" and _row_key(node) == wanted:
            return [label_of(n) for n in _subtree(nodes, index)
                    if n.kind in {"cell", "columnheader"}]
    return []


def find_definition(snapshot_text: str, term: str) -> str:
    """Value text for a `<dl>` term, e.g. 'Payment due' -> '2026-04-11'.

    `<dt>`/`<dd>` pairs are siblings, not nested, so this scans forward for the
    next `definition` at the same or greater depth and stops at the next term.
    """
    nodes = parse_nodes(snapshot_text)
    lowered = term.lower()
    for index, node in enumerate(nodes):
        if node.kind != "term" or lowered not in label_of(node).lower():
            continue
        for follower in nodes[index + 1:]:
            if follower.depth < node.depth:
                break
            if follower.kind == "definition":
                return label_of(follower)
            if follower.kind == "term":
                break
    return ""


def definition_attrs(snapshot_text: str, term: str) -> dict[str, str]:
    nodes = parse_nodes(snapshot_text)
    lowered = term.lower()
    for index, node in enumerate(nodes):
        if node.kind != "term" or lowered not in label_of(node).lower():
            continue
        for follower in nodes[index + 1:]:
            if follower.depth < node.depth:
                break
            if follower.kind == "definition":
                return follower.attrs
            if follower.kind == "term":
                break
    return {}


def error_messages(snapshot_text: str) -> list[str]:
    """Validation errors the page is *actually* showing.

    Structural signals only: an annotated `data-error-for` list item, or an
    `alert` region. Matching on wording would be a trap — a form carries hints
    like "Must be unique per vendor." that read exactly like a real error and
    would make the agent refuse to submit a perfectly good bill.
    """
    nodes = parse_nodes(snapshot_text)
    messages = [
        label_of(node) for node in nodes
        if "data-error-for" in node.attrs and label_of(node)
    ]
    if messages:
        return list(dict.fromkeys(messages))

    # Fall back to the alert banner, minus its generic headline.
    for node in nodes:
        if node.kind != "alert":
            continue
        text = label_of(node)
        if not text or re.fullmatch(r"the bill was not saved\.?", text.strip(), re.I):
            continue
        messages.append(text)
    return list(dict.fromkeys(messages))


def has_save_error(snapshot_text: str) -> bool:
    return bool(error_messages(snapshot_text))


def current_url(snapshot_text: str) -> str:
    for line in (snapshot_text or "").splitlines():
        if line.startswith("url:"):
            return line[4:].strip()
    return ""


def is_login_page(snapshot_text: str) -> bool:
    url = current_url(snapshot_text).lower()
    if "/login" in url:
        return True
    nodes = parse_nodes(snapshot_text)
    has_password = any(n.kind == "textbox" and "type=password" in n.raw for n in nodes)
    has_user = any(n.kind == "textbox" and ("type=email" in n.raw or "type=text" in n.raw)
                   for n in nodes)
    return has_password and has_user


def invoice_number_from_heading(snapshot_text: str) -> str:
    for node in parse_nodes(snapshot_text):
        if node.kind != "heading":
            continue
        match = _INVOICE_NUMBER_RE.search(label_of(node))
        if match:
            return match.group(0)
    return ""


def _value_of(node: Node) -> str:
    match = re.search(r"value=('([^']*)'|\"([^\"]*)\")", node.raw)
    if match:
        return match.group(2) or match.group(3) or ""
    return ""


def _selected_of(node: Node) -> str:
    match = re.search(r"selected=('([^']*)'|\"([^\"]*)\")", node.raw)
    return (match.group(2) or match.group(3) or "") if match else ""


# ==========================================================================
# value normalisation
# ==========================================================================

def _matched_number(match: "re.Match[str] | None") -> str:
    """The number from whichever money alternative matched (symbol or ISO code)."""
    if match is None:
        return ""
    for name in ("a", "b", "c"):
        value = match.groupdict().get(name)
        if value:
            return value
    return ""


def money_to_float(text: str) -> float | None:
    match = _MONEY_IN_TEXT.search(text or "")
    raw = _matched_number(match) if match else re.sub(r"[^0-9.\-]", "", text or "")
    try:
        return float(raw.replace(",", "")) if raw else None
    except ValueError:
        return None


def plain_money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def currency_code(text: str) -> str:
    match = re.search(r"\b(USD|EUR|GBP)\b", text or "")
    return match.group(1) if match else ""


def parse_date_any(text: str) -> str | None:
    """ISO date for whichever date style the page happens to use."""
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", (text or "").strip())
    if not cleaned:
        return None
    patterns = (
        (r"^(\d{4})-(\d{2})-(\d{2})", "ymd"),
        (r"^(\d{4})/(\d{2})/(\d{2})", "ymd"),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})", "mdy"),
        (r"^(\d{4})\.(\d{2})\.(\d{2})", "ymd"),
        (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", "dmy"),
        (r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})", "mon"),
        (r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", "dmon"),
    )
    for pattern, order in patterns:
        match = re.match(pattern, cleaned)
        if not match:
            continue
        try:
            if order == "ymd":
                year, month, day = (int(g) for g in match.groups())
            elif order == "mdy":
                month, day, year = (int(g) for g in match.groups())
            elif order == "dmy":
                day, month, year = (int(g) for g in match.groups())
            elif order == "mon":
                month = MONTHS.index(match.group(1)[:3].lower()) + 1
                day, year = int(match.group(2)), int(match.group(3))
            else:  # dmon
                day = int(match.group(1))
                month = MONTHS.index(match.group(2)[:3].lower()) + 1
                year = int(match.group(3))
        except (ValueError, IndexError):
            continue
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def days_between(later_iso: str, earlier_iso: str) -> int | None:
    try:
        return (date.fromisoformat(later_iso) - date.fromisoformat(earlier_iso)).days
    except (TypeError, ValueError):
        return None


def pdf_total(text: str) -> float | None:
    """Find the invoice total in extracted PDF text.

    Prefers a labelled total over a bare number: a PDF also contains subtotals,
    tax lines, bank details and the supplier's own account number.
    """
    labelled = re.search(
        r"(?:total\s+due|amount\s+due|balance\s+due|invoice\s+total|total)"
        rf"[^\n\d]{{0,28}}(?:{_MONEY_IN_TEXT.pattern})",
        text or "", re.IGNORECASE,
    )
    if labelled:
        found = money_to_float(_matched_number(labelled))
        if found is not None:
            return found
    amounts = [a for a in (money_to_float(m.group(0))
                           for m in _MONEY_IN_TEXT.finditer(text or "")) if a]
    return max(amounts) if amounts else None


# ==========================================================================
# briefing + goal
# ==========================================================================

@dataclass
class Brief:
    portal_url: str = ""
    portal_user: str = ""
    portal_password: str = ""
    ap_url: str = ""
    ap_user: str = ""
    ap_password: str = ""
    today: str = ""
    raw: str = ""


@dataclass
class GoalSpec:
    text: str = ""
    vendor: str = ""
    scope: str = "latest"          # exact | latest | due_window
    invoice: str = ""              # set when the request named one invoice
    days: int = 0
    skip_duplicates: bool = False
    wants_pdf: bool = False
    flag_threshold: float | None = None
    #: Set when the request is not a task this agent can carry out at all. Empty
    #: means proceed. The run stops immediately rather than starting a write
    #: path for something that is not a write request.
    unsupported: str = ""


# ==========================================================================
# the client
# ==========================================================================

class ScriptedClient:
    """Deterministic procedure speaking the model protocol."""

    provider = "scripted"

    def __init__(self, *, model: str = "scripted-policy-engine") -> None:
        self.model = model
        self._ids = itertools.count(1)
        self._init_run_state()

    def _init_run_state(self) -> None:
        self.brief = Brief()
        self.spec = GoalSpec()
        self.stage = "boot"
        self.login_target = ""              # "portal" | "ap"
        self.invoice_rows: list[dict[str, Any]] = []
        self.current: dict[str, Any] = {}
        self.ap_rows: list[dict[str, Any]] = []
        self.ap_searched = False
        self.pending_window: list[dict[str, Any]] = []
        self.skip_list: list[str] = []
        self.entered_bills: list[dict[str, Any]] = []
        self.bill_queue: list[dict[str, Any]] = []
        self.resume_stage = ""
        self.download_retries = 0
        self.render_waits = 0
        self.same_observation = 0
        self.last_signature = ""
        self.max_same_observation = 6
        self.pdf_amount: float | None = None
        self.submit_attempts = 0
        self.approved_fingerprint = ""
        self.asked_choice = False
        self.repairs_seen = 0
        self.notes: list[str] = []
        self.finished = False
        self.planned = False
        self.last_tool = ""

    # -- protocol ---------------------------------------------------------
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AssistantTurn:
        obs, tool_name, tool_args = latest_observation(messages)

        if self.stage == "boot":
            self._parse_brief(messages)
            self._parse_goal()
            self._maybe_repair(obs)

        self.last_tool = tool_name

        # The safety gate blocked a call and named the fingerprint it needs
        # approved. Handle that here rather than in each stage: approval is
        # scoped to one exact call, and the stage that made the call is the one
        # that should retry it once the human has answered.
        blocked = self._blocked_fingerprint(obs)
        if blocked and blocked != self.approved_fingerprint:
            self.approved_fingerprint = blocked
            amount = self.current.get("amount")
            unit = f" ({amount} {self.current.get('currency')})" if amount else ""
            return self._one(
                "The safety gate blocked that call. Asking a human to approve that "
                "exact action.",
                "request_approval",
                action=(
                    f"{tool_name or 'the blocked action'} for invoice "
                    f"{self.current.get('number') or 'the current record'}{unit}"
                ),
                fingerprint=blocked,
                justification=(
                    "Values were read from the vendor portal and cross-checked against "
                    "the downloaded PDF; this write is irreversible and crosses the "
                    "approval threshold."
                ),
            )

        handler = getattr(self, f"_do_{self.stage}", None)
        if handler is None:
            return self._finish(
                "success" if self.current else "blocked",
                self._summary() or "nothing further to do",
            )

        # A page we did not ask for: the session died and the server answered
        # with the login screen. Whatever stage was in flight has to start over
        # from a real session, so log in again and then return to it.
        if (is_login_page(obs) and self.current
                and self.stage not in {"portal_login", "ap_login"}):
            target = self.login_target or "portal"
            self.resume_stage = self.stage
            self.stage = "portal_login" if target == "portal" else "ap_login"
            self.login_attempts = 0
            return self._one(
                f"The session expired and {target} asked me to sign in again.",
                "browser_snapshot",
            )

        # Spin guard. If the same stage keeps seeing the same page, no amount of
        # retrying will help: change approach or stop and say so. Without this
        # the run just burns its whole budget on identical snapshots.
        signature = f"{self.stage}|{tool_name}|{hash(obs)}"
        self.same_observation = (
            self.same_observation + 1 if signature == self.last_signature else 0
        )
        self.last_signature = signature
        if self.same_observation >= self.max_same_observation:
            self.same_observation = 0
            return self._finish(
                "failed",
                f"Stuck on the {self.current.get('number') or 'current'} page: the same "
                f"view ({tool_name or 'read'}) kept returning the same result "
                f"{self.max_same_observation + 1} times with no progress, so I stopped "
                f"rather than keep retrying.",
                values={"bills": [], "extracted": self._extracted()},
            )

        return handler(obs, tool_name, tool_args)

    def _blocked_fingerprint(self, obs: str) -> str:
        """The fingerprint a blocked call is waiting on, if it said so.

        Approval is scoped to an exact call, so the agent has to ask for the
        fingerprint the gate named rather than one it invented.
        """
        if "BLOCKED:" not in (obs or "") or "needs approval" not in (obs or ""):
            return ""
        match = re.search(r"fingerprint='([^']+)'", obs)
        return match.group(1) if match else ""

    # -- small builders ----------------------------------------------------
    def _call(self, name: str, **args: Any) -> ToolCall:
        return ToolCall(id=f"call_{next(self._ids)}", name=name, input=args)

    def _turn(self, content: str, *calls: ToolCall) -> AssistantTurn:
        return AssistantTurn(content=content, tool_calls=list(calls))

    def _one(self, content: str, name: str, **args: Any) -> AssistantTurn:
        return self._turn(content, self._call(name, **args))

    # -- briefing / goal ---------------------------------------------------
    def _parse_brief(self, messages: list[Message]) -> None:
        first = next((m for m in messages if m.role == "user" and not m.tool_use_id), None)
        text = str(first.content) if first else ""
        self.brief.raw = text
        match = re.search(r"today\s+is\s+(\d{4}-\d{2}-\d{2})", text, re.I)
        self.brief.today = match.group(1) if match else ""

        # One bullet per system, so match on the bullet's own text. Matching on a
        # whole block would let a later section ("## Task: enter into the vendor
        # portal...") decide which system a bullet refers to.
        for line in text.splitlines():
            stripped = line.lstrip(" \t-*")
            urls = _URL_RE.findall(stripped)
            if not urls:
                continue
            url = urls[0].rstrip(".,")
            creds = _CRED_RE.search(stripped)
            name = stripped.split("**")[1].lower() if stripped.count("**") >= 2 \
                else stripped[:40].lower()
            if "portal" in name or "vendor" in name:
                self.brief.portal_url = url
                if creds:
                    self.brief.portal_user, self.brief.portal_password = (
                        creds.group(1).rstrip(".,;"), creds.group(2).rstrip(".,;")
                    )
            elif "ap" in name or "payable" in name or "ledger" in name:
                self.brief.ap_url = url
                if creds:
                    self.brief.ap_user, self.brief.ap_password = (
                        creds.group(1).rstrip(".,;"), creds.group(2).rstrip(".,;")
                    )

        if not self.brief.today:
            self.brief.today = date.today().isoformat()

    def _parse_goal(self) -> None:
        text = self.brief.raw
        match = re.search(r"(?:^|\n)##?\s*(?:Goal|Task)\s*\n+(.+)", text)
        self._parse_goal_text((match.group(1) if match else text).strip()[:800])

    def _parse_goal_text(self, goal: str) -> None:
        self.spec.text = goal

        self.spec.vendor = _extract_vendor(goal)

        # A number named in the request wins over any implied scope: if someone
        # says "invoice HL-2260", that is the one they mean.
        named = _INVOICE_NO_RE.search(goal)
        if named:
            self.spec.invoice = named.group(1).replace("\u2010", "-")
            self.spec.scope = "exact"

        days = _DAYS_RE.search(goal)
        if not self.spec.invoice:
            if days:
                self.spec.scope, self.spec.days = "due_window", int(days.group(1))
            elif self._asks_for_all_invoices(goal):
                self.spec.scope = "all_payable"
            elif re.search(r"\b(latest|most recent|newest)\b", goal, re.I):
                self.spec.scope = "latest"

        self.spec.skip_duplicates = bool(re.search(
            # "skip" has to match its own inflections: `skip\b` does not match
            # "skipping", and "skipping any already entered" is the phrasing this
            # project ships in its own README. Missing it meant the agent
            # proposed entering an invoice that was already in AP.
            r"already\s+(?:been\s+)?entered|already\s+(?:in|present)|"
            r"not already|if not already|has ?n[o']t been entered|"
            r"\bskip\w*\b|only if|don'?t (?:create|enter)|do not (?:create|enter)|"
            r"avoid\s+duplicates?|no\s+duplicates?|"
            r"(?:are|is|that'?s|which\s+is)\s+already",
            goal, re.I,
        ))
        self.spec.wants_pdf = bool(re.search(r"\bpdf\b|\bdownload\b", goal, re.I))
        amounts = [m.group("sym") or m.group("code") for m in _MONEY_RE.finditer(goal)]
        if amounts and re.search(r"over|above|exceed|more than|threshold", goal, re.I):
            self.spec.flag_threshold = float(amounts[0].replace(",", ""))

        # Refuse before acting. These are cheap string checks, but they stop the
        # agent wandering into a vendor page and proposing a write for a request
        # that was never a request. The failure they prevent is the expensive
        # one: a wrong bill written behind a plausible-looking approval prompt.
        if not _TASK_OBJECT.search(goal):
            self.spec.unsupported = (
                "The request is not about the vendor portal or the AP system, so "
                "there is nothing for me to do."
            )
        elif _DESTRUCTIVE.search(goal) and not re.search(
            r"\b(enter|create|add|record)\b", goal, re.I
        ):
            self.spec.unsupported = (
                "The request asks me to remove or delete something. I can only "
                "enter bills, not delete them, so I have not touched anything."
            )
        elif not _TASK_VERB.search(goal):
            self.spec.unsupported = (
                "The request does not ask me to do anything I can perform "
                "(enter a bill, or check whether one already exists)."
            )

    # -- boot --------------------------------------------------------------
    def _do_boot(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        # Refuse before opening a browser at all. A run that stops here costs one
        # step and cannot possibly have written anything.
        if self.spec.unsupported:
            return self._finish("blocked", self.spec.unsupported)
        if self.planned:
            self.stage = "portal_login"
            return self._one("Opening the vendor portal.", "browser_open",
                             url=self.brief.portal_url)
        self.planned = True
        vendor = self.spec.vendor or "the requested vendor"
        if self.spec.scope == "due_window":
            locate = (f"List every vendor in the portal and collect their payable "
                      f"invoices due within {self.spec.days} days of {self.brief.today}.")
        else:
            locate = f"Find {vendor} and list their invoices."
        return self._turn(
            "Planning first, then opening the portal.",
            self._call(
                "update_plan",
                steps=[
                    "Open the vendor portal and sign in.",
                    locate,
                    "Open the invoice detail page and read amount, currency and due date.",
                    "Download the invoice PDF and confirm the amount agrees.",
                    "Open the AP system, sign in, and check the invoice is not already "
                    "entered.",
                    "Enter the bill in AP.",
                    "Re-read the saved bill and report the outcome honestly.",
                ],
                criteria=[
                    {"id": "c1",
                     "description": "The invoice was identified from the vendor portal, "
                                    "ignoring draft and void rows.",
                     "check": "generic"},
                    {"id": "c2",
                     "description": "The portal page amount and the downloaded PDF amount "
                                    "agree.",
                     "check": "source_values_match"},
                    {"id": "c3",
                     "description": "No bill exists in AP for an invoice number that was "
                                    "already entered.",
                     "check": "no_duplicate_bill"},
                    {"id": "c4",
                     "description": "If a bill was created, AP shows exactly one bill for "
                                    "that invoice number with the expected amount, "
                                    "currency and due date.",
                     "check": "bill_field"},
                ],
                notes="Draft and void invoices are never payable. Dates must be entered "
                      "into AP as YYYY-MM-DD.",
            ),
        )

    # -- login (shared by both systems) -------------------------------------
    def _login_body(self, target: str, obs: str) -> AssistantTurn:
        """Sign in by reading the form's own state, not by remembering a step.

        The order below is derived entirely from the rendered page — username
        empty, then password empty, then submit. That keeps it correct after a
        session-expiry redirect drops the agent back here mid-run.
        """
        self.login_target = target
        user = (self.brief.portal_user if target == "portal" else self.brief.ap_user)
        password = (self.brief.portal_password if target == "portal"
                    else self.brief.ap_password)
        verb = "the vendor portal" if target == "portal" else "the AP system"

        if not is_login_page(obs):
            self.login_attempts = 0
            if self.resume_stage:
                # Coming back from an expired session: go where we were going
                # rather than restarting the whole task from the directory.
                stage, self.resume_stage = self.resume_stage, ""
                self.stage = stage
                url = (self._portal_invoice_url(self.current) if target == "portal"
                       and stage != "download_pdf" else None)
                if url:
                    return self._turn(
                        f"Back in the {verb} session; returning to the invoice.",
                        self._call("browser_open", url=url),
                    )
                if target == "portal" and stage == "download_pdf":
                    self.stage = "read_detail"
                    return self._turn(
                        f"Back in the {verb} session; returning to the invoice.",
                        self._call("browser_open",
                                   url=self._portal_invoice_url(self.current)),
                    )
                return self._turn(f"Back in the {verb} session; carrying on.",
                                  self._call("browser_snapshot"))
            self.stage = "find_vendor" if target == "portal" else "ap_check"
            return self._turn(f"Already signed in to {verb}; carrying on.",
                              self._call("browser_snapshot"))

        username_node = find_control(obs, "email", "username", "work email",
                                     kinds=("textbox",))
        password_node = find_control(obs, "password", kinds=("textbox",))

        if username_node and username_node.ref and not _value_of(username_node).strip():
            return self._one(f"Entering the {verb} username.", "browser_type",
                             ref=username_node.ref, text=user)

        if password_node and password_node.ref and not _value_of(password_node).strip():
            return self._one(f"Entering the {verb} password.", "browser_type",
                             ref=password_node.ref, text=password)

        button = find_control(obs, "sign in", "log in", kinds=("button",))
        if button and button.ref:
            self.login_attempts = getattr(self, "login_attempts", 0) + 1
            if self.login_attempts > 3:
                return self._finish(
                    "blocked",
                    f"Could not sign in to {verb} after {self.login_attempts} attempts. "
                    f"{_notice_text(obs) or 'The sign-in form kept rejecting the credentials.'}",
                )
            return self._one(f"Submitting the {verb} sign-in.", "browser_click",
                             ref=button.ref)

        return self._one(f"Looking for the {verb} sign-in form.", "browser_snapshot")

    def _do_portal_login(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        return self._login_body("portal", obs)

    def _do_ap_login(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if not self.brief.ap_url:
            return self._finish("blocked", "No AP system URL was in the briefing.")
        return self._login_body("ap", obs)

    # -- finding the vendor -------------------------------------------------
    def _do_find_vendor(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if not self.brief.portal_url:
            return self._finish("blocked", "No vendor portal URL was in the briefing.")
        if is_login_page(obs) or "/login" in current_url(obs).lower():
            self.stage, self.login_target = "portal_login", "portal"
            return self._login_body(self.login_target or "portal", obs)

        search = find_control(obs, "search", kinds=("textbox",))
        if self.spec.vendor and search and search.ref:
            self.stage = "pick_vendor"
            return self._one(f"Searching for {self.spec.vendor}.", "browser_type",
                             ref=search.ref, text=self.spec.vendor, submit=True)

        if not self.spec.vendor:
            # No vendor named: the goal must be the due-window sweep, so read the
            # whole directory from the listing page.
            self.stage = "pick_vendor"
            return self._one("Listing all vendors.", "browser_snapshot")

        return self._one("Going back to the vendor directory.", "browser_open",
                         url=f"{self.brief.portal_url}/vendors")

    def _do_pick_vendor(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if re.search(r"no vendors matched|no invoices", obs, re.I):
            return self._finish(
                "blocked",
                f"No vendor matching {self.spec.vendor!r} exists in the portal, so there "
                f"is nothing to enter into AP.",
            )

        candidates = vendor_links(obs)
        if not candidates:
            return self._one("The vendor list did not load; retrying.", "browser_snapshot")

        if self.spec.vendor:
            if len(candidates) > 1:
                if not self.asked_choice:
                    self.asked_choice = True
                    return self._one(
                        f"Several vendors match {self.spec.vendor!r}; asking which one.",
                        "ask_user",
                        question=f"Which vendor did you mean by {self.spec.vendor!r}?",
                        options=[c["label"] for c in candidates],
                        context="More than one vendor in the portal matches that name.",
                    )
                choice = parse_user_choice(obs)
                chosen = match_option(candidates, choice) or (
                    candidates[0] if len(candidates) == 1 else None
                )
                if chosen is None:
                    return self._finish(
                        "blocked",
                        f"Could not tell which vendor {self.spec.vendor!r} refers to "
                        f"(candidates: {', '.join(c['label'] for c in candidates)}).",
                    )
                self.spec.vendor = chosen["label"]
                self.stage = "read_invoices"
                return self._one(f"Opening {chosen['label']}.", "browser_click",
                                 ref=chosen["ref"])
            chosen = candidates[0]
            self.spec.vendor = chosen["label"]
            self.stage = "read_invoices"
            return self._one(f"Opening {chosen['label']}.", "browser_click",
                             ref=chosen["ref"])

        # A sweep is only legitimate if the request asked for one. With no vendor
        # named and no sweep scope, the task is not identifiable — "enter the
        # latest invoice" with no vendor, or a sentence that is not a task at all.
        # Falling through to the sweep here silently entered whichever vendor
        # sorted first, which is how you write the wrong bill.
        if self.spec.scope in {"due_window", "all_payable"}:
            self.window_queue = candidates
            self.stage = "scan_next_vendor"
            return self._do_scan_next_vendor(obs, tool_name, args)

        if not self.asked_choice:
            self.asked_choice = True
            return self._one(
                "The request names no vendor, so asking which one to use.",
                "ask_user",
                question="Which vendor should I take the invoice from?",
                options=[c["label"] for c in candidates],
                context="The request did not name a vendor, and it is not a "
                        "request to sweep every vendor, so guessing would risk "
                        "entering the wrong bill.",
            )
        choice = parse_user_choice(obs)
        chosen = match_option(candidates, choice)
        if chosen is None:
            return self._finish(
                "blocked",
                f"Could not tell which vendor {choice!r} refers to "
                f"(candidates: {', '.join(c['label'] for c in candidates)}).",
            )
        self.spec.vendor = chosen["label"]
        self.stage = "read_invoices"
        return self._one(f"Opening {chosen['label']}.", "browser_click",
                         ref=chosen["ref"])

    # -- reading a vendor's invoice list ------------------------------------
    def _do_read_invoices(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if is_login_page(obs):
            self.stage, self.login_target = "portal_login", "portal"
            return self._login_body(self.login_target or "portal", obs)
        if rows_pending(obs):
            self.render_waits += 1
            if self.render_waits > 8:
                return self._finish(
                    "blocked",
                    f"The invoice list for {self.spec.vendor} never rendered: the table "
                    f"header loaded but no invoice rows appeared after "
                    f"{self.render_waits} waits, so nothing could be selected.",
                )
            # Wait on a row, not the column heading: the heading is present
            # while the body is still empty.
            return self._one("The invoice table is still rendering; waiting for it.",
                             "browser_wait_for", target="tr[data-invoice-number]",
                             timeout_ms=10000)
        self.render_waits = 0

        rows = invoice_rows(obs)
        if rows:
            self.invoice_rows = rows
        nxt = find_control(obs, "next", kinds=("link",))
        if nxt and nxt.ref:
            return self._one("Collecting the rest of the list.", "browser_click",
                             ref=nxt.ref)
        if not self.invoice_rows:
            return self._finish("blocked", "This vendor has no invoices in the portal.")
        return self._select_invoice()

    def _select_invoice(self) -> AssistantTurn:
        payable = [r for r in self.invoice_rows if r["status"] not in {"draft", "void"}]

        if self.spec.scope == "exact":
            rows = [r for r in self.invoice_rows if r["number"] == self.spec.invoice]
            if not rows:
                return self._finish(
                    "blocked",
                    f"The vendor portal lists no invoice {self.spec.invoice} for "
                    f"{self.spec.vendor}, so nothing was entered.",
                )
            row = rows[0]
            if row["status"] in {"draft", "void"}:
                return self._finish(
                    "blocked",
                    f"Invoice {self.spec.invoice} is marked {row['status']}, so it is "
                    "not payable and was not entered.",
                )
            self.current = dict(row, vendor=self.spec.vendor)
            self.stage = "read_detail"
            return self._one(f"Opening the requested invoice {row['number']}.",
                             "browser_click", ref=row["ref"])

        if not payable:
            if self.spec.scope == "due_window":
                return self._scan_result_empty()
            return self._finish(
                "blocked",
                f"Every invoice from {self.spec.vendor} is a draft or void, so there is "
                f"nothing payable to enter.",
            )

        if self.spec.scope == "all_payable":
            # Every payable invoice from this vendor, oldest first. Duplicates
            # are still filtered per-invoice against AP, not guessed at here.
            queue = sorted(payable, key=lambda r: (r["due"] or "", r["number"]))
            self.bill_queue = queue[1:]
            self.window_matches = queue
            self.current = dict(queue[0], vendor=self.spec.vendor)
            self.stage = "read_detail"
            return self._one(
                f"{len(queue)} payable invoice(s) to review; starting with "
                f"{queue[0]['number']}, due {queue[0]['due']}.",
                "browser_click", ref=queue[0]["ref"],
            )

        if self.spec.scope == "latest":
            target = max(payable, key=lambda r: (r["issued"] or "", r["number"]))
            self.current = dict(target, vendor=self.spec.vendor)
            self.stage = "read_detail"
            return self._one(f"Opening {target['number']} (issued {target['issued']}).",
                             "browser_click", ref=target["ref"])

        window: list[dict[str, Any]] = []
        for row in payable:
            delta = days_between(row["due"] or "", self.brief.today)
            if delta is not None and 0 <= delta <= self.spec.days:
                row = dict(row, vendor=self.spec.vendor, due_in_days=delta)
                window.append(row)
        window.sort(key=lambda r: r["due"] or "")
        if not window:
            return self._scan_result_empty()
        self.window_matches = window
        self.current = dict(window[0])
        self.stage = "read_detail"
        return self._one(
            f"Opening {window[0]['number']}, due {window[0]['due']} "
            f"({window[0]['due_in_days']} days away).",
            "browser_click", ref=window[0]["ref"],
        )

    # -- due-window sweep ---------------------------------------------------
    def _do_scan_next_vendor(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        queue = getattr(self, "window_queue", [])
        if not queue:
            return self._finish("success", self._sweep_summary())
        candidate = queue.pop(0)
        self.stage = "read_invoices"
        return self._one(f"Checking {candidate['label']}.", "browser_click",
                         ref=candidate["ref"])

    def _scan_result_empty(self) -> AssistantTurn:
        """No match for this vendor in the sweep; try the next one."""
        return self._do_scan_next_vendor("", "", {})

    def _sweep_summary(self) -> str:
        entered = [r.get("number") for r in self.entered_bills]
        parts = []
        if entered:
            parts.append("Entered into AP: " + ", ".join(str(n) for n in entered) + ".")
        else:
            parts.append("No new invoice needed entering into AP.")
        if self.skip_list:
            parts.append(
                "Already present in AP and therefore left alone: "
                + ", ".join(self.skip_list) + "."
            )
        if self.spec.scope == "due_window" and not entered:
            parts.append(
                f"Every payable invoice within the next {self.spec.days} days "
                f"(today is {self.brief.today}) was already in AP."
            )
        return " ".join(parts)

    # -- invoice detail -----------------------------------------------------
    def _do_read_detail(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if is_login_page(obs):
            self.stage, self.login_target = "portal_login", "portal"
            return self._login_body(self.login_target or "portal", obs)

        attrs = definition_attrs(obs, "Amount")
        due_attrs = definition_attrs(obs, "Payment due")
        number = invoice_number_from_heading(obs) or self.current.get("number", "")

        amount = attrs.get("data-amount")
        if amount in (None, ""):
            amount = money_to_float(find_definition(obs, "Amount"))
        due = due_attrs.get("data-due-iso") or parse_date_any(
            find_definition(obs, "Payment due")
        )
        if not number or amount is None or not due:
            return self._one(
                "The detail page did not show a readable amount and due date; "
                "taking a fresh snapshot.",
                "browser_snapshot",
            )

        currency = attrs.get("data-currency") or currency_code(find_definition(obs, "Amount"))
        self.current.update({
            "number": number,
            "amount": float(amount),
            "currency": currency or self.current.get("currency") or "USD",
            "due": due,
            "vendor": self.spec.vendor or self.current.get("vendor", ""),
        })
        self.notes.append(
            f"invoice {number}: {self.current['amount']} {self.current['currency']} "
            f"due {due} (from the portal detail page)"
        )
        self.stage = "download_pdf"
        return self._turn(
            f"Read {number}: {self.current['amount']} {self.current['currency']}, "
            f"due {due}. Recording it with its source.",
            self._call(
                "remember", key=f"invoice:{number}",
                value={"amount": self.current["amount"], "currency": self.current["currency"],
                       "due": due, "vendor": self.current["vendor"]},
                source=f"vendor portal invoice {number} detail page",
            ),
        )

    def _do_download_pdf(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        link = find_control(obs, "download", "pdf", kinds=("link", "button"))
        if not link or not link.ref:
            return self._one("Looking for the invoice PDF link.", "browser_snapshot")
        self.stage = "extract_pdf"
        return self._one("Downloading the invoice PDF.", "browser_download", ref=link.ref)

    def _do_extract_pdf(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        path = pdf_path_from(obs)
        if not path:
            self.download_retries += 1
            if self.download_retries > 3:
                return self._finish(
                    "failed",
                    f"The invoice PDF for {self.current.get('number')} could not be "
                    f"downloaded after {self.download_retries} attempts, so the amount "
                    f"could not be confirmed.",
                    values={"bills": [], "extracted": self._extracted()},
                )
            self.stage = "download_pdf"
            return self._one("The download produced no file; trying the link again.",
                             "browser_snapshot")
        self.download_retries = 0
        self.stage = "check_pdf"
        return self._one("Reading the PDF text.", "extract_pdf_text", path=path)

    def _do_check_pdf(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if getattr(self, "awaiting_amount_choice", False):
            return self._apply_amount_choice(obs)
        if tool_name != "extract_pdf_text":
            self.stage = "extract_pdf"
            return self._do_extract_pdf(obs, tool_name, args)

        total = pdf_total(obs)
        if total is None:
            return self._finish(
                "blocked",
                f"Could not find an invoice total in the PDF for "
                f"{self.current.get('number')}, so the amount could not be confirmed.",
            )
        page_amount = float(self.current.get("amount") or 0)
        self.pdf_amount = total
        if abs(total - page_amount) <= 0.01:
            self.notes.append(f"PDF total {total} matches the page amount {page_amount}")
            self.stage = "ap_login"
            return self._one("Amounts agree. Opening AP.", "browser_open",
                             url=self.brief.ap_url)

        self.awaiting_amount_choice = True
        return self._one(
            f"The page says {page_amount} but the PDF says {total}; asking the user.",
            "ask_user",
            question=(
                f"Invoice {self.current.get('number')} shows {page_amount} on the portal "
                f"page but {total} in the downloaded PDF. Which amount should be entered "
                f"into AP?"
            ),
            options=[f"Use the PDF amount ({total})",
                     f"Use the page amount ({page_amount})",
                     "Skip this invoice"],
            context="The portal states the PDF is the authoritative document.",
        )

    def _asks_for_all_invoices(self, goal: str) -> bool:
        """True when the request is about a set of invoices, not a single one.

        A plural noun alone is not enough ("invoices" turns up in plenty of
        one-off requests), so this also looks for the words that actually
        generalise the ask.

        The noun is deliberately a set: this portal calls an open invoice a
        "payable" as often as an "invoice", and matching only "invoice" made
        "enter every open payable from Globex" fall through to single-invoice
        handling — quietly doing less than the user asked for.
        """
        text = goal.lower()
        noun = r"(?:invoices?|payables?|bills?)"
        if re.search(rf"\b(all|every|each|any)\b[^.]{{0,40}}\b{noun}\b", text):
            return True
        if re.search(rf"\b{noun}\b", text) and re.search(
            r"\b(have|hasn't|has not|aren't|are not|weren't|were not|should be|to be)\b",
            text,
        ):
            return True
        return bool(re.search(r"\binvoice numbers\b", text))

    def _apply_amount_choice(self, obs: str) -> AssistantTurn:
        """Act on the user's answer to the page-vs-PDF conflict."""
        choice = parse_user_choice(obs).lower()
        pdf = self.pdf_amount or 0.0
        page = float(self.current.get("amount") or 0)
        self.awaiting_amount_choice = False

        if "skip" in choice:
            self.skip_list.append(self.current.get("number", ""))
            self.notes.append(f"user chose to skip {self.current.get('number')}")
            self.stage = "scan_vendors"
            return self._do_scan_next_vendor(obs, "", {})

        # The portal calls the PDF authoritative, so PDF wins unless the user
        # explicitly picked the page amount.
        use_pdf = "page amount" not in choice
        amount = pdf if use_pdf else page
        self.current["amount"] = amount
        self.notes.append(
            f"page showed {page}, PDF showed {pdf}; using {amount} on the user's answer"
        )
        self.stage = "ap_login"
        return self._one(f"Using {amount} as instructed. Opening AP.", "browser_open",
                         url=self.brief.ap_url)

    # -- AP system ----------------------------------------------------------
    def _do_ap_check(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if is_login_page(obs):
            self.stage, self.login_target = "ap_login", "ap"
            return self._login_body("ap", obs)

        number = self.current.get("number", "")
        if not self.ap_searched:
            search = find_control(obs, "search", "filter", kinds=("textbox",))
            if search and search.ref:
                self.ap_searched = True
                return self._one(f"Checking AP for {number}.", "browser_type",
                                 ref=search.ref, text=number, submit=True)
        return self._do_ap_check_result(obs)

    def _do_ap_check_result(self, obs: str) -> AssistantTurn:
        number = self.current.get("number", "")
        existing = bill_rows(obs)
        if existing:
            bill_id = existing[0]["bill_id"]
            if self.spec.skip_duplicates:
                self.skip_list.append(number)
                self.notes.append(f"{number} already in AP as bill #{bill_id}; skipped")
                return self._next_window_item(
                    f"{number} was already in AP (bill #{bill_id}), so I did not create "
                    f"a duplicate."
                )
            return self._finish(
                "success",
                f"Invoice {number} was already entered in AP as bill #{bill_id}, so no "
                f"duplicate was created.",
                values={"bills": [], "already_present": True,
                        "extracted": self._extracted()},
            )
        self.stage = "fill_form"
        return self._one(f"{number} is not in AP yet. Opening the new bill form.",
                         "browser_open", url=f"{self.brief.ap_url}/bills/new")

    def _next_window_item(self, reason: str) -> AssistantTurn:
        """After skipping an already-entered invoice in a multi-invoice sweep."""
        if self.spec.scope in {"due_window", "all_payable"}:
            # Pop from the stored queue, not a copy of it: a copy would hand
            # back the same invoice on every call and loop forever.
            if self.spec.scope == "due_window":
                remaining = list(getattr(self, "window_matches", [])[1:])
            else:
                remaining = self.bill_queue
            if remaining:
                self.current = dict(remaining.pop(0))
                self.ap_searched = False
                self.submit_attempts = 0
                self.stage = "read_detail"
                return self._one(
                    f"{reason} Continuing with {self.current['number']}.",
                    "browser_open",
                    url=self._portal_invoice_url(self.current),
                )
            return self._finish("success", self._sweep_summary(), values=self._claimed())
        return self._finish("success", reason, values=self._claimed())

    # -- filling and submitting ---------------------------------------------
    def _do_refill_form(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        """Reopen a clean AP form after the server dropped the last submission."""
        if is_login_page(obs):
            self.stage, self.login_target = "ap_login", "ap"
            return self._login_body("ap", obs)
        # A blank form is a fresh attempt: the previous count described a page
        # that no longer exists, so it must not condemn the new one.
        self.submit_attempts = 0
        self.stage = "fill_form"
        return self._one("Reopening the AP bill form to try again.",
                         "browser_open", url=f"{self.brief.ap_url}/bills/new")

    def _do_fill_form(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if is_login_page(obs):
            self.stage, self.login_target = "ap_login", "ap"
            return self._login_body("ap", obs)

        pending = pending_fields(obs, self.current, self.pdf_amount)
        if pending:
            node, value = pending[0]
            if node.kind == "combobox":
                return self._one(f"Selecting {label_of(node)} = {value}.",
                                 "browser_set_value", ref=node.ref, value=value)
            return self._one(f"Filling {label_of(node)}.", "browser_type",
                             ref=node.ref, text=value)
        return self._do_submit(obs, tool_name, args)

    def _do_submit(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        errors = error_messages(obs)
        if errors:
            return self._finish("blocked", f"AP rejected the bill: {errors[0]}",
                                values={"bills": [], "ap_errors": errors,
                                        "extracted": self._extracted()})

        submit = find_control(obs, "save", "commit", "submit", kinds=("button",))
        if not submit or not submit.ref:
            return self._one("Looking for the submit button.", "browser_snapshot")

        self.submit_attempts += 1
        if self.submit_attempts > 3:
            return self._finish(
                "failed",
                f"The AP form would not submit: {label_of(submit)} did not save the bill "
                f"after {self.submit_attempts} attempts.",
                values={"bills": [], "extracted": self._extracted()},
            )
        self.stage = "saved"
        return self._one("Submitting the bill.", "browser_click", ref=submit.ref)

    def _do_saved(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if is_login_page(obs):
            self.stage, self.login_target = "ap_login", "ap"
            self.submit_attempts -= 1
            return self._login_body("ap", obs)

        errors = error_messages(obs)
        if errors:
            return self._finish("blocked", f"AP rejected the bill: {errors[0]}",
                                values={"bills": [], "ap_errors": errors,
                                        "extracted": self._extracted()})

        # A server-side error is not a rejection of the bill, and the form is
        # gone. Wait a moment, reopen a clean form, and submit again.
        if _is_server_error(obs):
            self.submit_attempts += 1
            if self.submit_attempts > 3:
                return self._finish(
                    "failed",
                    f"AP kept returning a server error while saving invoice "
                    f"{self.current.get('number')} "
                    f"({self.submit_attempts} attempts), so the bill was not entered.",
                    values={"bills": [], "extracted": self._extracted()},
                )
            self.stage = "refill_form"
            return self._one(
                f"AP returned a server error; waiting, then filling the form again.",
                "browser_wait_for", target="Invoice #", timeout_ms=8000,
            )

        if re.search(r"saved successfully", obs, re.I):
            self.stage = "evidence"
            return self._one("The AP system reported success. Capturing evidence.",
                             "browser_screenshot",
                             label=f"ap-bill-{self.current.get('number')}")

        # Still on the form with no confirmation. The usual cause is that the
        # submit click was blocked for approval and the page never changed, so
        # press the button again rather than staring at the same page.
        submit = find_control(obs, "save", "commit", "submit", kinds=("button",))
        if submit and submit.ref and self.submit_attempts < 4:
            self.submit_attempts += 1
            return self._one("Still on the form with no confirmation; pressing Save again.",
                             "browser_click", ref=submit.ref)
        if not submit or not submit.ref:
            return self._one("No confirmation is visible; re-reading the page.",
                             "browser_snapshot")
        return self._finish(
            "failed",
            f"The AP form never confirmed a save for invoice "
            f"{self.current.get('number')}. The page still shows the form after "
            f"{self.submit_attempts} attempts to submit.",
            values={"bills": [], "extracted": self._extracted()},
        )

    def _do_evidence(self, obs: str, tool_name: str, args: dict[str, Any]) -> AssistantTurn:
        if self.current:
            self.entered_bills.append(dict(self.current))
        if self.spec.scope in {"due_window", "all_payable"}:
            return self._next_window_item(
                f"{self.current.get('number')} is saved in AP."
            )
        self.stage = "finish"
        return self._finish("success", self._summary(), values=self._claimed())

    # -- helpers -------------------------------------------------------------
    def _extracted(self) -> list[dict[str, Any]]:
        if not self.current:
            return []
        return [
            {"key": "invoice_number", "value": self.current.get("number"),
             "source": "vendor portal detail page"},
            {"key": "amount", "value": self.current.get("amount"),
             "source": "vendor portal detail page + downloaded PDF"},
            {"key": "currency", "value": self.current.get("currency"),
             "source": "vendor portal detail page"},
            {"key": "due_date", "value": self.current.get("due"),
             "source": "vendor portal detail page"},
        ]

    def _bill_claim(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "invoice_number": row.get("number"),
            "vendor": row.get("vendor"),
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "due_date": row.get("due"),
            "source": "vendor portal invoice detail page, cross-checked against "
                      "the downloaded PDF",
        }

    def _claimed(self) -> dict[str, Any]:
        # Only bills that were actually submitted. The old fallback to
        # `self.current` let a run claim an invoice it had merely *read*, which
        # is precisely the overclaiming the verifier exists to catch — except
        # here the run was overclaiming about itself.
        return {
            "bills": [self._bill_claim(r) for r in self.entered_bills],
            "skipped_already_present": list(self.skip_list),
            "extracted": self._extracted(),
        }

    def _summary(self) -> str:
        if not self.current:
            return ""
        parts = [
            f"Invoice {self.current.get('number')} from {self.current.get('vendor')}: "
            f"{self.current.get('amount')} {self.current.get('currency')}, "
            f"due {self.current.get('due')}"
        ]
        if self.pdf_amount is not None:
            parts.append(f"the downloaded PDF total is {self.pdf_amount}")
        if self.skip_list:
            parts.append("already present in AP and left alone: "
                         + ", ".join(self.skip_list))
        return ". ".join(parts) + "."

    def _finish(self, status: str, summary: str, values: dict[str, Any] | None = None) -> AssistantTurn:
        if self.finished:
            return self._turn(summary or "Already finished.")
        self.finished = True
        return self._turn(
            summary,
            self._call(
                "finish", status=status, summary=summary,
                evidence=self.notes[-6:], values=values or self._claimed(),
            ),
        )

    def _portal_invoice_url(self, row: dict[str, Any]) -> str:
        return f"{self.brief.portal_url}/vendors/{row.get('vendor_id', '')}" \
               f"/invoices/{row.get('number', '')}"

    # -- verifier feedback ---------------------------------------------------
    def _maybe_repair(self, obs: str) -> None:
        if not obs or "independent verification" not in obs.lower():
            return
        if self.finished:
            return
        self.repairs_seen += 1
        self.notes.append(f"verification repair #{self.repairs_seen}: re-entered the bill")
        self.finished = False
        self.submit_attempts = 0
        self.approved_fingerprint = ""
        self.ap_searched = False
        self.stage = "fill_form"


# --------------------------------------------------------------------------
# observation parsing helpers
# --------------------------------------------------------------------------

def latest_observation(messages: list[Message]) -> tuple[str, str, dict[str, Any]]:
    """The most recent tool result, plus the call that produced it."""
    index = max((i for i, m in enumerate(messages)
                 if m.role == "user" and m.tool_use_id), default=-1)
    if index < 0:
        first = next((m for m in messages if m.role == "user"), None)
        return (str(first.content) if first else "", "", {})
    preceding = messages[index - 1]
    calls = preceding.tool_calls or []
    return (str(messages[index].content),
            calls[0].name if calls else "",
            dict(calls[0].input) if calls else {})


def parse_user_choice(obs: str) -> str:
    match = re.search(r"user answered:?\s*\"?([^\"\n]+)\"?", obs or "", re.I)
    return match.group(1).strip() if match else ""


def vendor_links(snapshot_text: str) -> list[dict[str, Any]]:
    """Vendor names on a directory page, each with a ref."""
    out: list[dict[str, Any]] = []
    nodes = parse_nodes(snapshot_text)
    for index, node in enumerate(nodes):
        if node.kind != "row":
            continue
        link = next((n for n in _subtree(nodes, index)
                     if n.kind == "link" and n.ref and n.label), None)
        if not link or not re.search(r'href=/vendors/[^/?"]+', link.raw):
            continue
        href = re.search(r'href=(/vendors/[^/?"]+)', link.raw)
        out.append({
            "label": link.label,
            "ref": link.ref,
            "vendor_id": href.group(1).rsplit("/", 1)[-1] if href else "",
        })
    return out


def rows_pending(snapshot_text: str) -> bool:
    """True when the invoice table exists but its rows have not rendered yet.

    The rows are the only thing that carries ``data-invoice-number``, so their
    absence is the signal. Waiting on the column heading would be useless: the
    heading is already there while the body is still empty, which is exactly the
    state a late-rendering table passes through.
    """
    text = snapshot_text or ""
    if not text:
        return True
    if re.search(r"data-invoice-number=", text):
        return False
    return bool(re.search(r"Invoice #|columnheader", text))


def invoice_rows(snapshot_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    nodes = parse_nodes(snapshot_text)
    for index, node in enumerate(nodes):
        number = node.attrs.get("data-invoice-number") if node.kind == "row" else None
        if not number:
            continue
        cells = row_cells(snapshot_text, node)
        link = next((n for n in _subtree(nodes, index)
                     if n.kind == "link" and n.ref), None)
        href = re.search(r"href=(/vendors/[^/]+/invoices/[^/\s]+)", link.raw) if link else None
        rows.append({
            "number": number,
            "ref": link.ref if link else "",
            "vendor_id": href.group(1).split("/")[2] if href else "",
            "status": node.attrs.get("data-status", ""),
            "issued": parse_date_any(cells[1]) if len(cells) > 1 else None,
            "issued_display": cells[1] if len(cells) > 1 else "",
            "due": parse_date_any(cells[2]) if len(cells) > 2 else None,
            "due_display": cells[2] if len(cells) > 2 else "",
            "amount": money_to_float(cells[3]) if len(cells) > 3 else None,
            "currency": currency_code(cells[3]) if len(cells) > 3 else "",
        })
    return rows


def bill_rows(snapshot_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in parse_nodes(snapshot_text):
        if node.kind != "row" or "data-bill-id" not in node.attrs:
            continue
        cells = row_cells(snapshot_text, node)
        rows.append({
            "bill_id": node.attrs.get("data-bill-id"),
            "number": node.attrs.get("data-invoice-number", ""),
            "vendor": cells[1] if len(cells) > 1 else "",
            "amount": money_to_float(cells[3]) if len(cells) > 3 else None,
        })
    return rows


def match_option(candidates: list[dict[str, Any]], choice: str) -> dict[str, Any] | None:
    lowered = (choice or "").strip().lower()
    if not lowered:
        return None
    for candidate in candidates:
        if candidate["label"].lower() == lowered:
            return candidate
    for candidate in candidates:
        if lowered in candidate["label"].lower():
            return candidate
    return None


def pdf_path_from(obs: str) -> str:
    """Pull the downloaded file's path out of whatever the tool said.

    Tools phrase this differently ("Downloaded X.pdf (2208 bytes) to /abs/path",
    '{"path": "/abs/path"}'), so take the last .pdf token and prefer an absolute
    path when there is one.
    """
    text = obs or ""
    quoted = re.search(r"[\"']((?:/|\./)[^\"']+\.pdf)[\"']", text)
    if quoted:
        return quoted.group(1)
    absolute = re.findall(r"(?:/|\./)[^\s\"',()]+\.pdf", text)
    if absolute:
        return absolute[-1]
    any_pdf = re.findall(r"[^\s\"',()]+\.pdf", text)
    return any_pdf[-1] if any_pdf else ""


def pending_fields(snapshot_text: str, current: dict[str, Any],
                   pdf_amount: float | None = None) -> list[tuple[Node, str]]:
    """Form controls that still need a value, paired with what to put there.

    Matching is on the *visible label*, never on `name` or `id`, so the
    `ui_rename` fault needs no special case. Emptiness is read back out of the
    rendered snapshot rather than tracked locally, so a fill that silently failed
    is retried instead of assumed done.
    """
    amount = current.get("amount")
    wanted: list[tuple[tuple[str, ...], str]] = [
        (("vendor account", "vendor"), current.get("vendor", "")),
        (("invoice number", "document reference"), current.get("number", "")),
        (("amount", "total value"), plain_money(amount)),
        (("due date", "payment due on"), current.get("due", "")),
        (("currency", "currency code"), current.get("currency", "")),
        (("notes", "internal notes"),
         f"Entered by AI Worker from the {current.get('vendor', 'vendor')} portal "
         f"invoice {current.get('number', '')}"
         + (f"; PDF total {pdf_amount}." if pdf_amount else ".")),
    ]

    pending: list[tuple[Node, str]] = []
    for needles, value in wanted:
        if not value:
            continue
        node = find_control(snapshot_text, *needles, kinds=("combobox", "textbox"))
        if node is None:
            continue
        if node.kind == "combobox":
            if _selected_of(node).strip().lower() != value.strip().lower():
                pending.append((node, value))
        elif not _value_of(node).strip():
            pending.append((node, value))
    return pending