"""PlanBlock: time the user (or a future planner) intends to spend on a task (ADR-0014).

A plan block is *intention*, never *fact*. It has no `actual_minutes`, no completion flag and
no duration bookkeeping: actual work is recorded as `WorkSession` rows, and the two must not
be collapsed into one concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidPlanBlock, PlanBlockNotActive
from assistant.domain.task import TaskId

PlanBlockId = UUID
"""Stable identity of a plan block."""


class PlanBlockStatus(StrEnum):
    """Whether the block is still planned."""

    ACTIVE = "active"
    CANCELLED = "cancelled"


def new_plan_block_id() -> PlanBlockId:
    """Generate a fresh plan block identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidPlanBlock(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PlanBlock:
    """A planned interval for one task."""

    task_id: TaskId
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime
    id: PlanBlockId = field(default_factory=new_plan_block_id)
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.starts_at, "starts_at")
        _require_aware(self.ends_at, "ends_at")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.ends_at <= self.starts_at:
            raise InvalidPlanBlock("ends_at must be after starts_at")
        if self.updated_at < self.created_at:
            raise InvalidPlanBlock("updated_at must not precede created_at")
        if self.cancelled_at is not None:
            _require_aware(self.cancelled_at, "cancelled_at")

    @property
    def status(self) -> PlanBlockStatus:
        """Derived from `cancelled_at`."""
        return (
            PlanBlockStatus.CANCELLED if self.cancelled_at is not None else PlanBlockStatus.ACTIVE
        )

    @property
    def is_active(self) -> bool:
        """Whether this block is still planned."""
        return self.cancelled_at is None

    @property
    def planned_seconds(self) -> int:
        """Planned duration in seconds — never to be confused with actual work."""
        return int((self.ends_at - self.starts_at).total_seconds())

    def cancel(self, at: datetime) -> PlanBlock:
        """Cancel the block; cancelling twice is an error, not a silent no-op."""
        _require_aware(at, "at")
        if not self.is_active:
            raise PlanBlockNotActive(f"plan block {self.id} is already cancelled")
        return replace(self, cancelled_at=at, updated_at=at)


__all__ = [
    "PlanBlock",
    "PlanBlockId",
    "PlanBlockStatus",
    "new_plan_block_id",
]

