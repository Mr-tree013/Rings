"""Typed command drafts: what an interpretation may propose, and nothing it may do (ADR-0018).

A draft is *data about an intent*. It has no `execute`, no service handle and no way to reach a
database — the only thing the project can do with one is render it as a preview and print the
equivalent structured CLI command for a human to run.

The value rules are the entities' rules, not a second copy: a draft title goes through
`validate_task_title`, a calendar interval through `validate_event_interval`, so a draft can
never describe something the corresponding domain object would reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from assistant.domain.calendar_event import (
    validate_event_interval,
    validate_event_title,
)
from assistant.domain.errors import InvalidCommandDraft
from assistant.domain.task import (
    TaskId,
    TaskPriority,
    validate_estimated_minutes,
    validate_task_title,
)


class CommandKind(StrEnum):
    """The commands V1 can interpret. Everything else is honestly unsupported."""

    CREATE_TASK = "create_task"
    COMPLETE_TASK = "complete_task"
    CANCEL_TASK = "cancel_task"
    SET_DEADLINE = "set_deadline"
    CLEAR_DEADLINE = "clear_deadline"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    REQUEST_WEEK_PLAN = "request_week_plan"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCommandDraft(f"{field_name} must be timezone-aware")


def _optional_text(value: str | None, field_name: str) -> str | None:
    """Normalise "the model said nothing" into `None` instead of an empty string."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > 2000:
        raise InvalidCommandDraft(f"{field_name} must be at most 2000 characters")
    return stripped


def _require_task_id(value: object) -> None:
    if not isinstance(value, UUID):
        raise InvalidCommandDraft("a task reference must be a UUID")


@dataclass(frozen=True, slots=True)
class CreateTaskDraft:
    """A new task, as described by the user."""

    title: str
    description: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    estimated_minutes: int | None = None
    deadline: datetime | None = None

    kind: ClassVar[CommandKind] = CommandKind.CREATE_TASK

    def __post_init__(self) -> None:
        try:
            stripped = validate_task_title(self.title)
            validate_estimated_minutes(self.estimated_minutes)
        except Exception as exc:
            raise InvalidCommandDraft(str(exc)) from exc
        object.__setattr__(self, "title", stripped)
        object.__setattr__(self, "description", _optional_text(self.description, "description"))
        if not isinstance(self.priority, TaskPriority):
            raise InvalidCommandDraft(
                f"priority must be one of: {', '.join(member.value for member in TaskPriority)}"
            )
        if self.deadline is not None:
            _require_aware(self.deadline, "deadline")


@dataclass(frozen=True, slots=True)
class CompleteTaskDraft:
    """Mark an existing task complete."""

    task_id: TaskId

    kind: ClassVar[CommandKind] = CommandKind.COMPLETE_TASK

    def __post_init__(self) -> None:
        _require_task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class CancelTaskDraft:
    """Cancel an existing task."""

    task_id: TaskId

    kind: ClassVar[CommandKind] = CommandKind.CANCEL_TASK

    def __post_init__(self) -> None:
        _require_task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class SetDeadlineDraft:
    """Give an existing task a deadline."""

    task_id: TaskId
    due_at: datetime

    kind: ClassVar[CommandKind] = CommandKind.SET_DEADLINE

    def __post_init__(self) -> None:
        _require_task_id(self.task_id)
        _require_aware(self.due_at, "due_at")


@dataclass(frozen=True, slots=True)
class ClearDeadlineDraft:
    """Remove an existing task's deadline."""

    task_id: TaskId

    kind: ClassVar[CommandKind] = CommandKind.CLEAR_DEADLINE

    def __post_init__(self) -> None:
        _require_task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class CreateCalendarEventDraft:
    """Occupied time the user wants recorded."""

    title: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None

    kind: ClassVar[CommandKind] = CommandKind.CREATE_CALENDAR_EVENT

    def __post_init__(self) -> None:
        try:
            stripped = validate_event_title(self.title)
            validate_event_interval(self.starts_at, self.ends_at)
        except Exception as exc:
            raise InvalidCommandDraft(str(exc)) from exc
        object.__setattr__(self, "title", stripped)
        object.__setattr__(self, "description", _optional_text(self.description, "description"))


@dataclass(frozen=True, slots=True)
class RequestWeekPlanDraft:
    """Ask for a weekly proposal — the preview is `pw plan week`, which stays reviewable."""

    next_week: bool = False

    kind: ClassVar[CommandKind] = CommandKind.REQUEST_WEEK_PLAN

    def __post_init__(self) -> None:
        if not isinstance(self.next_week, bool):
            raise InvalidCommandDraft("next_week must be a boolean")


type CommandDraft = (
    CreateTaskDraft
    | CompleteTaskDraft
    | CancelTaskDraft
    | SetDeadlineDraft
    | ClearDeadlineDraft
    | CreateCalendarEventDraft
    | RequestWeekPlanDraft
)
"""Every draft the interpreter can produce. A closed set on purpose."""

TIME_BEARING_KINDS: frozenset[CommandKind] = frozenset(
    {
        CommandKind.SET_DEADLINE,
        CommandKind.CREATE_CALENDAR_EVENT,
        CommandKind.REQUEST_WEEK_PLAN,
    }
)
"""Commands whose meaning depends on a configured planning timezone.

`CREATE_TASK` is time-bearing only when it carries a deadline, so it is checked separately.
"""


def is_time_bearing(draft: CommandDraft) -> bool:
    """Whether interpreting this draft required a timezone at all."""
    if isinstance(draft, CreateTaskDraft):
        return draft.deadline is not None
    return draft.kind in TIME_BEARING_KINDS


__all__ = [
    "TIME_BEARING_KINDS",
    "CancelTaskDraft",
    "ClearDeadlineDraft",
    "CommandDraft",
    "CommandKind",
    "CompleteTaskDraft",
    "CreateCalendarEventDraft",
    "CreateTaskDraft",
    "RequestWeekPlanDraft",
    "SetDeadlineDraft",
    "is_time_bearing",
]
