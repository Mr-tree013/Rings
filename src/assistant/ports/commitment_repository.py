"""CommitmentRepository port: durable planning state (ADR-0014).

This port deliberately covers tasks, deadlines, calendar events and plan blocks together.
The reason is atomicity, not convenience: completing or cancelling a task must also cancel
that task's unfinished plan blocks in the *same* database transaction. With one repository per
entity, every public method would open its own transaction and the application would have to
stitch two commits together — the half-state the domain forbids.

The interface stays grouped by domain semantics (create/read/update/terminal transitions per
entity). There is no generic CRUD, no raw SQL and no query builder.

`updated_at` is the optimistic concurrency token: task-scoped mutations take the version the
caller read and fail with `StaleTaskUpdate` when someone else moved the task first.
`set_deadline`/`clear_deadline` also advance the task's `updated_at`, because the task's
commitment state changed.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from assistant.domain.calendar_event import CalendarEvent, CalendarEventId
from assistant.domain.deadline import Deadline
from assistant.domain.plan_block import PlanBlock, PlanBlockId
from assistant.domain.scheduled_job import ScheduledJob
from assistant.domain.task import Task, TaskId, TaskStatus


@dataclass(frozen=True, slots=True)
class CommitmentTransitionResult:
    """A terminal task transition plus the plan blocks it cancelled."""

    task: Task
    cancelled_plan_blocks: int


class CommitmentRepository(Protocol):
    """Durable storage for tasks, deadlines, calendar events and plan blocks."""

    # ------------------------------------------------------------------------ tasks

    async def add_task(
        self,
        task: Task,
        *,
        deadline: Deadline | None = None,
        reminder_jobs: Sequence[ScheduledJob] = (),
    ) -> Task:
        """Store a new task, optionally with its active deadline and reminder jobs.

        Everything happens in one transaction: a task whose deadline is stored but whose
        reminders are not would be a task that silently forgets to warn the user.

        Raises:
            InvalidCommitment: the deadline does not belong to the task.
            DuplicateCommitment: a task or deadline with that identity already exists.
        """
        ...

    async def get_task(self, task_id: TaskId) -> Task | None:
        """Return the task, or `None`."""
        ...

    async def list_tasks(
        self,
        *,
        statuses: Collection[TaskStatus] | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        """List tasks ordered by `created_at`, then id.

        `statuses=None` means every status; the caller decides whether terminal tasks are
        interesting.
        """
        ...

    async def update_task(self, task: Task, *, expected_updated_at: datetime) -> Task:
        """Persist edited task details with an optimistic concurrency check.

        Raises:
            TaskNotFound: no such task.
            StaleTaskUpdate: the stored `updated_at` differs from `expected_updated_at`.
            TaskNotOpen: the stored task is not OPEN.
        """
        ...

    async def complete_task(
        self, task: Task, *, expected_updated_at: datetime
    ) -> CommitmentTransitionResult:
        """Store a COMPLETED task and cancel its unfinished plan blocks atomically.

        `task` is the already-transitioned domain object; its `completed_at` is the cutoff:
        active plan blocks whose `ends_at` is after it are cancelled, and blocks that already
        ended are left untouched as planning history.
        """
        ...

    async def cancel_task(
        self, task: Task, *, expected_updated_at: datetime
    ) -> CommitmentTransitionResult:
        """Store a CANCELLED task and cancel its unfinished plan blocks atomically."""
        ...

    # -------------------------------------------------------------------- deadlines

    async def get_deadline(self, task_id: TaskId) -> Deadline | None:
        """Return the task's active deadline, or `None`."""
        ...

    async def list_deadlines(
        self, task_ids: Collection[TaskId]
    ) -> dict[TaskId, Deadline]:
        """Return the active deadlines of several tasks at once (for list views)."""
        ...

    async def set_deadline(
        self,
        deadline: Deadline,
        *,
        expected_updated_at: datetime,
        at: datetime,
        reminder_jobs: Sequence[ScheduledJob] = (),
    ) -> Deadline:
        """Create or reschedule the task's active deadline.

        Rescheduling keeps the same deadline identity. The task must be OPEN (only OPEN tasks
        may change their commitment), and the task's `updated_at` advances to `at`. The
        deadline's `created_at` is used only when the row is created.

        `reminder_jobs` is the reminder schedule that should hold after the change; obsolete
        active jobs (including one that is currently being processed) are cancelled in the same
        transaction.
        """
        ...

    async def clear_deadline(
        self, *, task_id: TaskId, expected_updated_at: datetime, at: datetime
    ) -> None:
        """Remove the task's active deadline (the task must be OPEN).

        The deadline's active reminder jobs are cancelled in the same transaction: a reminder
        for a deadline that no longer exists must never be delivered.

        Raises:
            DeadlineNotFound: the task has no active deadline.
        """
        ...

    # --------------------------------------------------------------- calendar events

    async def add_calendar_event(self, event: CalendarEvent) -> CalendarEvent:
        """Store a new calendar event."""
        ...

    async def get_calendar_event(self, event_id: CalendarEventId) -> CalendarEvent | None:
        """Return the calendar event, or `None`."""
        ...

    async def list_calendar_events(
        self,
        *,
        query_start: datetime,
        query_end: datetime,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        """List events overlapping `[query_start, query_end)` (half-open).

        Overlap means `starts_at < query_end AND ends_at > query_start`, so an event ending
        exactly at `query_start` is not returned.
        """
        ...

    async def cancel_calendar_event(
        self, event_id: CalendarEventId, *, at: datetime
    ) -> CalendarEvent:
        """Cancel an event.

        Raises:
            CalendarEventNotFound: no such event.
            CalendarEventNotActive: the event was already cancelled.
        """
        ...

    # ------------------------------------------------------------------ plan blocks

    async def add_plan_block(self, block: PlanBlock) -> PlanBlock:
        """Store a new plan block (its task must already exist)."""
        ...

    async def get_plan_block(self, plan_block_id: PlanBlockId) -> PlanBlock | None:
        """Return the plan block, or `None`."""
        ...

    async def list_plan_blocks_for_task(
        self, task_id: TaskId, *, include_cancelled: bool = False
    ) -> list[PlanBlock]:
        """List one task's plan blocks ordered by `starts_at`."""
        ...

    async def list_plan_blocks_in_range(
        self,
        *,
        query_start: datetime,
        query_end: datetime,
        include_cancelled: bool = False,
    ) -> list[PlanBlock]:
        """List plan blocks overlapping `[query_start, query_end)` (half-open)."""
        ...

    async def cancel_plan_block(
        self, plan_block_id: PlanBlockId, *, at: datetime
    ) -> PlanBlock:
        """Cancel a plan block by hand.

        Raises:
            PlanBlockNotFound: no such block.
            PlanBlockNotActive: the block was already cancelled.
        """
        ...


__all__ = ["CommitmentRepository", "CommitmentTransitionResult"]
