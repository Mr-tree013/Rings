"""WorkRepository port: durable record of work that actually happened (ADR-0014).

Separate from `CommitmentRepository` on purpose: actual work is a log of facts, not planning
state, and nothing about it needs to share a transaction with task transitions.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from assistant.domain.task import TaskId
from assistant.domain.work_session import WorkSession, WorkSessionId


class WorkRepository(Protocol):
    """Durable storage for work sessions."""

    async def add_work_session(self, session: WorkSession) -> WorkSession:
        """Store one work session.

        Raises:
            TaskNotFound: the referenced task does not exist.
        """
        ...

    async def get_work_session(self, session_id: WorkSessionId) -> WorkSession | None:
        """Return the session, or `None`."""
        ...

    async def list_work_sessions_for_task(
        self, task_id: TaskId, *, limit: int | None = None
    ) -> list[WorkSession]:
        """List a task's sessions ordered by `started_at`, then id."""
        ...

    async def total_work_seconds(self, task_id: TaskId) -> int:
        """Sum the task's session durations, in exact seconds."""
        ...

    async def total_work_seconds_for_tasks(
        self, task_ids: Collection[TaskId]
    ) -> dict[TaskId, int]:
        """Sum several tasks at once (tasks without sessions are absent from the result)."""
        ...


__all__ = ["WorkRepository"]

