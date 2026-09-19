"""SQLite implementation of the CommitmentRepository port (ADR-0009, ADR-0014).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each
blocking method opens its own connection inside the worker thread that uses it.

Two properties are enforced here rather than in the application:

- **optimistic concurrency**: task-scoped writes carry the `updated_at` the caller read, and a
  mismatch raises `StaleTaskUpdate` instead of silently overwriting someone else's change;
- **atomic terminal transitions**: completing or cancelling a task and cancelling its
  unfinished plan blocks happen inside one transaction, so the database can never hold a
  completed task with live future plan blocks.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assistant.domain.calendar_event import CalendarEvent, CalendarEventId
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    CalendarEventNotFound,
    DeadlineNotFound,
    DomainError,
    DuplicateCommitment,
    PlanBlockNotFound,
    StaleTaskUpdate,
    TaskNotFound,
    TaskNotOpen,
)
from assistant.domain.plan_block import PlanBlock, PlanBlockId, PlanBlockOrigin
from assistant.domain.task import Task, TaskId, TaskPriority, TaskStatus
from assistant.ports.commitment_repository import CommitmentTransitionResult
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_TASK_FIELDS = (
    "id, title, description, status, priority, estimated_minutes, created_at, updated_at, "
    "completed_at, cancelled_at"
)
_DEADLINE_FIELDS = "id, task_id, due_at, created_at, updated_at"
_EVENT_FIELDS = (
    "id, title, description, starts_at, ends_at, created_at, updated_at, cancelled_at"
)
_BLOCK_FIELDS = (
    "id, task_id, starts_at, ends_at, created_at, updated_at, cancelled_at, origin, proposal_id"
)

_SELECT_TASK_SQL = f"SELECT {_TASK_FIELDS} FROM tasks WHERE id = ?"
_SELECT_DEADLINE_SQL = f"SELECT {_DEADLINE_FIELDS} FROM deadlines WHERE task_id = ?"
_SELECT_EVENT_SQL = f"SELECT {_EVENT_FIELDS} FROM calendar_events WHERE id = ?"
_SELECT_BLOCK_SQL = f"SELECT {_BLOCK_FIELDS} FROM plan_blocks WHERE id = ?"

_INSERT_TASK_SQL = f"INSERT INTO tasks ({_TASK_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_UPDATE_TASK_SQL = """
UPDATE tasks
   SET title = ?, description = ?, status = ?, priority = ?, estimated_minutes = ?,
       created_at = ?, updated_at = ?, completed_at = ?, cancelled_at = ?
 WHERE id = ? AND status = ? AND updated_at = ?
"""

_CANCEL_FUTURE_PLAN_BLOCKS_SQL = """
UPDATE plan_blocks
   SET cancelled_at = ?, updated_at = ?
 WHERE task_id = ? AND cancelled_at IS NULL AND ends_at > ?
"""


class SqliteCommitmentRepository:
    """Durable commitment storage backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------------ tasks

    async def add_task(self, task: Task, *, deadline: Deadline | None = None) -> Task:
        return await asyncio.to_thread(self._add_task_sync, task, deadline)

    async def get_task(self, task_id: TaskId) -> Task | None:
        return await asyncio.to_thread(self._get_task_sync, task_id)

    async def list_tasks(
        self,
        *,
        statuses: Collection[TaskStatus] | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_tasks_sync, statuses, limit)

    async def update_task(self, task: Task, *, expected_updated_at: datetime) -> Task:
        return await asyncio.to_thread(self._update_task_sync, task, expected_updated_at)

    async def complete_task(
        self, task: Task, *, expected_updated_at: datetime
    ) -> CommitmentTransitionResult:
        return await asyncio.to_thread(self._terminal_sync, task, expected_updated_at)

    async def cancel_task(
        self, task: Task, *, expected_updated_at: datetime
    ) -> CommitmentTransitionResult:
        return await asyncio.to_thread(self._terminal_sync, task, expected_updated_at)

    # -------------------------------------------------------------------- deadlines

    async def get_deadline(self, task_id: TaskId) -> Deadline | None:
        return await asyncio.to_thread(self._get_deadline_sync, task_id)

    async def list_deadlines(
        self, task_ids: Collection[TaskId]
    ) -> dict[TaskId, Deadline]:
        if not task_ids:
            return {}
        return await asyncio.to_thread(self._list_deadlines_sync, tuple(task_ids))

    async def set_deadline(
        self, deadline: Deadline, *, expected_updated_at: datetime, at: datetime
    ) -> Deadline:
        return await asyncio.to_thread(
            self._set_deadline_sync, deadline, expected_updated_at, at
        )

    async def clear_deadline(
        self, *, task_id: TaskId, expected_updated_at: datetime, at: datetime
    ) -> None:
        await asyncio.to_thread(self._clear_deadline_sync, task_id, expected_updated_at, at)

    # --------------------------------------------------------------- calendar events

    async def add_calendar_event(self, event: CalendarEvent) -> CalendarEvent:
        return await asyncio.to_thread(self._add_calendar_event_sync, event)

    async def get_calendar_event(self, event_id: CalendarEventId) -> CalendarEvent | None:
        return await asyncio.to_thread(self._get_calendar_event_sync, event_id)

    async def list_calendar_events(
        self,
        *,
        query_start: datetime,
        query_end: datetime,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        return await asyncio.to_thread(
            self._list_calendar_events_sync, query_start, query_end, include_cancelled
        )

    async def cancel_calendar_event(
        self, event_id: CalendarEventId, *, at: datetime
    ) -> CalendarEvent:
        return await asyncio.to_thread(self._cancel_calendar_event_sync, event_id, at)

    # ------------------------------------------------------------------ plan blocks

    async def add_plan_block(self, block: PlanBlock) -> PlanBlock:
        return await asyncio.to_thread(self._add_plan_block_sync, block)

    async def get_plan_block(self, plan_block_id: PlanBlockId) -> PlanBlock | None:
        return await asyncio.to_thread(self._get_plan_block_sync, plan_block_id)

    async def list_plan_blocks_for_task(
        self, task_id: TaskId, *, include_cancelled: bool = False
    ) -> list[PlanBlock]:
        return await asyncio.to_thread(
            self._list_plan_blocks_for_task_sync, task_id, include_cancelled
        )

    async def list_plan_blocks_in_range(
        self,
        *,
        query_start: datetime,
        query_end: datetime,
        include_cancelled: bool = False,
    ) -> list[PlanBlock]:
        return await asyncio.to_thread(
            self._list_plan_blocks_in_range_sync, query_start, query_end, include_cancelled
        )

    async def cancel_plan_block(
        self, plan_block_id: PlanBlockId, *, at: datetime
    ) -> PlanBlock:
        return await asyncio.to_thread(self._cancel_plan_block_sync, plan_block_id, at)

    # ------------------------------------------------------------ blocking internals

    def _add_task_sync(self, task: Task, deadline: Deadline | None) -> Task:
        if deadline is not None and deadline.task_id != task.id:
            raise CommitmentStoreError("deadline does not belong to the task being stored")
        with self._database.connect() as connection, transaction(connection):
            try:
                connection.execute(_INSERT_TASK_SQL, _task_parameters(task))
                if deadline is not None:
                    connection.execute(
                        f"INSERT INTO deadlines ({_DEADLINE_FIELDS}) VALUES (?, ?, ?, ?, ?)",
                        _deadline_parameters(deadline),
                    )
            except sqlite3.IntegrityError as exc:
                raise _translate_integrity_error(exc) from exc
        return task

    def _get_task_sync(self, task_id: TaskId) -> Task | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_TASK_SQL, (str(task_id),)).fetchone()
        return None if row is None else _row_to_task(row)

    def _list_tasks_sync(
        self, statuses: Collection[TaskStatus] | None, limit: int | None
    ) -> list[Task]:
        statement = f"SELECT {_TASK_FIELDS} FROM tasks"
        parameters: list[object] = []
        if statuses is not None:
            values = sorted(str(status) for status in statuses)
            placeholders = ", ".join("?" for _ in values)
            statement += f" WHERE status IN ({placeholders})"
            parameters.extend(values)
        statement += " ORDER BY created_at, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_task(row) for row in rows]

    def _update_task_sync(self, task: Task, expected_updated_at: datetime) -> Task:
        with self._database.connect() as connection, transaction(connection):
            self._require_open_at_version(connection, task.id, expected_updated_at)
            cursor = connection.execute(
                _UPDATE_TASK_SQL,
                (
                    task.title,
                    task.description,
                    str(task.status),
                    str(task.priority),
                    task.estimated_minutes,
                    to_utc_iso(task.created_at),
                    to_utc_iso(task.updated_at),
                    _optional_iso(task.completed_at),
                    _optional_iso(task.cancelled_at),
                    str(task.id),
                    str(TaskStatus.OPEN),
                    to_utc_iso(expected_updated_at),
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskUpdate(task.id, expected_updated_at)
        return task

    def _terminal_sync(
        self, task: Task, expected_updated_at: datetime
    ) -> CommitmentTransitionResult:
        cutoff = task.completed_at or task.cancelled_at
        if cutoff is None:
            raise CommitmentStoreError("a terminal transition needs completed_at or cancelled_at")
        with self._database.connect() as connection, transaction(connection):
            self._require_open_at_version(connection, task.id, expected_updated_at)
            cursor = connection.execute(
                _UPDATE_TASK_SQL,
                (
                    task.title,
                    task.description,
                    str(task.status),
                    str(task.priority),
                    task.estimated_minutes,
                    to_utc_iso(task.created_at),
                    to_utc_iso(task.updated_at),
                    _optional_iso(task.completed_at),
                    _optional_iso(task.cancelled_at),
                    str(task.id),
                    str(TaskStatus.OPEN),
                    to_utc_iso(expected_updated_at),
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskUpdate(task.id, expected_updated_at)
            cancelled_blocks = connection.execute(
                _CANCEL_FUTURE_PLAN_BLOCKS_SQL,
                (to_utc_iso(cutoff), to_utc_iso(cutoff), str(task.id), to_utc_iso(cutoff)),
            ).rowcount
            row = connection.execute(_SELECT_TASK_SQL, (str(task.id),)).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise CommitmentStoreError(f"task {task.id} disappeared during its transition")
        return CommitmentTransitionResult(
            task=_row_to_task(row), cancelled_plan_blocks=cancelled_blocks
        )

    def _get_deadline_sync(self, task_id: TaskId) -> Deadline | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_DEADLINE_SQL, (str(task_id),)).fetchone()
        return None if row is None else _row_to_deadline(row)

    def _list_deadlines_sync(self, task_ids: tuple[TaskId, ...]) -> dict[TaskId, Deadline]:
        placeholders = ", ".join("?" for _ in task_ids)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_DEADLINE_FIELDS} FROM deadlines WHERE task_id IN ({placeholders})",
                tuple(str(task_id) for task_id in task_ids),
            ).fetchall()
        return {
            deadline.task_id: deadline
            for deadline in (_row_to_deadline(row) for row in rows)
        }

    def _set_deadline_sync(
        self, deadline: Deadline, expected_updated_at: datetime, at: datetime
    ) -> Deadline:
        with self._database.connect() as connection, transaction(connection):
            self._require_open_at_version(connection, deadline.task_id, expected_updated_at)
            existing = connection.execute(
                _SELECT_DEADLINE_SQL, (str(deadline.task_id),)
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"INSERT INTO deadlines ({_DEADLINE_FIELDS}) VALUES (?, ?, ?, ?, ?)",
                    _deadline_parameters(deadline),
                )
                stored = deadline
            else:
                stored = Deadline(
                    id=UUID(str(existing["id"])),
                    task_id=deadline.task_id,
                    due_at=deadline.due_at,
                    created_at=from_utc_iso(str(existing["created_at"])),
                    updated_at=at,
                )
                connection.execute(
                    "UPDATE deadlines SET due_at = ?, updated_at = ? WHERE task_id = ?",
                    (to_utc_iso(stored.due_at), to_utc_iso(stored.updated_at), str(stored.task_id)),
                )
            self._touch_task(connection, deadline.task_id, at)
        return stored

    def _clear_deadline_sync(
        self, task_id: TaskId, expected_updated_at: datetime, at: datetime
    ) -> None:
        with self._database.connect() as connection, transaction(connection):
            self._require_open_at_version(connection, task_id, expected_updated_at)
            cursor = connection.execute(
                "DELETE FROM deadlines WHERE task_id = ?", (str(task_id),)
            )
            if cursor.rowcount != 1:
                raise DeadlineNotFound(task_id)
            self._touch_task(connection, task_id, at)

    def _add_calendar_event_sync(self, event: CalendarEvent) -> CalendarEvent:
        insert_sql = (
            f"INSERT INTO calendar_events ({_EVENT_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        try:
            with self._database.connect() as connection:
                connection.execute(
                    insert_sql,
                    _event_parameters(event),
                )
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc
        return event

    def _get_calendar_event_sync(self, event_id: CalendarEventId) -> CalendarEvent | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_EVENT_SQL, (str(event_id),)).fetchone()
        return None if row is None else _row_to_calendar_event(row)

    def _list_calendar_events_sync(
        self, query_start: datetime, query_end: datetime, include_cancelled: bool
    ) -> list[CalendarEvent]:
        statement = (
            f"SELECT {_EVENT_FIELDS} FROM calendar_events "
            "WHERE starts_at < ? AND ends_at > ?"
        )
        parameters: list[object] = [to_utc_iso(query_end), to_utc_iso(query_start)]
        if not include_cancelled:
            statement += " AND cancelled_at IS NULL"
        statement += " ORDER BY starts_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_calendar_event(row) for row in rows]

    def _cancel_calendar_event_sync(
        self, event_id: CalendarEventId, at: datetime
    ) -> CalendarEvent:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(_SELECT_EVENT_SQL, (str(event_id),)).fetchone()
            if row is None:
                raise CalendarEventNotFound(event_id)
            event = _row_to_calendar_event(row)
            cancelled = event.cancel(at)
            connection.execute(
                "UPDATE calendar_events SET cancelled_at = ?, updated_at = ? WHERE id = ?",
                (to_utc_iso(at), to_utc_iso(at), str(event_id)),
            )
        return cancelled

    def _add_plan_block_sync(self, block: PlanBlock) -> PlanBlock:
        try:
            with self._database.connect() as connection:
                connection.execute(
                    "INSERT INTO plan_blocks "
                    f"({_BLOCK_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _block_parameters(block),
                )
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc
        return block

    def _get_plan_block_sync(self, plan_block_id: PlanBlockId) -> PlanBlock | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_BLOCK_SQL, (str(plan_block_id),)).fetchone()
        return None if row is None else _row_to_plan_block(row)

    def _list_plan_blocks_for_task_sync(
        self, task_id: TaskId, include_cancelled: bool
    ) -> list[PlanBlock]:
        statement = f"SELECT {_BLOCK_FIELDS} FROM plan_blocks WHERE task_id = ?"
        parameters: list[object] = [str(task_id)]
        if not include_cancelled:
            statement += " AND cancelled_at IS NULL"
        statement += " ORDER BY starts_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_plan_block(row) for row in rows]

    def _list_plan_blocks_in_range_sync(
        self, query_start: datetime, query_end: datetime, include_cancelled: bool
    ) -> list[PlanBlock]:
        statement = f"SELECT {_BLOCK_FIELDS} FROM plan_blocks WHERE starts_at < ? AND ends_at > ?"
        parameters: list[object] = [to_utc_iso(query_end), to_utc_iso(query_start)]
        if not include_cancelled:
            statement += " AND cancelled_at IS NULL"
        statement += " ORDER BY starts_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_plan_block(row) for row in rows]

    def _cancel_plan_block_sync(self, plan_block_id: PlanBlockId, at: datetime) -> PlanBlock:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(_SELECT_BLOCK_SQL, (str(plan_block_id),)).fetchone()
            if row is None:
                raise PlanBlockNotFound(plan_block_id)
            block = _row_to_plan_block(row)
            cancelled = block.cancel(at)
            connection.execute(
                "UPDATE plan_blocks SET cancelled_at = ?, updated_at = ? WHERE id = ?",
                (to_utc_iso(at), to_utc_iso(at), str(plan_block_id)),
            )
        return cancelled

    def _require_open_at_version(
        self, connection: sqlite3.Connection, task_id: TaskId, expected_updated_at: datetime
    ) -> None:
        row = connection.execute(
            "SELECT status, updated_at FROM tasks WHERE id = ?", (str(task_id),)
        ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if str(row["updated_at"]) != to_utc_iso(expected_updated_at):
            raise StaleTaskUpdate(task_id, expected_updated_at)
        if str(row["status"]) != str(TaskStatus.OPEN):
            raise TaskNotOpen(
                f"task {task_id} is {row['status']}; only OPEN tasks can change"
            )

    def _touch_task(
        self, connection: sqlite3.Connection, task_id: TaskId, at: datetime
    ) -> None:
        cursor = connection.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (to_utc_iso(at), str(task_id))
        )
        if cursor.rowcount != 1:  # pragma: no cover - the caller just checked the row
            raise TaskNotFound(task_id)


def _task_parameters(task: Task) -> tuple[object, ...]:
    return (
        str(task.id),
        task.title,
        task.description,
        str(task.status),
        str(task.priority),
        task.estimated_minutes,
        to_utc_iso(task.created_at),
        to_utc_iso(task.updated_at),
        _optional_iso(task.completed_at),
        _optional_iso(task.cancelled_at),
    )


def _deadline_parameters(deadline: Deadline) -> tuple[object, ...]:
    return (
        str(deadline.id),
        str(deadline.task_id),
        to_utc_iso(deadline.due_at),
        to_utc_iso(deadline.created_at),
        to_utc_iso(deadline.updated_at),
    )


def _event_parameters(event: CalendarEvent) -> tuple[object, ...]:
    return (
        str(event.id),
        event.title,
        event.description,
        to_utc_iso(event.starts_at),
        to_utc_iso(event.ends_at),
        to_utc_iso(event.created_at),
        to_utc_iso(event.updated_at),
        _optional_iso(event.cancelled_at),
    )


def _block_parameters(block: PlanBlock) -> tuple[object, ...]:
    return (
        str(block.id),
        str(block.task_id),
        to_utc_iso(block.starts_at),
        to_utc_iso(block.ends_at),
        to_utc_iso(block.created_at),
        to_utc_iso(block.updated_at),
        _optional_iso(block.cancelled_at),
        str(block.origin),
        None if block.proposal_id is None else str(block.proposal_id),
    )


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else from_utc_iso(str(value))


def _row_to_task(row: sqlite3.Row) -> Task:
    try:
        return Task(
            id=UUID(str(row["id"])),
            title=str(row["title"]),
            description=None if row["description"] is None else str(row["description"]),
            status=TaskStatus(str(row["status"])),
            priority=TaskPriority(str(row["priority"])),
            estimated_minutes=(
                None if row["estimated_minutes"] is None else int(row["estimated_minutes"])
            ),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            completed_at=_optional_datetime(row["completed_at"]),
            cancelled_at=_optional_datetime(row["cancelled_at"]),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored task is not readable: {exc}") from exc


def _row_to_deadline(row: sqlite3.Row) -> Deadline:
    try:
        return Deadline(
            id=UUID(str(row["id"])),
            task_id=UUID(str(row["task_id"])),
            due_at=from_utc_iso(str(row["due_at"])),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored deadline is not readable: {exc}") from exc


def _row_to_calendar_event(row: sqlite3.Row) -> CalendarEvent:
    try:
        return CalendarEvent(
            id=UUID(str(row["id"])),
            title=str(row["title"]),
            description=None if row["description"] is None else str(row["description"]),
            starts_at=from_utc_iso(str(row["starts_at"])),
            ends_at=from_utc_iso(str(row["ends_at"])),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            cancelled_at=_optional_datetime(row["cancelled_at"]),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored calendar event is not readable: {exc}") from exc


def _row_to_plan_block(row: sqlite3.Row) -> PlanBlock:
    try:
        return PlanBlock(
            id=UUID(str(row["id"])),
            task_id=UUID(str(row["task_id"])),
            starts_at=from_utc_iso(str(row["starts_at"])),
            ends_at=from_utc_iso(str(row["ends_at"])),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            cancelled_at=_optional_datetime(row["cancelled_at"]),
            origin=PlanBlockOrigin(str(row["origin"])),
            proposal_id=(
                None if row["proposal_id"] is None else UUID(str(row["proposal_id"]))
            ),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored plan block is not readable: {exc}") from exc


def _translate_integrity_error(error: sqlite3.IntegrityError) -> Exception:
    message = str(error)
    if "FOREIGN KEY" in message.upper():
        return TaskNotFound("referenced task")
    if "UNIQUE" in message.upper():
        return DuplicateCommitment(message)
    return CommitmentStoreError(f"commitment write rejected by the database: {error}")


__all__ = ["SqliteCommitmentRepository"]
