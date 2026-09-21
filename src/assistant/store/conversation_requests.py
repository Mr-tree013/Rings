"""SQLite implementation of the durable accepted-input queue (Phase 11A, ADR-0041).

Two statements in this file are load-bearing rather than incidental:

* `accept` inserts inside `ON CONFLICT DO NOTHING` and then reads back the surviving row, so two
  concurrent POSTs with the same client id produce one row and both callers get the same answer;
* `claim_next` does its select and its update inside one `BEGIN IMMEDIATE`, and refuses to claim
  while the thread already has a `PROCESSING` row, so "one active request per thread" is a property
  of the database rather than of a worker's discipline.

Every timestamp comes from the caller (`at=`), exactly as in the rest of the store layer: this
module has no clock and never invents an instant. Nothing here reads or writes model text, prompts
or provider responses. `input_text` is the user's own words, and it is cleared the moment the
durable conversation message exists and the request is terminal.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from uuid import UUID

from assistant.domain.conversation_request import (
    ConversationProgressStage,
    ConversationRequest,
    ConversationRequestId,
    ConversationRequestStatus,
)
from assistant.domain.errors import (
    ConversationRequestNotFound,
    InvalidConversationRequest,
)
from assistant.store.db import Database, transaction
from assistant.store.serialization import from_utc_iso, to_utc_iso

REQUEST_FIELDS = (
    "id, thread_id, client_request_id, input_text, status, stage, turn_id, error_code, "
    "cancel_requested_at, created_at, started_at, finished_at"
)


class SqliteConversationRequestRepository:
    """The durable queue of accepted conversation input, backed by the runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ reading

    async def get(self, request_id: ConversationRequestId) -> ConversationRequest | None:
        return await asyncio.to_thread(self._get_sync, request_id)

    async def get_by_client_id(
        self, thread_id: UUID, client_request_id: str
    ) -> ConversationRequest | None:
        return await asyncio.to_thread(self._get_by_client_id_sync, thread_id, client_request_id)

    async def list_for_thread(
        self, thread_id: UUID, *, limit: int = 50
    ) -> list[ConversationRequest]:
        return await asyncio.to_thread(self._list_for_thread_sync, thread_id, limit)

    async def list_by_status(
        self, status: ConversationRequestStatus, *, limit: int = 100
    ) -> list[ConversationRequest]:
        return await asyncio.to_thread(self._list_by_status_sync, status, limit)

    # ------------------------------------------------------------------ accepting

    async def accept(self, request: ConversationRequest) -> tuple[ConversationRequest, bool]:
        return await asyncio.to_thread(self._accept_sync, request)

    async def claim_next(
        self, *, at: datetime, thread_id: UUID | None = None
    ) -> ConversationRequest | None:
        return await asyncio.to_thread(self._claim_next_sync, thread_id, at)

    # ------------------------------------------------------------------ transitions

    async def set_stage(
        self, request_id: ConversationRequestId, stage: ConversationProgressStage
    ) -> ConversationRequest:
        return await asyncio.to_thread(self._set_stage_sync, request_id, stage)

    async def attach_turn(
        self, request_id: ConversationRequestId, turn_id: UUID
    ) -> ConversationRequest:
        return await asyncio.to_thread(self._attach_turn_sync, request_id, turn_id)

    async def request_cancel(
        self, request_id: ConversationRequestId, *, at: datetime
    ) -> ConversationRequest:
        return await asyncio.to_thread(self._request_cancel_sync, request_id, at)

    async def complete(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        stage: ConversationProgressStage | None = None,
        error_code: str | None = None,
    ) -> ConversationRequest:
        return await asyncio.to_thread(
            self._finish_sync,
            request_id,
            ConversationRequestStatus.COMPLETED,
            at,
            stage,
            error_code,
        )

    async def fail(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        error_code: str | None,
        stage: ConversationProgressStage | None = None,
    ) -> ConversationRequest:
        return await asyncio.to_thread(
            self._finish_sync,
            request_id,
            ConversationRequestStatus.FAILED,
            at,
            stage,
            error_code,
        )

    async def cancel(
        self, request_id: ConversationRequestId, *, at: datetime
    ) -> ConversationRequest:
        return await asyncio.to_thread(
            self._finish_sync,
            request_id,
            ConversationRequestStatus.CANCELLED,
            at,
            None,
            None,
        )

    async def interrupt(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        error_code: str | None = None,
        stage: ConversationProgressStage | None = None,
    ) -> ConversationRequest:
        return await asyncio.to_thread(
            self._finish_sync,
            request_id,
            ConversationRequestStatus.INTERRUPTED,
            at,
            stage,
            error_code,
        )

    async def recover(self, *, at: datetime) -> tuple[ConversationRequest, ...]:
        return await asyncio.to_thread(self._recover_sync, at)

    # ------------------------------------------------------------------ reading (sync)

    def _get_sync(self, request_id: ConversationRequestId) -> ConversationRequest | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
        return None if row is None else row_to_request(row)

    def _get_by_client_id_sync(
        self, thread_id: UUID, client_request_id: str
    ) -> ConversationRequest | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests "
                "WHERE thread_id = ? AND client_request_id = ?",
                (str(thread_id), client_request_id),
            ).fetchone()
        return None if row is None else row_to_request(row)

    def _list_for_thread_sync(self, thread_id: UUID, limit: int) -> list[ConversationRequest]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE thread_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (str(thread_id), limit),
            ).fetchall()
        return [row_to_request(row) for row in rows]

    def _list_by_status_sync(
        self, status: ConversationRequestStatus, limit: int
    ) -> list[ConversationRequest]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE status = ? "
                "ORDER BY created_at, id LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [row_to_request(row) for row in rows]

    # ------------------------------------------------------------------ accepting (sync)

    def _accept_sync(self, request: ConversationRequest) -> tuple[ConversationRequest, bool]:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                f"INSERT INTO conversation_requests ({REQUEST_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (thread_id, client_request_id) DO NOTHING",
                _request_values(request),
            )
            if cursor.rowcount == 1:
                return request, True
            # The pair was already accepted: the *stored* row is the answer, unchanged. A retried
            # POST therefore cannot become a second turn, whatever else it carries.
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests "
                "WHERE thread_id = ? AND client_request_id = ?",
                (str(request.thread_id), request.client_request_id),
            ).fetchone()
        if row is None:  # pragma: no cover - the conflict target guarantees this cannot happen
            raise InvalidConversationRequest(
                "the accepted request could not be read back after a duplicate insert"
            )
        return row_to_request(row), False

    def _claim_next_sync(
        self, thread_id: UUID | None, at: datetime
    ) -> ConversationRequest | None:
        parameters: tuple[object, ...] = ()
        thread_filter = ""
        if thread_id is not None:
            thread_filter = "AND thread_id = ?"
            parameters = (str(thread_id),)
        query = (
            f"SELECT {REQUEST_FIELDS} FROM conversation_requests "
            f"WHERE status = 'queued' {thread_filter} "
            "AND thread_id NOT IN "
            "(SELECT thread_id FROM conversation_requests WHERE status = 'processing') "
            "ORDER BY created_at, id LIMIT 1"
        )
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return None
            claimed = row_to_request(row)
            cursor = connection.execute(
                "UPDATE conversation_requests SET status = 'processing', stage = ?, started_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (
                    ConversationProgressStage.UNDERSTANDING.value,
                    to_utc_iso(at),
                    str(claimed.id),
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - the row was just selected as queued
                raise InvalidConversationRequest(f"request {claimed.id} could not be claimed")
        return ConversationRequest(
            id=claimed.id,
            thread_id=claimed.thread_id,
            client_request_id=claimed.client_request_id,
            input_text=claimed.input_text,
            status=ConversationRequestStatus.PROCESSING,
            stage=ConversationProgressStage.UNDERSTANDING,
            turn_id=claimed.turn_id,
            error_code=claimed.error_code,
            cancel_requested_at=claimed.cancel_requested_at,
            created_at=claimed.created_at,
            started_at=at,
            finished_at=None,
        )

    # -------------------------------------------------------------- transitions (sync)

    def _set_stage_sync(
        self, request_id: ConversationRequestId, stage: ConversationProgressStage
    ) -> ConversationRequest:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE conversation_requests SET stage = ? WHERE id = ? AND status = 'processing'",
                (stage.value, str(request_id)),
            )
            if cursor.rowcount != 1:
                raise _not_processing(request_id, connection)
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
        assert row is not None  # the update above proved the row exists
        return row_to_request(row)

    def _attach_turn_sync(
        self, request_id: ConversationRequestId, turn_id: UUID
    ) -> ConversationRequest:
        with self._database.connect() as connection, transaction(connection):
            request_cursor = connection.execute(
                "UPDATE conversation_requests SET turn_id = ? WHERE id = ? AND turn_id IS NULL",
                (str(turn_id), str(request_id)),
            )
            if request_cursor.rowcount != 1:
                raise InvalidConversationRequest(f"request {request_id} is already bound to a turn")
            turn_cursor = connection.execute(
                "UPDATE conversation_turns SET request_id = ? WHERE id = ? AND request_id IS NULL",
                (str(request_id), str(turn_id)),
            )
            if turn_cursor.rowcount != 1:
                raise InvalidConversationRequest(f"turn {turn_id} is already bound to a request")
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
        if row is None:  # pragma: no cover - the update above proved the row exists
            raise ConversationRequestNotFound(request_id)
        return row_to_request(row)

    def _request_cancel_sync(
        self, request_id: ConversationRequestId, at: datetime
    ) -> ConversationRequest:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise ConversationRequestNotFound(request_id)
            current = row_to_request(row)
            if not current.is_terminal and current.cancel_requested_at is None:
                connection.execute(
                    "UPDATE conversation_requests SET cancel_requested_at = ? "
                    "WHERE id = ? AND cancel_requested_at IS NULL",
                    (to_utc_iso(at), str(request_id)),
                )
                row = connection.execute(
                    f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                    (str(request_id),),
                ).fetchone()
        assert row is not None  # the row was read inside this transaction
        return row_to_request(row)

    def _finish_sync(
        self,
        request_id: ConversationRequestId,
        status: ConversationRequestStatus,
        at: datetime,
        stage: ConversationProgressStage | None,
        error_code: str | None,
    ) -> ConversationRequest:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise ConversationRequestNotFound(request_id)
            current = row_to_request(row)
            if current.is_terminal:
                # A retried transition is not a second effect: the terminal row is the answer.
                return current
            # The user's words are dropped from the queue row exactly when they are already safe
            # somewhere else: the durable conversation message exists and the request is over.
            connection.execute(
                "UPDATE conversation_requests SET status = ?, stage = ?, error_code = ?, "
                "finished_at = ?, "
                "input_text = CASE WHEN turn_id IS NULL THEN input_text ELSE NULL END "
                "WHERE id = ? AND status IN ('queued', 'processing')",
                (
                    status.value,
                    None if stage is None else stage.value,
                    error_code,
                    to_utc_iso(at),
                    str(request_id),
                ),
            )
            row = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
        assert row is not None  # the row was read inside this transaction
        return row_to_request(row)

    # ------------------------------------------------------------------ recovery (sync)

    def _recover_sync(self, at: datetime) -> tuple[ConversationRequest, ...]:
        """Fail-closed resolution of every request a previous process left `PROCESSING`.

        The only evidence this build accepts is a *linked, durable turn row*. A terminal turn is
        mirrored; a turn parked waiting for a human is finished as it is (nothing is pending, and
        the pending offer is itself durable); anything else — including a claim with no turn at all
        — becomes `INTERRUPTED`. No evidence means no rerun.
        """
        with self._database.connect() as connection, transaction(connection):
            rows = connection.execute(
                f"SELECT {REQUEST_FIELDS} FROM conversation_requests "
                "WHERE status = 'processing' ORDER BY created_at, id"
            ).fetchall()
            if not rows:
                return ()
            resolved: list[ConversationRequest] = []
            for row in rows:
                request = row_to_request(row)
                status, stage = _recovery_outcome(connection, request)
                connection.execute(
                    "UPDATE conversation_requests SET status = ?, stage = ?, error_code = ?, "
                    "finished_at = ?, "
                    "input_text = CASE WHEN turn_id IS NULL THEN input_text ELSE NULL END "
                    "WHERE id = ? AND status = 'processing'",
                    (
                        status.value,
                        None if stage is None else stage.value,
                        None if status is ConversationRequestStatus.COMPLETED else "INTERRUPTED",
                        to_utc_iso(at),
                        str(request.id),
                    ),
                )
                updated = connection.execute(
                    f"SELECT {REQUEST_FIELDS} FROM conversation_requests WHERE id = ?",
                    (str(request.id),),
                ).fetchone()
                assert updated is not None  # the row was read inside this transaction
                resolved.append(row_to_request(updated))
        return tuple(resolved)


def _recovery_outcome(
    connection: sqlite3.Connection, request: ConversationRequest
) -> tuple[ConversationRequestStatus, ConversationProgressStage | None]:
    """What a crashed `PROCESSING` request becomes, from its linked turn and nothing else."""
    if request.turn_id is None:
        # Claimed, but no durable evidence that any application work began. Fail closed anyway: a
        # rerun could duplicate a side effect this build cannot see.
        return ConversationRequestStatus.INTERRUPTED, request.stage
    row = connection.execute(
        "SELECT status FROM conversation_turns WHERE id = ?", (str(request.turn_id),)
    ).fetchone()
    if row is None:  # pragma: no cover - the foreign key guarantees the turn exists
        return ConversationRequestStatus.INTERRUPTED, request.stage
    turn_status = str(row["status"])
    if turn_status == "waiting_confirmation":
        return ConversationRequestStatus.COMPLETED, ConversationProgressStage.WAITING_CONFIRMATION
    if turn_status == "completed":
        return ConversationRequestStatus.COMPLETED, request.stage
    if turn_status == "failed":
        return ConversationRequestStatus.FAILED, request.stage
    return ConversationRequestStatus.INTERRUPTED, ConversationProgressStage.UNDERSTANDING


def _not_processing(
    request_id: ConversationRequestId, connection: sqlite3.Connection
) -> Exception:
    row = connection.execute(
        "SELECT status FROM conversation_requests WHERE id = ?", (str(request_id),)
    ).fetchone()
    if row is None:
        return ConversationRequestNotFound(request_id)
    return InvalidConversationRequest(f"request {request_id} is {row['status']}, not processing")


def _request_values(request: ConversationRequest) -> tuple[object, ...]:
    return (
        str(request.id),
        str(request.thread_id),
        request.client_request_id,
        request.input_text,
        request.status.value,
        None if request.stage is None else request.stage.value,
        None if request.turn_id is None else str(request.turn_id),
        request.error_code,
        None
        if request.cancel_requested_at is None
        else to_utc_iso(request.cancel_requested_at),
        to_utc_iso(request.created_at),
        None if request.started_at is None else to_utc_iso(request.started_at),
        None if request.finished_at is None else to_utc_iso(request.finished_at),
    )


def row_to_request(row: sqlite3.Row | tuple[object, ...]) -> ConversationRequest:
    """Rebuild one request from its row."""
    return ConversationRequest(
        id=UUID(str(row[0])),
        thread_id=UUID(str(row[1])),
        client_request_id=str(row[2]),
        input_text=None if row[3] is None else str(row[3]),
        status=ConversationRequestStatus(str(row[4])),
        stage=None if row[5] is None else ConversationProgressStage(str(row[5])),
        turn_id=None if row[6] is None else UUID(str(row[6])),
        error_code=None if row[7] is None else str(row[7]),
        cancel_requested_at=None if row[8] is None else from_utc_iso(str(row[8])),
        created_at=from_utc_iso(str(row[9])),
        started_at=None if row[10] is None else from_utc_iso(str(row[10])),
        finished_at=None if row[11] is None else from_utc_iso(str(row[11])),
    )


__all__ = ["SqliteConversationRequestRepository", "row_to_request"]
