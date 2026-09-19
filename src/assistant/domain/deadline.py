"""Deadline: the latest acceptable completion time for a task (ADR-0014).

A deadline is not a time block: it carries no start, no duration and no reminder time, and it
never occupies calendar time. It is stored as its own row (not a field inside `Task`) and
belongs to exactly one task; at most one deadline per task is active at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidDeadline
from assistant.domain.task import TaskId

DeadlineId = UUID
"""Stable identity of a deadline; rescheduling keeps the same identity."""


def new_deadline_id() -> DeadlineId:
    """Generate a fresh deadline identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidDeadline(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Deadline:
    """The latest time a task should be finished by."""

    task_id: TaskId
    due_at: datetime
    created_at: datetime
    updated_at: datetime
    id: DeadlineId = field(default_factory=new_deadline_id)

    def __post_init__(self) -> None:
        _require_aware(self.due_at, "due_at")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise InvalidDeadline("updated_at must not precede created_at")

    def rescheduled(self, *, due_at: datetime, at: datetime) -> Deadline:
        """Return a copy with a new due time; the deadline identity is preserved."""
        _require_aware(due_at, "due_at")
        _require_aware(at, "at")
        return replace(self, due_at=due_at, updated_at=at)


__all__ = ["Deadline", "DeadlineId", "new_deadline_id"]

