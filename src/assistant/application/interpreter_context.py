"""The bounded, deterministic context an interpretation is allowed to see (ADR-0018).

Data minimisation is the design here, not an optimisation:

- only *open* tasks are listed, and only the fields an identity decision needs;
- task descriptions never leave the database (they are the most likely place for content the
  user never meant to send to a provider);
- work sessions, calendar events, plan blocks, notifications, scheduler payloads, knowledge
  content, file paths and mail never enter this structure at all — not because they are
  filtered out later, but because nothing here can read them;
- the list is capped and ordered deterministically, so the same state always produces byte-
  identical context, and the model is told when the list was cut off.

Context values are data. The prompt says so, and the request builder keeps them inside a JSON
document rather than splicing them into instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from assistant.domain.deadline import Deadline
from assistant.domain.task import Task, TaskId, TaskPriority, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository

MAX_INTERPRETER_TASKS = 50
"""How many open tasks the interpreter may see. Everything beyond this stays local."""

_PRIORITY_RANK = {
    TaskPriority.HIGH: 0,
    TaskPriority.NORMAL: 1,
    TaskPriority.LOW: 2,
}


@dataclass(frozen=True, slots=True)
class InterpreterTaskContext:
    """The smallest description of a task that still allows an unambiguous reference."""

    id: TaskId
    title: str
    priority: TaskPriority
    estimated_minutes: int | None
    deadline: datetime | None
    updated_at: datetime

    def to_payload(self) -> dict[str, object]:
        """The JSON representation sent to the provider."""
        return {
            "id": str(self.id),
            "title": self.title,
            "priority": self.priority.value,
            "estimated_minutes": self.estimated_minutes,
            "deadline": _instant(self.deadline),
            "updated_at": _instant(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class InterpreterContext:
    """Everything the model is told about the user's current state, and nothing more."""

    current_time: datetime
    planning_timezone: str | None
    open_tasks: tuple[InterpreterTaskContext, ...]
    tasks_truncated: bool

    @property
    def task_ids(self) -> frozenset[TaskId]:
        """The only task identities the model is authorised to reference."""
        return frozenset(task.id for task in self.open_tasks)

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document placed in the user message."""
        return {
            "current_time": _instant(self.current_time),
            "planning_timezone": self.planning_timezone,
            "tasks_truncated": self.tasks_truncated,
            "open_tasks": [task.to_payload() for task in self.open_tasks],
        }

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, compact separators, UTF-8 text."""
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


class InterpreterContextBuilder:
    """Reads the bounded context from authoritative state. It can only read."""

    def __init__(
        self,
        commitments: CommitmentRepository,
        clock: Clock,
        *,
        planning_timezone: str | None,
        max_tasks: int = MAX_INTERPRETER_TASKS,
    ) -> None:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        self._commitments = commitments
        self._clock = clock
        self._planning_timezone = planning_timezone
        self._max_tasks = max_tasks

    async def build(self) -> InterpreterContext:
        """Return the same context for the same state, every time."""
        tasks = await self._commitments.list_tasks(statuses=(TaskStatus.OPEN,))
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in tasks]) if tasks else {}
        )
        ordered = _order(tasks, deadlines)
        visible = ordered[: self._max_tasks]
        return InterpreterContext(
            current_time=self._clock.now(),
            planning_timezone=self._planning_timezone,
            open_tasks=tuple(
                InterpreterTaskContext(
                    id=task.id,
                    title=task.title,
                    priority=task.priority,
                    estimated_minutes=task.estimated_minutes,
                    deadline=deadline.due_at if deadline is not None else None,
                    updated_at=task.updated_at,
                )
                for task, deadline in visible
            ),
            tasks_truncated=len(ordered) > self._max_tasks,
        )


def _order(
    tasks: list[Task], deadlines: dict[TaskId, Deadline]
) -> list[tuple[Task, Deadline | None]]:
    """Deadline tasks first (earliest due), then the rest; deterministic tie-breaks."""
    with_deadline: list[tuple[Task, Deadline]] = []
    without_deadline: list[Task] = []
    for task in tasks:
        deadline = deadlines.get(task.id)
        if deadline is None:
            without_deadline.append(task)
        else:
            with_deadline.append((task, deadline))
    with_deadline.sort(
        key=lambda entry: (
            entry[1].due_at,
            _PRIORITY_RANK[entry[0].priority],
            entry[0].created_at,
            str(entry[0].id),
        )
    )
    without_deadline.sort(
        key=lambda task: (
            _PRIORITY_RANK[task.priority],
            task.created_at,
            str(task.id),
        )
    )
    return [
        (task, deadline) for task, deadline in with_deadline
    ] + [(task, None) for task in without_deadline]


def _instant(value: datetime | None) -> str | None:
    """Render an instant as UTC ISO 8601, or `None`."""
    return None if value is None else value.astimezone(UTC).isoformat()


__all__ = [
    "MAX_INTERPRETER_TASKS",
    "InterpreterContext",
    "InterpreterContextBuilder",
    "InterpreterTaskContext",
]
