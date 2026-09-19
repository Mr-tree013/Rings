"""Work application service: recording work that actually happened (ADR-0014).

Recording is deliberately allowed for terminal tasks: people often log what they did *after*
marking a task complete, and the work session is a fact about the past, not a change to the
task's plan.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime

from assistant.domain.errors import TaskNotFound, WorkSessionNotFound
from assistant.domain.task import TaskId
from assistant.domain.work_session import WorkSession, WorkSessionId, new_work_session_id
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.work_repository import WorkRepository


class WorkService:
    """Records and reads the actual effort spent on tasks."""

    def __init__(
        self,
        work: WorkRepository,
        commitments: CommitmentRepository,
        clock: Clock,
        *,
        new_session_id_factory: Callable[[], WorkSessionId] = new_work_session_id,
    ) -> None:
        self._work = work
        self._commitments = commitments
        self._clock = clock
        self._new_session_id = new_session_id_factory

    async def record_session(
        self, *, task_id: TaskId, started_at: datetime, ended_at: datetime
    ) -> WorkSession:
        """Record one work session for an existing task.

        The task may already be completed or cancelled: back-filling history is normal.

        Raises:
            TaskNotFound: no such task.
            InvalidWorkSession: the interval is empty, negative or not timezone-aware.
        """
        if await self._commitments.get_task(task_id) is None:
            raise TaskNotFound(task_id)
        session = WorkSession(
            id=self._new_session_id(),
            task_id=task_id,
            started_at=started_at,
            ended_at=ended_at,
            created_at=self._clock.now(),
        )
        return await self._work.add_work_session(session)

    async def list_task_sessions(
        self, task_id: TaskId, *, limit: int | None = None
    ) -> list[WorkSession]:
        """List a task's sessions, oldest first."""
        return await self._work.list_work_sessions_for_task(task_id, limit=limit)

    async def get_task_actual_seconds(self, task_id: TaskId) -> int:
        """Total actual effort for a task, in exact seconds (never from plan blocks)."""
        if await self._commitments.get_task(task_id) is None:
            raise TaskNotFound(task_id)
        return await self._work.total_work_seconds(task_id)

    async def actual_seconds_for_tasks(
        self, task_ids: Collection[TaskId]
    ) -> dict[TaskId, int]:
        """Total actual effort for several tasks at once (for list views)."""
        return await self._work.total_work_seconds_for_tasks(task_ids)

    async def require_session(self, session_id: WorkSessionId) -> WorkSession:
        """Return one session or raise `WorkSessionNotFound`."""
        session = await self._work.get_work_session(session_id)
        if session is None:
            raise WorkSessionNotFound(session_id)
        return session


__all__ = ["WorkService"]
