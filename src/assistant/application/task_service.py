"""Task application service: the only path that mutates tasks and deadlines (ADR-0014).

The service owns *when* things happen (it injects `Clock` and id factories) while the domain
owns *what is legal* (state transitions, invariants). Concurrency is optimistic: every write
carries the `updated_at` the caller read, so a concurrent edit is rejected instead of being
silently overwritten.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from assistant.domain.deadline import Deadline, DeadlineId, new_deadline_id
from assistant.domain.errors import AmbiguousId, TaskNotFound
from assistant.domain.task import Task, TaskId, TaskPriority, TaskStatus, new_task_id
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import (
    CommitmentRepository,
    CommitmentTransitionResult,
)


@dataclass(frozen=True, slots=True)
class CreateTask:
    """Structured input for a new task (no natural-language date parsing here)."""

    title: str
    description: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    estimated_minutes: int | None = None
    due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EditTask:
    """The complete new details of a task (the caller merges partial input first)."""

    title: str
    description: str | None
    priority: TaskPriority
    estimated_minutes: int | None


class TaskService:
    """Creates, edits and terminates tasks, and manages their active deadline."""

    def __init__(
        self,
        commitments: CommitmentRepository,
        clock: Clock,
        *,
        new_task_id_factory: Callable[[], TaskId] = new_task_id,
        new_deadline_id_factory: Callable[[], DeadlineId] = new_deadline_id,
    ) -> None:
        self._commitments = commitments
        self._clock = clock
        self._new_task_id = new_task_id_factory
        self._new_deadline_id = new_deadline_id_factory

    async def create_task(self, command: CreateTask) -> Task:
        """Create a task, optionally with its deadline, in one transaction."""
        now = self._clock.now()
        task = Task(
            id=self._new_task_id(),
            title=command.title.strip(),
            description=command.description,
            priority=command.priority,
            estimated_minutes=command.estimated_minutes,
            created_at=now,
            updated_at=now,
        )
        deadline = None
        if command.due_at is not None:
            deadline = Deadline(
                id=self._new_deadline_id(),
                task_id=task.id,
                due_at=command.due_at,
                created_at=now,
                updated_at=now,
            )
        return await self._commitments.add_task(task, deadline=deadline)

    async def update_task(self, task_id: TaskId, command: EditTask) -> Task:
        """Edit an OPEN task's details."""
        task = await self.require_task(task_id)
        updated = task.update_details(
            title=command.title.strip(),
            description=command.description,
            priority=command.priority,
            estimated_minutes=command.estimated_minutes,
            at=self._clock.now(),
        )
        return await self._commitments.update_task(updated, expected_updated_at=task.updated_at)

    async def complete_task(self, task_id: TaskId) -> CommitmentTransitionResult:
        """Complete a task and cancel its unfinished plan blocks, atomically."""
        task = await self.require_task(task_id)
        completed = task.complete(self._clock.now())
        return await self._commitments.complete_task(
            completed, expected_updated_at=task.updated_at
        )

    async def cancel_task(self, task_id: TaskId) -> CommitmentTransitionResult:
        """Cancel a task and its unfinished plan blocks, atomically."""
        task = await self.require_task(task_id)
        cancelled = task.cancel(self._clock.now())
        return await self._commitments.cancel_task(
            cancelled, expected_updated_at=task.updated_at
        )

    async def set_deadline(self, task_id: TaskId, due_at: datetime) -> Deadline:
        """Set or move the task's active deadline (OPEN tasks only)."""
        task = await self.require_task(task_id)
        now = self._clock.now()
        deadline = Deadline(
            id=self._new_deadline_id(),
            task_id=task.id,
            due_at=due_at,
            created_at=now,
            updated_at=now,
        )
        return await self._commitments.set_deadline(
            deadline, expected_updated_at=task.updated_at, at=now
        )

    async def clear_deadline(self, task_id: TaskId) -> None:
        """Remove the task's active deadline (OPEN tasks only)."""
        task = await self.require_task(task_id)
        await self._commitments.clear_deadline(
            task_id=task.id, expected_updated_at=task.updated_at, at=self._clock.now()
        )

    async def get_deadline(self, task_id: TaskId) -> Deadline | None:
        """Return the task's active deadline, or `None`."""
        return await self._commitments.get_deadline(task_id)

    async def require_task(self, task_id: TaskId) -> Task:
        """Return the task or raise `TaskNotFound`."""
        task = await self._commitments.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    async def list_tasks(self, *, include_terminal: bool = False) -> list[Task]:
        """List tasks; by default only OPEN ones, ordered by creation."""
        statuses = None if include_terminal else (TaskStatus.OPEN,)
        return await self._commitments.list_tasks(statuses=statuses)

    async def resolve_task_id(self, reference: str) -> TaskId:
        """Resolve a full UUID or an unambiguous id prefix.

        Raises:
            TaskNotFound: nothing matches.
            AmbiguousId: more than one task matches; the CLI never guesses.
        """
        text = reference.strip().lower()
        if not text:
            raise TaskNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        if candidate is not None:
            if await self._commitments.get_task(candidate) is None:
                raise TaskNotFound(candidate)
            return candidate
        matching = [
            task.id
            for task in await self._commitments.list_tasks()
            if str(task.id).startswith(text)
        ]
        if not matching:
            raise TaskNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]


__all__ = ["CreateTask", "EditTask", "TaskService"]
