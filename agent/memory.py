"""Structured working memory with provenance.

Two tiers:

* **facts** — `key -> {value, source, step, ts, confidence}`. Provenance is not
  decoration: it is what lets the verifier and the report tell the user *where*
  a number came from, and it is what makes "fabricate a value" detectable after
  the fact (a fact with no source is a fact nobody wrote down).
* **rolling summary** — older steps collapse into a bounded narrative so the
  context window stays small without losing the thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Fact:
    key: str
    value: Any
    source: str = ""
    step: int = 0
    confidence: float = 1.0
    ts: float = field(default_factory=time.time)
    #: Set when two sources disagreed about this key.
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_source(self) -> bool:
        return bool(self.source.strip())

    def render(self) -> str:
        rendered = self.value if isinstance(self.value, str) else repr(self.value)
        bits = [f"{self.key} = {rendered}"]
        if self.source:
            bits.append(f"(source: {self.source}")
            if self.step:
                bits.append(f", step {self.step}")
            bits.append(")")
        if self.confidence < 1.0:
            bits.append(f" [confidence {self.confidence:.2f}]")
        if self.conflicts:
            others = "; ".join(
                f"{c.get('value')} via {c.get('source')}" for c in self.conflicts
            )
            bits.append(f" !! CONFLICTS WITH: {others}")
        return " ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "step": self.step,
            "confidence": self.confidence,
            "conflicts": self.conflicts,
        }


@dataclass
class StepSummary:
    step: int
    goal: str
    outcome: str
    tools: list[str] = field(default_factory=list)
    error: str | None = None


class WorkingMemory:
    """Bounded, provenance-carrying fact store + rolling step summary."""

    def __init__(self, *, max_facts: int = 60, summary_window: int = 8) -> None:
        self.facts: dict[str, Fact] = {}
        self.step_summaries: list[StepSummary] = []
        self.max_facts = max_facts
        self.summary_window = summary_window
        self.notes: list[str] = []
        self._unsourced: list[str] = []

    # -- facts ------------------------------------------------------------
    def remember(
        self,
        key: str,
        value: Any,
        *,
        source: str = "",
        step: int = 0,
        confidence: float = 1.0,
    ) -> Fact:
        """Write a fact. Re-writing a key with a different value records a conflict
        rather than silently overwriting — that is how silent data conflicts
        become visible instead of disappearing."""
        key = key.strip()
        if not key:
            raise ValueError("memory key must not be empty")

        existing = self.facts.get(key)
        if existing is not None:
            if _values_differ(existing.value, value):
                if source and not _source_matches(existing.source, source):
                    existing.conflicts.append({"value": value, "source": source, "step": step})
                else:
                    existing.conflicts.append(
                        {"value": value, "source": existing.source or source, "step": step}
                    )
            existing.value = value
            if source:
                existing.source = source
            existing.step = step or existing.step
            existing.confidence = min(existing.confidence, confidence) if source else confidence
            return existing

        fact = Fact(key=key, value=value, source=source, step=step, confidence=confidence)
        self.facts[key] = fact
        if not fact.has_source:
            self._unsourced.append(key)
        self._evict_if_needed()
        return fact

    def _evict_if_needed(self) -> None:
        if len(self.facts) <= self.max_facts:
            return
        # Drop the oldest facts that carry no provenance first, then oldest.
        while len(self.facts) > self.max_facts:
            candidates = [f for f in self.facts.values() if not f.has_source]
            pool = candidates or list(self.facts.values())
            victim = min(pool, key=lambda f: (f.step, f.ts))
            self.facts.pop(victim.key, None)

    def recall(self, query: str = "") -> list[Fact]:
        if not query.strip():
            return sorted(self.facts.values(), key=lambda f: (f.step, f.ts))
        needle = query.strip().lower()
        scored: list[tuple[int, Fact]] = []
        for fact in self.facts.values():
            haystack_key = fact.key.lower()
            haystack_value = str(fact.value).lower()
            haystack_source = fact.source.lower()
            if haystack_key == needle:
                score = 0
            elif haystack_key.startswith(needle):
                score = 1
            elif needle in haystack_key:
                score = 2
            elif needle in haystack_value:
                score = 3
            elif needle in haystack_source:
                score = 4
            else:
                continue
            scored.append((score, fact))
        scored.sort(key=lambda pair: (pair[0], pair[1].step))
        return [fact for _, fact in scored]

    def get(self, key: str, default: Any = None) -> Any:
        fact = self.facts.get(key)
        return fact.value if fact else default

    def has(self, key: str) -> bool:
        return key in self.facts

    @property
    def conflicts(self) -> list[Fact]:
        return [f for f in self.facts.values() if f.conflicts]

    @property
    def unsourced_keys(self) -> list[str]:
        return [f.key for f in self.facts.values() if not f.has_source]

    # -- rolling summary --------------------------------------------------
    def record_step(
        self, step: int, goal: str, outcome: str, tools: Iterable[str] = (),
        error: str | None = None,
    ) -> StepSummary:
        summary = StepSummary(
            step=step, goal=goal, outcome=outcome, tools=list(tools), error=error
        )
        self.step_summaries.append(summary)
        return summary

    def note(self, text: str) -> None:
        self.notes.append(text)

    def render_facts(self, limit: int | None = None) -> str:
        facts = sorted(self.facts.values(), key=lambda f: (f.step, f.ts))
        if limit is not None:
            facts = facts[-limit:]
        if not facts:
            return "(no facts recorded yet)"
        return "\n".join(f"- {f.render()}" for f in facts)

    def render_summary(self, recent: int | None = None) -> str:
        keep = recent if recent is not None else self.summary_window
        older = self.step_summaries[:-keep] if keep else self.step_summaries
        recent_steps = self.step_summaries[-keep:] if keep else []
        lines: list[str] = []
        if older:
            lines.append(f"Earlier ({len(older)} steps):")
            for s in older:
                marker = " ERROR" if s.error else ""
                lines.append(f"  step {s.step}{marker}: {s.goal} -> {s.outcome}")
        if recent_steps:
            lines.append("Recent steps:")
            for s in recent_steps:
                marker = f" ERROR: {s.error}" if s.error else ""
                lines.append(f"  step {s.step}: {s.goal} -> {s.outcome}{marker}")
        if self.notes:
            lines.append("Notes:")
            lines += [f"  - {n}" for n in self.notes[-5:]]
        return "\n".join(lines) if lines else "(no steps yet)"

    def render(self, *, recent_steps: int | None = None, fact_limit: int = 40) -> str:
        parts = ["## Working memory", self.render_facts(limit=fact_limit)]
        summary = self.render_summary(recent=recent_steps)
        if summary:
            parts += ["", "## Progress log", summary]
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": [f.to_dict() for f in self.facts.values()],
            "steps": [
                {
                    "step": s.step, "goal": s.goal, "outcome": s.outcome,
                    "tools": s.tools, "error": s.error,
                }
                for s in self.step_summaries
            ],
            "notes": list(self.notes),
            "conflicts": [f.key for f in self.conflicts],
        }


def _values_differ(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > 1e-9
    return str(a) != str(b)


def _source_matches(a: str, b: str) -> bool:
    return bool(a) and a.strip().lower() == b.strip().lower()