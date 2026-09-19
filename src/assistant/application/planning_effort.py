"""Remaining-effort calculation from the estimate and recorded work (ADR-0015).

Frozen formula: `actual_minutes = ceil(actual_seconds / 60)`, and
`remaining = estimate - actual_minutes` while actual is still below the estimate. Plans never
reduce remaining effort; only work sessions do.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant.domain.planning import PlanningIssue, PlanningIssueCode
from assistant.domain.task import TaskId


@dataclass(frozen=True, slots=True)
class RemainingEffort:
    """How much work is left, plus the issue that explains a zero."""

    remaining_minutes: int
    issue: PlanningIssue | None = None


def compute_remaining_effort(
    *, task_id: TaskId, estimated_minutes: int | None, actual_seconds: int
) -> RemainingEffort:
    """Return the remaining effort for one task, or an explicit reason for zero."""
    if estimated_minutes is None:
        return RemainingEffort(
            remaining_minutes=0,
            issue=PlanningIssue(
                code=PlanningIssueCode.MISSING_ESTIMATE,
                task_id=task_id,
                message=(
                    "task has no estimate, so the planner will not schedule it; "
                    "set one with `pw task edit`"
                ),
            ),
        )
    actual_minutes = max(0, -(-actual_seconds // 60))
    if actual_minutes >= estimated_minutes:
        return RemainingEffort(
            remaining_minutes=0,
            issue=PlanningIssue(
                code=PlanningIssueCode.ESTIMATE_EXHAUSTED,
                task_id=task_id,
                message=(
                    f"recorded work ({actual_minutes}m) already meets the estimate "
                    f"({estimated_minutes}m); finish the task or raise the estimate"
                ),
                required_minutes=estimated_minutes,
                scheduled_minutes=0,
            ),
        )
    return RemainingEffort(remaining_minutes=estimated_minutes - actual_minutes)


__all__ = ["RemainingEffort", "compute_remaining_effort"]
