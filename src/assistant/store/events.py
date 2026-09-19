"""SQLite implementation of the `EventRepository` port (ADR-0002, ADR-0003, ADR-0008).

Structure of every operation: a thin `async` public method delegates to a private
blocking `_*_sync` method with `asyncio.to_thread`. The blocking half opens its own
connection through `Database.connect()`, so connection creation, use and close all happen
inside the same worker thread (ADR-0009).

Deduplication and transition atomicity remain database properties: a partial unique index
rejects reused `(source, external_id)` identities, and every transition runs inside one
`BEGIN IMMEDIATE` transaction that re-checks the status it believed the event had.
"""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.errors import (
    DomainError,
    DuplicateInboundEvent,
    EventNotFound,
    InvalidEventTransition,
    InvalidInboundEvent,
    UnexpectedEventStatus,
)
from assistant.domain.inbound_event import PENDING_STATUSES, EventId, EventStatus, InboundEvent
from assistant.ports.clock import Clock
from assistant.store.db import Database, transaction
from assistant.store.errors import StoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_COLUMNS = (
    "id, source, external_id, event_type, content, received_at, status, attempts, last_error"
)

_INSERT_SQL = f"""
INSERT INTO inbound_events (
    {_COLUMNS}, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_ID_SQL = f"SELECT {_COLUMNS} FROM inbound_events WHERE id = ?"

_SELECT_BY_IDENTITY_SQL = (
    f"SELECT {_COLUMNS} FROM inbound_events WHERE source = ? AND external_id = ?"
)

_UPDATE_STATUS_SQL = """
UPDATE inbound_events
   SET status = ?, attempts = ?, last_error = ?, updated_at = ?
 WHERE id = ? AND status = ?
"""


class SqliteEventRepository:
    """Durable `InboundEvent` storage backed by SQLite.

    Every blocking operation happens in a worker thread with a dedicated connection; no
    `sqlite3.Connection` is ever stored on the instance.
    """

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def add(self, event: InboundEvent) -> InboundEvent:
        """Persist a new event.

        Raises:
            DuplicateInboundEvent: the `(source, external_id)` identity already exists.
            StoreError: any other persistence failure.
        """
        return await asyncio.to_thread(self._add_sync, event)

    async def get(self, event_id: EventId) -> InboundEvent | None:
        """Return the stored event, or `None` when it does not exist."""
        return await asyncio.to_thread(self._get_sync, event_id)

    async def get_by_external_identity(
        self, source: str, external_id: str
    ) -> InboundEvent | None:
        """Return the event stored for `(source, external_id)`, or `None`.

        Raises:
            ValueError: `external_id` is empty or blank — `None` is not an identity.
        """
        if not external_id.strip():
            raise ValueError("external_id must be a non-empty string")
        return await asyncio.to_thread(self._get_by_external_identity_sync, source, external_id)

    async def list_pending(self, *, limit: int) -> list[InboundEvent]:
        """Return `RECEIVED` and `FAILED` events, oldest first.

        `limit` must be positive; a non-positive limit is a programming error and raises
        `ValueError` rather than silently returning an empty list.
        """
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        return await asyncio.to_thread(self._list_pending_sync, limit)

    async def transition(
        self,
        event_id: EventId,
        *,
        expected: EventStatus,
        target: EventStatus,
        error: str | None = None,
    ) -> InboundEvent:
        """Atomically move an event from `expected` to `target`."""
        return await asyncio.to_thread(self._transition_sync, event_id, expected, target, error)

    def _add_sync(self, event: InboundEvent) -> InboundEvent:
        now = to_utc_iso(self._clock.now())
        with self._database.connect() as connection:
            try:
                connection.execute(
                    _INSERT_SQL,
                    (
                        str(event.id),
                        event.source,
                        event.external_id,
                        event.event_type,
                        event.content,
                        to_utc_iso(event.received_at),
                        str(event.status),
                        event.attempts,
                        event.last_error,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise self._translate_integrity_error(connection, event, exc) from exc
        return event

    def _get_sync(self, event_id: EventId) -> InboundEvent | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_BY_ID_SQL, (str(event_id),)).fetchone()
        return None if row is None else _row_to_event(row)

    def _get_by_external_identity_sync(
        self, source: str, external_id: str
    ) -> InboundEvent | None:
        with self._database.connect() as connection:
            row = connection.execute(
                _SELECT_BY_IDENTITY_SQL, (source, external_id)
            ).fetchone()
        return None if row is None else _row_to_event(row)

    def _list_pending_sync(self, limit: int) -> list[InboundEvent]:
        statuses = sorted(str(status) for status in PENDING_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM inbound_events "
                f"WHERE status IN ({placeholders}) "
                "ORDER BY received_at, id LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def _transition_sync(
        self,
        event_id: EventId,
        expected: EventStatus,
        target: EventStatus,
        error: str | None,
    ) -> InboundEvent:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(_SELECT_BY_ID_SQL, (str(event_id),)).fetchone()
            if row is None:
                raise EventNotFound(event_id)
            current = _row_to_event(row)
            if current.status is not expected:
                raise UnexpectedEventStatus(event_id, expected, current.status)
            updated = current.transition_to(target, error=error)
            cursor = connection.execute(
                _UPDATE_STATUS_SQL,
                (
                    str(updated.status),
                    updated.attempts,
                    updated.last_error,
                    to_utc_iso(self._clock.now()),
                    str(event_id),
                    str(expected),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"inbound event {event_id} changed while it was being updated")
        return updated

    def _translate_integrity_error(
        self, connection: sqlite3.Connection, event: InboundEvent, error: sqlite3.IntegrityError
    ) -> DomainError | StoreError:
        """Classify a constraint failure by reading the database, not the error text."""
        if event.external_id is not None:
            existing = connection.execute(
                "SELECT id FROM inbound_events WHERE source = ? AND external_id = ?",
                (event.source, event.external_id),
            ).fetchone()
            if existing is not None:
                return DuplicateInboundEvent(event.source, event.external_id)
        return StoreError(f"could not persist inbound event {event.id}: {error}")


def _row_to_event(row: sqlite3.Row) -> InboundEvent:
    try:
        return InboundEvent(
            id=UUID(str(row["id"])),
            source=str(row["source"]),
            external_id=None if row["external_id"] is None else str(row["external_id"]),
            event_type=str(row["event_type"]),
            content=None if row["content"] is None else str(row["content"]),
            received_at=from_utc_iso(str(row["received_at"])),
            status=EventStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
        )
    except (ValueError, InvalidInboundEvent, InvalidEventTransition, StoreError) as exc:
        raise StoreError(f"stored inbound event is not readable: {exc}") from exc


__all__ = ["SqliteEventRepository"]

