"""Plan: a short, editable checklist plus explicit success criteria.

The plan is deliberately shallow. A deep, immutable plan is a liability: the
moment reality diverges, the agent either follows a plan that no longer applies
or abandons the plan entirely. Here it is a checklist the agent revises, and
the success criteria are stated *before* acting — which is what makes
verification meaningful, because the verifier re-checks the criteria rather
than the agent's story.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class PlanStep:
    id: str
    description: str
    status: StepStatus = StepStatus.PENDING
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description,
                "status": self.status.value, "note": self.note}


@dataclass
class SuccessCriterion:
    """A checkable statement the verifier can independently evaluate."""

    id: str
    description: str
    #: How the verifier should check it.
    check: str = "generic"
    expected: Any = None
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    criteria: list[SuccessCriterion] = field(default_factory=list)
    notes: str = ""
    revision: int = 0

    # -- queries ----------------------------------------------------------
    @property
    def next_step(self) -> PlanStep | None:
        for step in self.steps:
            if step.status is StepStatus.IN_PROGRESS:
                return step
        for step in self.steps:
            if step.status is StepStatus.PENDING:
                return step
        return None

    @property
    def complete(self) -> bool:
        return bool(self.steps) and all(
            s.status in {StepStatus.DONE, StepStatus.FAILED, StepStatus.BLOCKED}
            for s in self.steps
        )

    @property
    def any_failed(self) -> bool:
        return any(s.status in {StepStatus.FAILED, StepStatus.BLOCKED} for s in self.steps)

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    # -- mutation ---------------------------------------------------------
    def set_status(self, step_id: str, status: StepStatus, note: str = "") -> PlanStep | None:
        step = self.step(step_id)
        if step is None:
            return None
        step.status = status
        if note:
            step.note = note
        return step

    def ensure_in_progress(self, description: str) -> PlanStep:
        step = self.next_step
        if step is None:
            step = self.add_step(description)
        step.status = StepStatus.IN_PROGRESS
        return step

    def add_step(self, description: str) -> PlanStep:
        step = PlanStep(id=f"s{len(self.steps) + 1}", description=description)
        self.steps.append(step)
        return step

    def revise(self, steps: list[str], criteria: list[SuccessCriterion] | None = None,
               notes: str = "") -> None:
        """Replace the plan, preserving progress on steps that survived by id."""
        previous = {s.description: s for s in self.steps}
        rebuilt: list[PlanStep] = []
        for index, description in enumerate(steps, start=1):
            step_id = f"s{index}"
            carried = previous.get(description)
            rebuilt.append(
                PlanStep(
                    id=step_id,
                    description=description,
                    status=carried.status if carried else StepStatus.PENDING,
                    note=carried.note if carried else "",
                )
            )
        self.steps = rebuilt
        if criteria is not None:
            self.criteria = criteria
        if notes:
            self.notes = notes
        self.revision += 1

    def add_criterion(self, criterion: SuccessCriterion) -> SuccessCriterion:
        self.criteria.append(criterion)
        return criterion

    # -- rendering --------------------------------------------------------
    def render(self) -> str:
        icon = {
            StepStatus.PENDING: " ",
            StepStatus.IN_PROGRESS: ">",
            StepStatus.DONE: "x",
            StepStatus.FAILED: "!",
            StepStatus.BLOCKED: "#",
        }
        lines = ["## Plan (rev %d)" % self.revision]
        for step in self.steps:
            marker = f"[{icon[step.status]}]"
            suffix = f"  — {step.note}" if step.note else ""
            lines.append(f"{marker} {step.id}: {step.description}{suffix}")
        if self.criteria:
            lines.append("")
            lines.append("## Success criteria (the verifier re-checks these independently)")
            for criterion in self.criteria:
                required = "" if criterion.required else "  (optional)"
                lines.append(f"  - {criterion.id}: {criterion.description}{required}")
        if self.notes:
            lines.append("")
            lines.append(f"## Plan notes\n{self.notes}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "revision": self.revision,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
            "criteria": [c.to_dict() for c in self.criteria],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        plan = cls(
            goal=data.get("goal", ""),
            notes=data.get("notes", ""),
            revision=data.get("revision", 0),
        )
        plan.steps = [
            PlanStep(
                id=s.get("id", f"s{i}"),
                description=s.get("description", ""),
                status=StepStatus(s.get("status", "pending")),
                note=s.get("note", ""),
            )
            for i, s in enumerate(data.get("steps", []), start=1)
        ]
        plan.criteria = [SuccessCriterion(**c) for c in data.get("criteria", [])]
        return plan


DEFAULT_STEP_TEMPLATE = [
    "Open the relevant system and sign in if needed.",
    "Locate the records the request refers to.",
    "Read and record the required values, noting where each came from.",
    "Check whether the work has already been done.",
    "Perform the write, or report why it cannot be done.",
    "Re-read the result independently and compare against the criteria.",
]


class Planner:
    """Holds the plan and the default shape; the model revises it via tools."""

    def __init__(self, goal: str) -> None:
        self.plan = Plan(goal=goal)
        self.plan.steps = [PlanStep(id=f"s{i}", description=d)
                           for i, d in enumerate(DEFAULT_STEP_TEMPLATE, start=1)]
        self.plan.steps[0].status = StepStatus.IN_PROGRESS

    @property
    def criteria(self) -> list[SuccessCriterion]:
        return self.plan.criteria

    def revise(self, payload: dict[str, Any]) -> Plan:
        steps = payload.get("steps")
        if isinstance(steps, list) and steps:
            self.plan.revise(
                [str(s) for s in steps][:14],
                notes=str(payload.get("notes", "")),
            )
        criteria = payload.get("criteria")
        if isinstance(criteria, list):
            self.plan.criteria = [
                SuccessCriterion(
                    id=str(c.get("id") or f"c{i}"),
                    description=str(c.get("description", "")),
                    check=str(c.get("check", "generic")),
                    expected=c.get("expected"),
                    required=bool(c.get("required", True)),
                )
                for i, c in enumerate(criteria[:12], start=1)
                if isinstance(c, dict) and c.get("description")
            ]
        return self.plan

    def mark(self, step_id: str, status: StepStatus, note: str = "") -> None:
        self.plan.set_status(step_id, status, note)

    def complete_current(self, note: str = "") -> PlanStep | None:
        step = self.plan.next_step
        if step is not None:
            step.status = StepStatus.DONE
            if note:
                step.note = note
        return step

    def fail_current(self, note: str) -> PlanStep | None:
        step = self.plan.next_step
        if step is not None:
            step.status = StepStatus.FAILED
            step.note = note
        return step