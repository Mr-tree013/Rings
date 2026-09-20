"""Task: the unit of commitment to *do* something (ADR-0014).

A `Task` is not a calendar entry. It has no start/end time of its own: it is tracked until it
is completed or cancelled, and any time-shaped information lives in `Deadline`, `PlanBlock`
or `WorkSession`.

Titles are validated here (non-blank, bounded) and trimmed by the application service, so the
domain itself stays a pure value object.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidTask, InvalidTaskTransition

TaskId = UUID
"""Stable identity of a task."""

TITLE_MAX_LENGTH = 500


class TaskStatus(StrEnum):
    """Lifecycle of a task. Terminal states are never left in this phase."""

    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    """A three-step priority; deliberately not a 1-100 scale."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


def new_task_id() -> TaskId:
    """Generate a fresh task identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTask(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def validate_task_title(title: str) -> str:
    """Return the stripped title, or raise `InvalidTask`.

    This is the one definition of what a task title is; `Task` and any command draft that
    carries a title both go through it, so no second rule set can drift from the entity.
    """
    stripped = title.strip()
    if not stripped:
        raise InvalidTask("task title must not be blank")
    if len(stripped) > TITLE_MAX_LENGTH:
        raise InvalidTask(f"task title must be at most {TITLE_MAX_LENGTH} characters")
    return stripped


def validate_estimated_minutes(value: int | None) -> None:
    """Check the estimate rule (`None` or at least one minute)."""
    if value is not None and value < 1:
        raise InvalidTask("estimated_minutes must be None or at least 1")


@dataclass(frozen=True, slots=True)
class Task:
    """A commitment that is open until completed or cancelled."""

    title: str
    created_at: datetime
    updated_at: datetime
    id: TaskId = field(default_factory=new_task_id)
    description: str | None = None
    status: TaskStatus = TaskStatus.OPEN
    priority: TaskPriority = TaskPriority.NORMAL
    estimated_minutes: int | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_task_title(self.title)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        _require_optional_aware(self.completed_at, "completed_at")
        _require_optional_aware(self.cancelled_at, "cancelled_at")
        if self.updated_at < self.created_at:
            raise InvalidTask("updated_at must not precede created_at")
        validate_estimated_minutes(self.estimated_minutes)
        if self.status is TaskStatus.OPEN:
            if self.completed_at is not None or self.cancelled_at is not None:
                raise InvalidTask("an OPEN task must not carry completed_at or cancelled_at")
        elif self.status is TaskStatus.COMPLETED:
            if self.completed_at is None:
                raise InvalidTask("a COMPLETED task must carry completed_at")
            if self.cancelled_at is not None:
                raise InvalidTask("a COMPLETED task must not carry cancelled_at")
        else:
            if self.cancelled_at is None:
                raise InvalidTask("a CANCELLED task must carry cancelled_at")
            if self.completed_at is not None:
                raise InvalidTask("a CANCELLED task must not carry completed_at")

    @property
    def is_open(self) -> bool:
        """Whether the task can still be changed."""
        return self.status is TaskStatus.OPEN

    def update_details(
        self,
        *,
        title: str,
        description: str | None,
        priority: TaskPriority,
        estimated_minutes: int | None,
        at: datetime,
    ) -> Task:
        """Return a copy with edited details.

        Only an OPEN task can be edited: a terminal task is history, and the application
        offers no "reopen" path in this phase.
        """
        _require_aware(at, "at")
        if not self.is_open:
            raise InvalidTaskTransition(self.status, "edited")
        return replace(
            self,
            title=title,
            description=description,
            priority=priority,
            estimated_minutes=estimated_minutes,
            updated_at=at,
        )

    def complete(self, at: datetime) -> Task:
        """Move an OPEN task to COMPLETED."""
        _require_aware(at, "at")
        if self.status is not TaskStatus.OPEN:
            raise InvalidTaskTransition(self.status, TaskStatus.COMPLETED)
        return replace(
            self,
            status=TaskStatus.COMPLETED,
            completed_at=at,
            updated_at=at,
        )

    def cancel(self, at: datetime) -> Task:
        """Move an OPEN task to CANCELLED."""
        _require_aware(at, "at")
        if self.status is not TaskStatus.OPEN:
            raise InvalidTaskTransition(self.status, TaskStatus.CANCELLED)
        return replace(
            self,
            status=TaskStatus.CANCELLED,
            cancelled_at=at,
            updated_at=at,
        )


__all__ = [
    "TITLE_MAX_LENGTH",
    "Task",
    "TaskId",
    "TaskPriority",
    "TaskStatus",
    "new_task_id",
    "validate_estimated_minutes",
    "validate_task_title",
]
