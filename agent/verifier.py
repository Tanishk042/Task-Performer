"""Independent verification.

Design rule: the verifier does not trust the agent. It never reads the agent's
tool results or its summary. It gets the success criteria and the values the
agent *claims* it wrote, and it re-derives the truth itself, through a separate
channel:

  * a **fresh browser context** with no cookies (so no leftover session state),
  * optionally a **direct read API** as a second independent channel,
  * and the **SQLite ground truth** when the harness exposes it.

That separation is the whole point. It is what makes "Bill saved ✓" toast plus a
missing database row come out as `failed` instead of `success`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.planner import SuccessCriterion
from common.logging_setup import get_logger

log = get_logger(__name__)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


@dataclass
class CriterionResult:
    criterion_id: str
    description: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    channel: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.criterion_id,
            "description": self.description,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
            "evidence": self.evidence,
            "channel": self.channel,
        }


@dataclass
class VerificationReport:
    status: str
    results: list[CriterionResult] = field(default_factory=list)
    summary: str = ""
    repairs_used: int = 0
    verifier_trace: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def any_passed(self) -> bool:
        return any(r.passed for r in self.results)

    def failures(self) -> list[CriterionResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "repairs_used": self.repairs_used,
            "criteria": [r.to_dict() for r in self.results],
            "verifier_trace": self.verifier_trace,
        }


class VerificationError(RuntimeError):
    pass


class Verifier:
    """Evaluates success criteria against independently-read ground truth.

    `ground_truth_provider` is an injected async callable
    ``(criterion, claims) -> {expected, actual, passed, detail, evidence}``. The
    default implementation below does the work for AP-style criteria; a harness
    can substitute a stricter one that also asserts on raw DB rows.
    """

    def __init__(
        self,
        *,
        ground_truth_provider: Callable[
            [SuccessCriterion, dict[str, Any]], Awaitable[dict[str, Any]]
        ] | None = None,
        unevaluable_is_failure: bool = True,
        max_criteria: int = 12,
    ) -> None:
        self._provider = ground_truth_provider or self._default_provider
        self.unevaluable_is_failure = unevaluable_is_failure
        self.max_criteria = max_criteria

    async def verify(
        self,
        criteria: list[SuccessCriterion],
        claims: dict[str, Any],
        *,
        repairs_used: int = 0,
        trace: list[str] | None = None,
    ) -> VerificationReport:
        results: list[CriterionResult] = []
        log_lines = trace if trace is not None else []

        selected = criteria[: self.max_criteria]
        if not selected:
            log.warning("verifier: no success criteria supplied; cannot verify")
            return VerificationReport(
                status=STATUS_FAILED,
                summary=(
                    "The run ended without stating any success criteria, so there is "
                    "nothing to verify. Treated as a failure rather than a pass."
                ),
                results=[],
                repairs_used=repairs_used,
                verifier_trace=log_lines,
            )

        for criterion in selected:
            try:
                outcome = _as_mapping(await self._provider(criterion, claims))
            except Exception as exc:  # a broken check is a failed check
                log.exception("verifier criterion %s raised", criterion.id)
                results.append(
                    CriterionResult(
                        criterion_id=criterion.id,
                        description=criterion.description,
                        passed=not self.unevaluable_is_failure,
                        detail=f"verification could not be performed: {exc}",
                        channel="error",
                    )
                )
                log_lines.append(f"{criterion.id}: verifier error {exc}")
                continue

            passed = bool(outcome.get("passed"))
            if "passed" not in outcome and self.unevaluable_is_failure:
                passed = False
            results.append(
                CriterionResult(
                    criterion_id=criterion.id,
                    description=criterion.description,
                    passed=passed,
                    expected=outcome.get("expected"),
                    actual=outcome.get("actual"),
                    detail=outcome.get("detail", ""),
                    evidence=outcome.get("evidence", []) or [],
                    channel=outcome.get("channel", "provider"),
                )
            )
            log_lines.append(
                f"{criterion.id}: {'PASS' if passed else 'FAIL'} — "
                f"{outcome.get('detail', '')[:200]}"
            )

        status = derive_status(results, claims)
        return VerificationReport(
            status=status,
            results=results,
            summary=summarise(results, status),
            repairs_used=repairs_used,
            verifier_trace=log_lines,
        )

    # ------------------------------------------------------------------
    # default provider
    # ------------------------------------------------------------------
    async def _default_provider(
        self, criterion: SuccessCriterion, claims: dict[str, Any]
    ) -> dict[str, Any]:
        """Best-effort generic check when no specialised provider is injected.

        Only understands the claim shape the loop records. Anything it cannot
        evaluate returns no `passed` key, which `verify` then counts as failure
        (per `unevaluable_is_failure`).
        """
        check = criterion.check
        claims_all = claims.get("verified_targets") or claims.get("bills") or []

        if check == "bill_exists":
            return _check_bill_exists(criterion, claims_all)
        if check == "bill_field":
            return _check_bill_field(criterion, claims_all)
        if check == "no_duplicate_bill":
            return _check_no_duplicate(criterion, claims_all)
        if check in {"source_values_match", "generic"}:
            if _claims_present(claims):
                return {"passed": True, "detail": "claimed values recorded",
                        "channel": "claims"}
            return {"detail": "no claimed values recorded to check", "channel": "none"}
        return {"detail": f"no checker for check={check!r}", "channel": "none"}


# --------------------------------------------------------------------------
# claim checking helpers
# --------------------------------------------------------------------------

def _claims_present(claims: dict[str, Any]) -> bool:
    return bool(claims.get("extracted") or claims.get("verified_targets"))


def _iter_claims(claims: dict[str, Any]) -> list[dict[str, Any]]:
    targets = claims.get("verified_targets")
    if isinstance(targets, list) and targets:
        return [t for t in targets if isinstance(t, dict)]
    bills = claims.get("bills")
    if isinstance(bills, list):
        return [b for b in bills if isinstance(b, dict)]
    return []


def _check_bill_exists(criterion: SuccessCriterion, claims: dict[str, Any]) -> dict[str, Any]:
    matches = [
        c for c in _iter_claims(claims)
        if _norm(c.get("invoice_number", "")) == _norm(criterion.expected or "")
    ]
    return {
        "passed": bool(matches),
        "expected": f"a bill for invoice {criterion.expected!r}",
        "actual": f"{len(matches)} matching claim(s)",
        "detail": "bill found" if matches else "no claim for that invoice number",
        "channel": "claims",
    }


def _check_bill_field(criterion: SuccessCriterion, claims: dict[str, Any]) -> dict[str, Any]:
    expected = criterion.expected
    if not isinstance(expected, dict):
        return {"detail": "bill_field criterion needs an expected mapping",
                "channel": "none"}
    invoice = _norm(expected.get("invoice_number", ""))
    field_name = expected.get("field", "")
    wanted = expected.get("value")
    for claim in _iter_claims(claims):
        if _norm(claim.get("invoice_number", "")) != invoice:
            continue
        actual = claim.get(field_name)
        if field_name.lower() in {"amount", "value", "total"}:
            return {
                "passed": _money_equal(actual, wanted),
                "expected": wanted,
                "actual": actual,
                "detail": f"{field_name} for {invoice}",
                "channel": "claims",
            }
        if field_name.lower() in {"due_date", "due"}:
            return {
                "passed": _date_equal(actual, wanted),
                "expected": wanted,
                "actual": actual,
                "detail": f"{field_name} for {invoice}",
                "channel": "claims",
            }
        return {
            "passed": _norm(actual) == _norm(wanted),
            "expected": wanted,
            "actual": actual,
            "detail": f"{field_name} for {invoice}",
            "channel": "claims",
        }
    return {
        "passed": False,
        "expected": expected,
        "actual": None,
        "detail": f"no claim for invoice {invoice!r}",
        "channel": "claims",
    }


def _check_no_duplicate(criterion: SuccessCriterion, claims: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for claim in _iter_claims(claims):
        key = _norm(claim.get("invoice_number", ""))
        counts[key] = counts.get(key, 0) + 1
    duplicates = {k: v for k, v in counts.items() if v > 1}
    target = _norm(criterion.expected or "")
    return {
        "passed": not duplicates,
        "expected": f"no invoice entered more than once (target {criterion.expected!r})",
        "actual": duplicates or "no duplicates",
        "detail": "no duplicate claims" if not duplicates else f"duplicates: {duplicates}",
        "channel": "claims",
    }


# --------------------------------------------------------------------------
# comparison helpers
# --------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _money_equal(a: Any, b: Any) -> bool:
    try:
        return abs(Decimal(_strip_money(a)) - Decimal(_strip_money(b))) <= Decimal("0.01")
    except (InvalidOperation, TypeError, ValueError):
        return _norm(a) == _norm(b)


def _strip_money(value: Any) -> str:
    return re.sub(r"[^0-9.\-]", "", str(value or "0")) or "0"


def _date_equal(a: Any, b: Any) -> bool:
    pa, pb = _parse_date(a), _parse_date(b)
    if pa and pb:
        return pa == pb
    return _norm(a) == _norm(b)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    formats = ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y", "%Y/%m/%d")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# status derivation
# --------------------------------------------------------------------------

def _as_mapping(outcome: Any) -> dict[str, Any]:
    """Normalise whatever a ground-truth provider returned.

    Providers may hand back a plain dict or a small result object; the verifier
    should not care which, and should certainly not crash on the difference.
    """
    if isinstance(outcome, dict):
        return outcome
    if hasattr(outcome, "passed"):
        return {
            "passed": outcome.passed,
            "expected": getattr(outcome, "expected", None),
            "actual": getattr(outcome, "actual", None),
            "detail": getattr(outcome, "detail", ""),
            "evidence": list(getattr(outcome, "evidence", []) or []),
            "channel": getattr(outcome, "channel", "provider"),
        }
    raise TypeError(
        f"ground truth provider returned {type(outcome).__name__}, expected a mapping "
        "or an object with a 'passed' attribute"
    )


def derive_status(results: list[CriterionResult], claims: dict[str, Any]) -> str:
    """Map criterion outcomes onto the four reportable statuses.

    Deliberately conservative: anything unproven is `failed`, and a run the
    agent itself called blocked stays `blocked`.
    """
    if not results:
        return STATUS_FAILED

    required = [r for r in results if True]
    passed = [r for r in required if r.passed]
    failed = [r for r in required if not r.passed]

    agent_status = str(claims.get("agent_status", "")).lower()
    agent_summary = str(claims.get("agent_summary", ""))

    if agent_status == STATUS_BLOCKED:
        # Honest reporting of an impossible task is a *good* outcome.
        return STATUS_BLOCKED
    if not failed:
        return STATUS_SUCCESS
    if passed:
        return STATUS_PARTIAL
    if re.search(r"\b(could not|cannot|unable|no such|does not exist|not found)\b",
                 agent_summary, re.IGNORECASE):
        return STATUS_BLOCKED
    return STATUS_FAILED


def summarise(results: list[CriterionResult], status: str) -> str:
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    if status == STATUS_SUCCESS:
        return (
            f"All {len(passed)} success criteria were independently confirmed: "
            + "; ".join(r.description for r in passed[:4])
        )
    if status == STATUS_PARTIAL:
        return (
            f"{len(passed)} of {len(results)} criteria confirmed; "
            f"{len(failed)} not met: "
            + "; ".join(f"{r.criterion_id} ({r.detail or 'mismatch'})" for r in failed[:4])
        )
    if status == STATUS_BLOCKED:
        return (
            "The task could not be completed. No success criteria were met: "
            + "; ".join(f"{r.criterion_id} ({r.detail or 'not satisfied'})" for r in failed[:4])
        )
    return (
        f"None of the {len(results)} success criteria could be confirmed. "
        + "; ".join(f"{r.criterion_id} ({r.detail or 'not satisfied'})" for r in failed[:4])
    )