"""SQLite implementation of the conversation repository (Phase 10A, ADR-0033).

Everything durable about a conversation lives here: threads, messages, turns and the operations
the runtime was allowed to perform. Two properties in this file are load-bearing rather than
incidental:

- **Order is explicit.** A turn is written before its operations, and an operation is written as
  `APPLYING` *before* the service that mutates local state is called. The crash fence depends on
  that order, so no method here batches two of those facts into one write.
- **Arguments are data.** `arguments_json` is written from the typed argument object and read back
  through `build_arguments`, which rejects unknown types, unknown keys and wrong types. A row that
  cannot be decoded is a hard error, never a partially reconstructed operation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from uuid import UUID

from assistant.domain.conversation import (
    ConversationMessage,
    ConversationMessageId,
    ConversationMessageRole,
    ConversationOperation,
    ConversationOperationId,
    ConversationOperationStatus,
    ConversationThread,
    ConversationThreadId,
    ConversationThreadStatus,
    ConversationTurn,
    ConversationTurnId,
    ConversationTurnStatus,
)
from assistant.domain.conversation_plan import (
    ConversationOperationType,
    arguments_payload,
    build_arguments,
)
from assistant.domain.errors import (
    ConversationOperationNotFound,
    ConversationThreadNotFound,
    InvalidConversationOperation,
)
from assistant.store.db import Database, transaction
from assistant.store.serialization import from_utc_iso, to_utc_iso

THREAD_FIELDS = "id, title, status, created_at, updated_at, archived_at"
MESSAGE_FIELDS = "id, thread_id, role, text, created_at"
TURN_FIELDS = (
    "id, thread_id, user_message_id, assistant_message_id, interpreter_version, "
    "context_fingerprint, status, created_at, completed_at"
)
OPERATION_FIELDS = (
    "id, turn_id, ordinal, operation_type, arguments_json, operation_fingerprint, status, "
    "result_kind, result_ref, confirmation_expires_at, created_at, updated_at"
)


class SqliteConversationRepository:
    """Durable conversation history, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # -------------------------------------------------------------------------- threads

    async def add_thread(self, thread: ConversationThread) -> ConversationThread:
        return await asyncio.to_thread(self._add_thread_sync, thread)

    async def update_thread(self, thread: ConversationThread) -> ConversationThread:
        return await asyncio.to_thread(self._update_thread_sync, thread)

    async def get_thread(self, thread_id: ConversationThreadId) -> ConversationThread | None:
        return await asyncio.to_thread(self._get_thread_sync, thread_id)

    async def latest_active_thread(self) -> ConversationThread | None:
        return await asyncio.to_thread(self._latest_active_thread_sync)

    async def list_threads(self, *, limit: int = 20) -> list[ConversationThread]:
        return await asyncio.to_thread(self._list_threads_sync, limit)

    # ------------------------------------------------------------------------- messages

    async def add_message(self, message: ConversationMessage) -> ConversationMessage:
        return await asyncio.to_thread(self._add_message_sync, message)

    async def get_message(self, message_id: ConversationMessageId) -> ConversationMessage | None:
        return await asyncio.to_thread(self._get_message_sync, message_id)

    async def list_messages(
        self, thread_id: ConversationThreadId, *, limit: int | None = None
    ) -> list[ConversationMessage]:
        return await asyncio.to_thread(self._list_messages_sync, thread_id, limit)

    # ---------------------------------------------------------------------------- turns

    async def add_turn(self, turn: ConversationTurn) -> ConversationTurn:
        return await asyncio.to_thread(self._add_turn_sync, turn)

    async def update_turn(self, turn: ConversationTurn) -> ConversationTurn:
        return await asyncio.to_thread(self._update_turn_sync, turn)

    async def get_turn(self, turn_id: ConversationTurnId) -> ConversationTurn | None:
        return await asyncio.to_thread(self._get_turn_sync, turn_id)

    async def list_turns(
        self, thread_id: ConversationThreadId, *, limit: int | None = None
    ) -> list[ConversationTurn]:
        return await asyncio.to_thread(self._list_turns_sync, thread_id, limit)

    async def turns_waiting_for_confirmation(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationTurn]:
        return await asyncio.to_thread(self._turns_waiting_for_confirmation_sync, thread_id)

    # ----------------------------------------------------------------------- operations

    async def add_operation(self, operation: ConversationOperation) -> ConversationOperation:
        return await asyncio.to_thread(self._add_operation_sync, operation)

    async def update_operation(self, operation: ConversationOperation) -> ConversationOperation:
        return await asyncio.to_thread(self._update_operation_sync, operation)

    async def get_operation(
        self, operation_id: ConversationOperationId
    ) -> ConversationOperation | None:
        return await asyncio.to_thread(self._get_operation_sync, operation_id)

    async def list_operations(self, turn_id: ConversationTurnId) -> list[ConversationOperation]:
        return await asyncio.to_thread(self._list_operations_sync, turn_id)

    async def operations_waiting_for_confirmation(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationOperation]:
        return await asyncio.to_thread(self._operations_waiting_sync, thread_id)

    async def list_operations_by_status(
        self, status: ConversationOperationStatus, *, limit: int = 100
    ) -> list[ConversationOperation]:
        return await asyncio.to_thread(self._list_operations_by_status_sync, status, limit)

    # ------------------------------------------------------------------ threads (sync)

    def _add_thread_sync(self, thread: ConversationThread) -> ConversationThread:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO conversation_threads ({THREAD_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(thread.id),
                    thread.title,
                    thread.status.value,
                    to_utc_iso(thread.created_at),
                    to_utc_iso(thread.updated_at),
                    None if thread.archived_at is None else to_utc_iso(thread.archived_at),
                ),
            )
        return thread

    def _update_thread_sync(self, thread: ConversationThread) -> ConversationThread:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE conversation_threads SET title = ?, status = ?, updated_at = ?, "
                "archived_at = ? WHERE id = ?",
                (
                    thread.title,
                    thread.status.value,
                    to_utc_iso(thread.updated_at),
                    None if thread.archived_at is None else to_utc_iso(thread.archived_at),
                    str(thread.id),
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationThreadNotFound(thread.id)
        return thread

    def _get_thread_sync(self, thread_id: ConversationThreadId) -> ConversationThread | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {THREAD_FIELDS} FROM conversation_threads WHERE id = ?",
                (str(thread_id),),
            ).fetchone()
        return None if row is None else row_to_thread(row)

    def _latest_active_thread_sync(self) -> ConversationThread | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {THREAD_FIELDS} FROM conversation_threads WHERE status = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (ConversationThreadStatus.ACTIVE.value,),
            ).fetchone()
        return None if row is None else row_to_thread(row)

    def _list_threads_sync(self, limit: int) -> list[ConversationThread]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {THREAD_FIELDS} FROM conversation_threads "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_thread(row) for row in rows]

    # ----------------------------------------------------------------- messages (sync)

    def _add_message_sync(self, message: ConversationMessage) -> ConversationMessage:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO conversation_messages ({MESSAGE_FIELDS}) VALUES (?, ?, ?, ?, ?)",
                (
                    str(message.id),
                    str(message.thread_id),
                    message.role.value,
                    message.text,
                    to_utc_iso(message.created_at),
                ),
            )
        return message

    def _get_message_sync(
        self, message_id: ConversationMessageId
    ) -> ConversationMessage | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {MESSAGE_FIELDS} FROM conversation_messages WHERE id = ?",
                (str(message_id),),
            ).fetchone()
        return None if row is None else row_to_message(row)

    def _list_messages_sync(
        self, thread_id: ConversationThreadId, limit: int | None
    ) -> list[ConversationMessage]:
        query = (
            f"SELECT {MESSAGE_FIELDS} FROM conversation_messages WHERE thread_id = ? "
            "ORDER BY created_at, rowid"
        )
        parameters: tuple[object, ...] = (str(thread_id),)
        if limit is not None:
            # Keep the newest `limit`, then restore reading order.
            query = (
                f"SELECT * FROM (SELECT {MESSAGE_FIELDS} FROM conversation_messages "
                "WHERE thread_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?) "
                "ORDER BY created_at, rowid"
            )
            parameters = (str(thread_id), limit)
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [row_to_message(row) for row in rows]

    # -------------------------------------------------------------------- turns (sync)

    def _add_turn_sync(self, turn: ConversationTurn) -> ConversationTurn:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO conversation_turns ({TURN_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(turn.id),
                    str(turn.thread_id),
                    str(turn.user_message_id),
                    None if turn.assistant_message_id is None else str(turn.assistant_message_id),
                    turn.interpreter_version,
                    turn.context_fingerprint,
                    turn.status.value,
                    to_utc_iso(turn.created_at),
                    None if turn.completed_at is None else to_utc_iso(turn.completed_at),
                ),
            )
        return turn

    def _update_turn_sync(self, turn: ConversationTurn) -> ConversationTurn:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE conversation_turns SET assistant_message_id = ?, status = ?, "
                "completed_at = ? WHERE id = ?",
                (
                    None if turn.assistant_message_id is None else str(turn.assistant_message_id),
                    turn.status.value,
                    None if turn.completed_at is None else to_utc_iso(turn.completed_at),
                    str(turn.id),
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidConversationOperation(f"no such turn: {turn.id}")
        return turn

    def _get_turn_sync(self, turn_id: ConversationTurnId) -> ConversationTurn | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {TURN_FIELDS} FROM conversation_turns WHERE id = ?", (str(turn_id),)
            ).fetchone()
        return None if row is None else row_to_turn(row)

    def _list_turns_sync(
        self, thread_id: ConversationThreadId, limit: int | None
    ) -> list[ConversationTurn]:
        query = (
            f"SELECT {TURN_FIELDS} FROM conversation_turns WHERE thread_id = ? "
            "ORDER BY created_at, rowid"
        )
        parameters: tuple[object, ...] = (str(thread_id),)
        if limit is not None:
            query = (
                f"SELECT * FROM (SELECT {TURN_FIELDS} FROM conversation_turns "
                "WHERE thread_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?) "
                "ORDER BY created_at, rowid"
            )
            parameters = (str(thread_id), limit)
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [row_to_turn(row) for row in rows]

    def _turns_waiting_for_confirmation_sync(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationTurn]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {TURN_FIELDS} FROM conversation_turns "
                "WHERE thread_id = ? AND status = ? ORDER BY created_at, rowid",
                (str(thread_id), ConversationTurnStatus.WAITING_CONFIRMATION.value),
            ).fetchall()
        return [row_to_turn(row) for row in rows]

    # --------------------------------------------------------------- operations (sync)

    def _add_operation_sync(self, operation: ConversationOperation) -> ConversationOperation:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO conversation_operations ({OPERATION_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _operation_values(operation),
            )
        return operation

    def _update_operation_sync(self, operation: ConversationOperation) -> ConversationOperation:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE conversation_operations SET status = ?, result_kind = ?, result_ref = ?, "
                "confirmation_expires_at = ?, updated_at = ? WHERE id = ?",
                (
                    operation.status.value,
                    operation.result_kind,
                    operation.result_ref,
                    None
                    if operation.confirmation_expires_at is None
                    else to_utc_iso(operation.confirmation_expires_at),
                    to_utc_iso(operation.updated_at),
                    str(operation.id),
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationOperationNotFound(operation.id)
        return operation

    def _get_operation_sync(
        self, operation_id: ConversationOperationId
    ) -> ConversationOperation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {OPERATION_FIELDS} FROM conversation_operations WHERE id = ?",
                (str(operation_id),),
            ).fetchone()
        return None if row is None else row_to_operation(row)

    def _list_operations_sync(self, turn_id: ConversationTurnId) -> list[ConversationOperation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {OPERATION_FIELDS} FROM conversation_operations WHERE turn_id = ? "
                "ORDER BY ordinal, id",
                (str(turn_id),),
            ).fetchall()
        return [row_to_operation(row) for row in rows]

    def _operations_waiting_sync(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationOperation]:
        return self._operations_of_thread_sync(
            thread_id, (ConversationOperationStatus.WAITING_CONFIRMATION,)
        )

    def _list_operations_by_status_sync(
        self, status: ConversationOperationStatus, limit: int
    ) -> list[ConversationOperation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {OPERATION_FIELDS} FROM conversation_operations WHERE status = ? "
                "ORDER BY created_at, id LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [row_to_operation(row) for row in rows]

    def _operations_of_thread_sync(
        self, thread_id: ConversationThreadId, statuses: Sequence[ConversationOperationStatus]
    ) -> list[ConversationOperation]:
        placeholders = ", ".join("?" for _ in statuses)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {OPERATION_FIELDS} FROM conversation_operations "
                f"WHERE turn_id IN (SELECT id FROM conversation_turns WHERE thread_id = ?) "
                f"AND status IN ({placeholders}) ORDER BY created_at, ordinal",
                (str(thread_id), *(status.value for status in statuses)),
            ).fetchall()
        return [row_to_operation(row) for row in rows]


def _operation_values(operation: ConversationOperation) -> tuple[object, ...]:
    return (
        str(operation.id),
        str(operation.turn_id),
        operation.ordinal,
        operation.operation_type.value,
        json.dumps(
            arguments_payload(operation.arguments),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        operation.operation_fingerprint,
        operation.status.value,
        operation.result_kind,
        operation.result_ref,
        None
        if operation.confirmation_expires_at is None
        else to_utc_iso(operation.confirmation_expires_at),
        to_utc_iso(operation.created_at),
        to_utc_iso(operation.updated_at),
    )


def row_to_thread(row: sqlite3.Row | tuple[object, ...]) -> ConversationThread:
    """Rebuild one thread from its row."""
    return ConversationThread(
        id=UUID(str(row[0])),
        title=None if row[1] is None else str(row[1]),
        status=ConversationThreadStatus(str(row[2])),
        created_at=from_utc_iso(str(row[3])),
        updated_at=from_utc_iso(str(row[4])),
        archived_at=None if row[5] is None else from_utc_iso(str(row[5])),
    )


def row_to_message(row: sqlite3.Row | tuple[object, ...]) -> ConversationMessage:
    """Rebuild one message from its row."""
    return ConversationMessage(
        id=UUID(str(row[0])),
        thread_id=UUID(str(row[1])),
        role=ConversationMessageRole(str(row[2])),
        text=str(row[3]),
        created_at=from_utc_iso(str(row[4])),
    )


def row_to_turn(row: sqlite3.Row | tuple[object, ...]) -> ConversationTurn:
    """Rebuild one turn from its row."""
    return ConversationTurn(
        id=UUID(str(row[0])),
        thread_id=UUID(str(row[1])),
        user_message_id=UUID(str(row[2])),
        assistant_message_id=None if row[3] is None else UUID(str(row[3])),
        interpreter_version=str(row[4]),
        context_fingerprint=str(row[5]),
        status=ConversationTurnStatus(str(row[6])),
        created_at=from_utc_iso(str(row[7])),
        completed_at=None if row[8] is None else from_utc_iso(str(row[8])),
    )


def row_to_operation(row: sqlite3.Row | tuple[object, ...]) -> ConversationOperation:
    """Rebuild one operation from its row, re-validating its arguments."""
    operation_type = ConversationOperationType(str(row[3]))
    arguments = build_arguments(operation_type.value, json.loads(str(row[4])))
    return ConversationOperation(
        id=UUID(str(row[0])),
        turn_id=UUID(str(row[1])),
        ordinal=int(str(row[2])),
        operation_type=operation_type,
        arguments=arguments,
        operation_fingerprint=str(row[5]),
        status=ConversationOperationStatus(str(row[6])),
        result_kind=None if row[7] is None else str(row[7]),
        result_ref=None if row[8] is None else str(row[8]),
        confirmation_expires_at=None if row[9] is None else from_utc_iso(str(row[9])),
        created_at=from_utc_iso(str(row[10])),
        updated_at=from_utc_iso(str(row[11])),
    )


__all__ = [
    "SqliteConversationRepository",
    "row_to_message",
    "row_to_operation",
    "row_to_thread",
    "row_to_turn",
]
