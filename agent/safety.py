"""Safety policy: classify every action, then *enforce* the result in code.

Two ideas do the work here.

1. **Risk is per-call, not per-tool.** `browser_click` is a read when it opens a
   detail page and an irreversible write when it presses Save. So classification
   refines the static tool risk by inspecting the arguments *and* the current
   page snapshot (what is under that ref?).

2. **Enforcement lives in the executor, not the prompt.** The model can be
   persuaded to skip an approval; it cannot talk the executor out of one. If a
   rule fires and no grant exists for this exact call fingerprint, the call is
   refused before it touches the browser.

Policy thresholds live in `config/policy.yaml` so they are configurable without
touching code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.tools.base import RISK_ORDER, Risk, ToolRegistry, ToolSpec
from common.logging_setup import get_logger

log = get_logger(__name__)


class RuleName(str, Enum):
    IRREVERSIBLE_WRITE = "irreversible_write"
    AMOUNT_OVER_THRESHOLD = "amount_over_threshold"
    VENDOR_AMBIGUITY = "vendor_ambiguity"
    DUPLICATE_SUSPECTED = "duplicate_suspected"
    LOW_CONFIDENCE_EXTRACTION = "low_confidence_extraction"
    UNVERIFIED_SOURCE = "unverified_source"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


#: Text on the page that means "pressing this commits something".
WRITE_VERB_PATTERN = re.compile(
    r"\b(save|submit|commit|post|create|add|confirm|approve|void|cancel|delete|remove|"
    r"dispute|pay|send|finali[sz]e|record)\b",
    re.IGNORECASE,
)

#: Text that reads like navigation, even though it contains a verb.
READ_VERB_PATTERN = re.compile(
    r"\b(view|show|open|details?|invoice[s]?|list|search|filter|sort|download|back|next|"
    r"previous|expand|see)\b",
    re.IGNORECASE,
)

#: Payment / financial verbs that always imply an irreversible write.
HIGH_CONSEQUENCE_PATTERN = re.compile(
    r"\b(pay|payment|disburse|transfer|wire|void|refund|charge)\b", re.IGNORECASE
)

AMOUNT_KEYS = ("amount", "value", "total", "total_value", "price", "cost", "sum")
CURRENCY_KEYS = ("currency", "ccy", "ccy_code", "currency_code")

#: Tools that must never be gated, because gating them blocks the only route to
#: an approval. `request_approval` reaching a human is never itself privileged.
APPROVAL_EXEMPT_TOOLS = frozenset({"request_approval"})
VENDOR_KEYS = ("vendor", "vendor_name", "vendor_account", "payee", "supplier", "customer")
INVOICE_KEYS = ("invoice_number", "invoice", "document_ref", "reference", "invoice_no")


def call_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """Stable identity for an approval grant.

    Approving "save bill vendor=Acme amount=4810" must not silently approve the
    next different save.
    """
    payload = _canonical(tool, args)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _canonical(tool: str, args: dict[str, Any]) -> str:
    def norm(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: norm(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, float):
            return f"{value:.2f}"
        if isinstance(value, int):
            return str(value)
        return value

    import json

    return json.dumps({"tool": tool, "args": norm(args)}, sort_keys=True, default=str)


@dataclass
class Rule:
    name: RuleName
    description: str
    risk: Risk = Risk.IRREVERSIBLE_WRITE
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Outcome of classifying one tool call."""

    tool: str
    risk: Risk
    base_risk: Risk
    reasons: list[str] = field(default_factory=list)
    rules_fired: list[RuleName] = field(default_factory=list)
    requires_approval: bool = False
    action_description: str = ""
    risk_reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def is_write(self) -> bool:
        return RISK_ORDER[self.risk] >= RISK_ORDER[Risk.REVERSIBLE_WRITE]


@dataclass
class ApprovalGrant:
    fingerprint: str
    tool: str
    action_description: str
    granted_by: str = "user"
    reason: str = ""


class ApprovalStore:
    """Grants, scoped to a call fingerprint, with a bounded lifetime."""

    def __init__(self, *, max_grants: int = 20) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._max = max_grants
        self.consumed: set[str] = set()

    def grant(self, fingerprint: str, tool: str, description: str,
              granted_by: str = "user", reason: str = "") -> ApprovalGrant:
        approval = ApprovalGrant(
            fingerprint=fingerprint, tool=tool, action_description=description,
            granted_by=granted_by, reason=reason,
        )
        self._grants[fingerprint] = approval
        if len(self._grants) > self._max:
            oldest = next(iter(self._grants))
            self._grants.pop(oldest, None)
        log.info("approval granted for %s (%s)", fingerprint, description)
        return approval

    def deny(self, fingerprint: str, reason: str = "") -> None:
        self._grants.pop(fingerprint, None)
        self.consumed.add(fingerprint)
        log.info("approval denied for %s: %s", fingerprint, reason or "no reason given")

    def has(self, fingerprint: str) -> bool:
        return fingerprint in self._grants and fingerprint not in self.consumed

    def get(self, fingerprint: str) -> ApprovalGrant | None:
        return self._grants.get(fingerprint)

    def clear(self) -> None:
        self._grants.clear()
        self.consumed.clear()

    def __len__(self) -> int:
        return len(self._grants)


class Policy:
    """Configurable rule set + call classifier."""

    def __init__(self, config: dict[str, Any] | None = None,
                 registry: ToolRegistry | None = None) -> None:
        self.config = config or {}
        self.registry = registry
        approvals_cfg = self.config.get("approvals", {}) or {}
        self.require_approval_for_irreversible: bool = bool(
            approvals_cfg.get("require_for_irreversible_write", True)
        )
        self.auto_approve_reads: bool = bool(approvals_cfg.get("auto_approve_reads", True))
        self.thresholds: dict[str, Any] = self.config.get("thresholds", {}) or {}
        self.rules: dict[RuleName, Rule] = {}
        self._load_rules(approvals_cfg.get("rules", {}) or {})

    def _load_rules(self, overrides: dict[str, Any]) -> None:
        defaults = {
            RuleName.IRREVERSIBLE_WRITE: Rule(
                RuleName.IRREVERSIBLE_WRITE,
                "Any irreversible write requires explicit approval.",
                Risk.IRREVERSIBLE_WRITE,
            ),
            RuleName.AMOUNT_OVER_THRESHOLD: Rule(
                RuleName.AMOUNT_OVER_THRESHOLD,
                "Financial amounts above the configured threshold require approval.",
                Risk.IRREVERSIBLE_WRITE,
            ),
            RuleName.VENDOR_AMBIGUITY: Rule(
                RuleName.VENDOR_AMBIGUITY,
                "More than one candidate entity matched; a human must pick.",
                Risk.IRREVERSIBLE_WRITE,
            ),
            RuleName.DUPLICATE_SUSPECTED: Rule(
                RuleName.DUPLICATE_SUSPECTED,
                "A record with the same key may already exist.",
                Risk.IRREVERSIBLE_WRITE,
            ),
            RuleName.LOW_CONFIDENCE_EXTRACTION: Rule(
                RuleName.LOW_CONFIDENCE_EXTRACTION,
                "A value was extracted with low confidence or from one source only.",
                Risk.IRREVERSIBLE_WRITE,
            ),
            RuleName.UNVERIFIED_SOURCE: Rule(
                RuleName.UNVERIFIED_SOURCE,
                "A value has no recorded provenance.",
                Risk.REVERSIBLE_WRITE,
            ),
            RuleName.EXTERNAL_SIDE_EFFECT: Rule(
                RuleName.EXTERNAL_SIDE_EFFECT,
                "The action has an effect outside the systems being read.",
                Risk.IRREVERSIBLE_WRITE,
            ),
        }
        for name, rule in defaults.items():
            override = overrides.get(name.value, {})
            rule.enabled = bool(override.get("enabled", rule.enabled))
            rule.params = {**rule.params, **(override.get("params", {}) or {})}
            rule.risk = Risk(override.get("risk", rule.risk.value))
            self.rules[name] = rule

    # -- classification ---------------------------------------------------
    def classify(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        snapshot_text: str = "",
        snapshot: Any = None,
    ) -> Decision:
        spec = self.registry.get(tool) if self.registry and self.registry.has(tool) else None
        base_risk = spec.risk if spec else Risk.READ
        risk = base_risk
        reasons: list[str] = []
        rules: list[RuleName] = []

        decision = Decision(
            tool=tool, risk=base_risk, base_risk=base_risk,
            fingerprint=call_fingerprint(tool, args),
            payload=dict(args),
        )

        # 1. Refine generic browser actions by what is actually under the ref.
        element_text = self._element_text(args.get("ref"), snapshot)
        if tool in {"browser_click", "browser_type", "browser_select", "browser_press"}:
            if element_text:
                decision.action_description = f"{tool} on {element_text!r}"
                if HIGH_CONSEQUENCE_PATTERN.search(element_text):
                    risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                    reasons.append(f"element text {element_text!r} implies a financial action")
                elif WRITE_VERB_PATTERN.search(element_text) and not READ_VERB_PATTERN.search(
                    element_text
                ):
                    risk = max(risk, Risk.REVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                    reasons.append(f"element text {element_text!r} implies a state change")
                if WRITE_VERB_PATTERN.search(element_text):
                    risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                    reasons.append(f"element text {element_text!r} matches a commit verb")
            else:
                decision.action_description = tool
        else:
            decision.action_description = spec.description.split("\n")[0] if spec else tool

        # 2. Domain rules on the arguments themselves.
        if _has_any(args, AMOUNT_KEYS):
            amount = _first_value(args, AMOUNT_KEYS)
            currency = _first_value(args, CURRENCY_KEYS) or "USD"
            threshold_cfg = self.thresholds.get("amount", {}) or {}
            threshold = float(threshold_cfg.get("value", 10000.0))
            only_currency = (threshold_cfg.get("currency") or "USD")
            parsed = _to_float(amount)
            reasons.append(f"amount={amount} {currency}")
            if (
                parsed is not None
                and parsed > threshold
                and str(currency).upper() == str(only_currency).upper()
                and self.rules[RuleName.AMOUNT_OVER_THRESHOLD].enabled
            ):
                rules.append(RuleName.AMOUNT_OVER_THRESHOLD)
                risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                reasons.append(
                    f"amount {parsed:,.2f} {currency} exceeds approval threshold "
                    f"{threshold:,.2f} {only_currency}"
                )

        if _looks_like_ambiguity(args):
            if self.rules[RuleName.VENDOR_AMBIGUITY].enabled:
                rules.append(RuleName.VENDOR_AMBIGUITY)
                risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                reasons.append("more than one candidate vendor was supplied")

        if _looks_like_duplicate(args):
            if self.rules[RuleName.DUPLICATE_SUSPECTED].enabled:
                rules.append(RuleName.DUPLICATE_SUSPECTED)
                risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                reasons.append("a record with the same key may already exist")

        confidence = args.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < 0.75 and self.rules[
                    RuleName.LOW_CONFIDENCE_EXTRACTION
                ].enabled:
                    rules.append(RuleName.LOW_CONFIDENCE_EXTRACTION)
                    risk = max(risk, Risk.REVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
                    reasons.append(f"extraction confidence {confidence} below 0.75")
            except (TypeError, ValueError):
                pass

        if not str(args.get("source", "")).strip() and _touches_persisted_value(args):
            if self.rules[RuleName.UNVERIFIED_SOURCE].enabled:
                rules.append(RuleName.UNVERIFIED_SOURCE)
                reasons.append("value has no recorded source/provenance")

        if tool == "http_request" and str(args.get("method", "GET")).upper() not in {"GET", "HEAD"}:
            rules.append(RuleName.EXTERNAL_SIDE_EFFECT)
            risk = max(risk, Risk.IRREVERSIBLE_WRITE, key=lambda r: RISK_ORDER[r])
            reasons.append("non-read HTTP method")

        decision.risk = risk
        decision.reasons = reasons
        decision.rules_fired = rules

        # 3. Does it need approval?
        needs = False
        if self.require_approval_for_irreversible and risk == Risk.IRREVERSIBLE_WRITE:
            needs = True
        if any(self.rules[r].enabled for r in rules if self.rules[r].risk == Risk.IRREVERSIBLE_WRITE):
            needs = True

        # Asking for approval is never itself gated. If it were, an irreversible
        # write could never be authorised: the agent would be blocked at the one
        # call that is supposed to unblock it.
        if tool in APPROVAL_EXEMPT_TOOLS:
            needs = False
            reasons.append(f"{tool} is exempt: it is how approval is requested")

        decision.requires_approval = needs
        if needs:
            decision.risk_reason = (
                "; ".join(reasons) or f"{tool} is an irreversible write"
            )
        return decision

    def _element_text(self, ref: Any, snapshot: Any) -> str:
        if not ref or snapshot is None:
            return ""
        node = None
        finder = getattr(snapshot, "find_by_ref", None)
        if finder is None:
            return ""
        try:
            node = finder(str(ref))
        except Exception:
            return ""
        if node is None:
            return ""
        return (node.label or node.text or "").strip()

    # -- enforcement ------------------------------------------------------
    def enforce(self, decision: Decision, approvals: ApprovalStore) -> tuple[bool, str]:
        """Return `(allowed, reason)`. This is the actual gate."""
        if not decision.requires_approval:
            return True, ""
        if self.auto_approve_reads and decision.risk == Risk.READ:
            return True, ""
        if approvals.has(decision.fingerprint):
            return True, ""
        return False, (
            f"BLOCKED: {decision.tool} needs approval ({decision.risk_reason}). "
            f"Call request_approval with action=<what you want to do>, "
            f"justification=<why it is safe>, and fingerprint={decision.fingerprint!r} "
            "— use that exact fingerprint, or the approval will not cover this call. "
            "The run pauses until a human answers."
        )

    def describe(self) -> dict[str, Any]:
        return {
            "require_approval_for_irreversible": self.require_approval_for_irreversible,
            "thresholds": self.thresholds,
            "rules": {
                name.value: {"enabled": r.enabled, "risk": r.risk.value, "why": r.description}
                for name, r in self.rules.items()
            },
        }


# --------------------------------------------------------------------------
# argument helpers
# --------------------------------------------------------------------------

def _has_any(args: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(k in args and args[k] not in (None, "") for k in keys)


def _first_value(args: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in args and args[key] not in (None, ""):
            return args[key]
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _looks_like_ambiguity(args: dict[str, Any]) -> bool:
    """Ambiguity is asserted explicitly by the caller (usually `ask_user`)."""
    if args.get("ambiguous"):
        return True
    candidates = args.get("candidates")
    if isinstance(candidates, (list, tuple)) and len(candidates) > 1:
        return True
    return False


def _looks_like_duplicate(args: dict[str, Any]) -> bool:
    if args.get("duplicate_suspected"):
        return True
    if args.get("already_exists"):
        return True
    return False


def _touches_persisted_value(args: dict[str, Any]) -> bool:
    return _has_any(args, AMOUNT_KEYS + VENDOR_KEYS + INVOICE_KEYS)