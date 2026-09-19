"""SQLite implementation of the WorkRepository port (ADR-0009, ADR-0014).

Work sessions are an append-only log of what actually happened. The table stores only the two
timestamps: durations are derived on read, so a session's recorded length can never drift from
its facts.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection
from uuid import UUID

from assistant.domain.errors import DomainError, DuplicateCommitment, TaskNotFound
from assistant.domain.task import TaskId
from assistant.domain.work_session import WorkSession, WorkSessionId
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_SESSION_FIELDS = "id, task_id, started_at, ended_at, created_at"


class SqliteWorkRepository:
    """Durable work-session storage backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_work_session(self, session: WorkSession) -> WorkSession:
        return await asyncio.to_thread(self._add_sync, session)

    async def get_work_session(self, session_id: WorkSessionId) -> WorkSession | None:
        return await asyncio.to_thread(self._get_sync, session_id)

    async def list_work_sessions_for_task(
        self, task_id: TaskId, *, limit: int | None = None
    ) -> list[WorkSession]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_sync, task_id, limit)

    async def total_work_seconds(self, task_id: TaskId) -> int:
        totals = await asyncio.to_thread(self._totals_sync, (task_id,))
        return totals.get(task_id, 0)

    async def total_work_seconds_for_tasks(
        self, task_ids: Collection[TaskId]
    ) -> dict[TaskId, int]:
        if not task_ids:
            return {}
        return await asyncio.to_thread(self._totals_sync, tuple(task_ids))

    # ------------------------------------------------------------ blocking internals

    def _add_sync(self, session: WorkSession) -> WorkSession:
        try:
            with self._database.connect() as connection:
                connection.execute(
                    f"INSERT INTO work_sessions ({_SESSION_FIELDS}) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(session.id),
                        str(session.task_id),
                        to_utc_iso(session.started_at),
                        to_utc_iso(session.ended_at),
                        to_utc_iso(session.created_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "FOREIGN KEY" in message.upper():
                raise TaskNotFound(session.task_id) from exc
            raise DuplicateCommitment(message) from exc
        return session

    def _get_sync(self, session_id: WorkSessionId) -> WorkSession | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_FIELDS} FROM work_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def _list_sync(self, task_id: TaskId, limit: int | None) -> list[WorkSession]:
        statement = (
            f"SELECT {_SESSION_FIELDS} FROM work_sessions WHERE task_id = ? "
            "ORDER BY started_at, id"
        )
        parameters: list[object] = [str(task_id)]
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_session(row) for row in rows]

    def _totals_sync(self, task_ids: tuple[TaskId, ...]) -> dict[TaskId, int]:
        placeholders = ", ".join("?" for _ in task_ids)
        statement = (
            "SELECT task_id, started_at, ended_at FROM work_sessions "
            f"WHERE task_id IN ({placeholders})"
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                statement, tuple(str(task_id) for task_id in task_ids)
            ).fetchall()
        totals: dict[TaskId, int] = {}
        for row in rows:
            task_id = UUID(str(row["task_id"]))
            started = from_utc_iso(str(row["started_at"]))
            ended = from_utc_iso(str(row["ended_at"]))
            totals[task_id] = totals.get(task_id, 0) + int((ended - started).total_seconds())
        return totals


def _row_to_session(row: sqlite3.Row) -> WorkSession:
    try:
        return WorkSession(
            id=UUID(str(row["id"])),
            task_id=UUID(str(row["task_id"])),
            started_at=from_utc_iso(str(row["started_at"])),
            ended_at=from_utc_iso(str(row["ended_at"])),
            created_at=from_utc_iso(str(row["created_at"])),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored work session is not readable: {exc}") from exc


__all__ = ["SqliteWorkRepository"]
