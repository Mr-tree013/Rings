"""SQLite implementation of the `EventRepository` port (ADR-0002, ADR-0003, ADR-0009).

Structure of every operation: a thin `async` public method delegates to a private
blocking `_*_sync` method with `asyncio.to_thread`. The blocking half opens its own
connection through `Database.connect()`, so connection creation, use and close all happen
inside the same worker thread (ADR-0009).

Deduplication, claiming and fencing are database properties here, not Python bookkeeping:

- a partial unique index rejects reused `(source, external_id)` identities;
- `claim_next` selects and updates the same row inside one `BEGIN IMMEDIATE` transaction,
  so two workers can never both win;
- `complete_claim` and `fail_claim` only touch a row that is still `PROCESSING` under the
  caller's `claim_token`, which is what makes an expired worker harmless (ADR-0010).

The domain decides *what* a transition means (`InboundEvent.claimed/completed/failed/
dead_lettered`); this module only decides *when* a row may move and writes it atomically.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import (
    DomainError,
    DuplicateInboundEvent,
    EventNotFound,
    InvalidEventTransition,
    InvalidInboundEvent,
    StaleEventClaim,
    UnexpectedEventStatus,
)
from assistant.domain.event_claim import EventClaim
from assistant.domain.inbound_event import PENDING_STATUSES, EventId, EventStatus, InboundEvent
from assistant.ports.clock import Clock
from assistant.store.db import Database, transaction
from assistant.store.errors import StoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_EVENT_COLUMNS = (
    "id, source, external_id, event_type, content, received_at, status, attempts, "
    "last_error, next_attempt_at, dead_lettered_at"
)

_INSERT_SQL = f"""
INSERT INTO inbound_events (
    {_EVENT_COLUMNS}, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_ID_SQL = f"SELECT {_EVENT_COLUMNS} FROM inbound_events WHERE id = ?"

_SELECT_BY_IDENTITY_SQL = (
    f"SELECT {_EVENT_COLUMNS} FROM inbound_events WHERE source = ? AND external_id = ?"
)

_SELECT_LEASED_BY_ID_SQL = (
    f"SELECT {_EVENT_COLUMNS}, claim_token FROM inbound_events WHERE id = ?"
)

# Eligibility, in SQL, for a single claim transaction. Timestamps are fixed-width UTC ISO
# text, so plain string comparison is chronological comparison.
_CLAIM_CANDIDATE_SQL = f"""
SELECT {_EVENT_COLUMNS}
  FROM inbound_events
 WHERE status = 'RECEIVED'
    OR (status = 'FAILED' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?)
    OR (status = 'PROCESSING' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
 ORDER BY received_at ASC, id ASC
 LIMIT 1
"""

_CLAIM_UPDATE_SQL = """
UPDATE inbound_events
   SET status = 'PROCESSING',
       attempts = ?,
       claim_token = ?,
       claimed_by = ?,
       claimed_at = ?,
       lease_expires_at = ?,
       next_attempt_at = NULL,
       updated_at = ?
 WHERE id = ?
   AND (status = 'RECEIVED'
        OR (status = 'FAILED' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?)
        OR (status = 'PROCESSING' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?))
"""

_COMPLETE_SQL = """
UPDATE inbound_events
   SET status = ?, attempts = ?, last_error = NULL, next_attempt_at = NULL,
       dead_lettered_at = NULL, claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
       lease_expires_at = NULL, updated_at = ?
 WHERE id = ? AND status = 'PROCESSING' AND claim_token = ?
"""

_RETRY_SQL = """
UPDATE inbound_events
   SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?,
       dead_lettered_at = NULL, claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
       lease_expires_at = NULL, updated_at = ?
 WHERE id = ? AND status = 'PROCESSING' AND claim_token = ?
"""

_DEAD_LETTER_SQL = """
UPDATE inbound_events
   SET status = ?, attempts = ?, last_error = ?, next_attempt_at = NULL,
       dead_lettered_at = ?, claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
       lease_expires_at = NULL, updated_at = ?
 WHERE id = ? AND status = 'PROCESSING' AND claim_token = ?
"""


class SqliteEventRepository:
    """Durable `InboundEvent` storage backed by SQLite.

    Every blocking operation happens in a worker thread with a dedicated connection; no
    `sqlite3.Connection` is ever stored on the instance.
    """

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    # ------------------------------------------------------------------ ingestion

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

        Kept for compatibility and inspection. This is **not** a work-claim API: a worker
        must use `claim_next`, which cannot hand the same event to two workers.
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

    # ---------------------------------------------------------------------- worker

    async def claim_next(
        self,
        *,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> EventClaim | None:
        """Atomically take the oldest eligible event, or return `None`.

        Eligible: `RECEIVED`; `FAILED` whose retry time has arrived; `PROCESSING` whose
        lease has expired. Eligibility and the update happen in one write transaction, so
        competing workers cannot claim the same event.

        Raises:
            ValueError: `worker_id` is blank, the token is the nil UUID, the timestamps
                are naive, or the lease does not extend past `now`.
            StoreError: the selected row could not be updated (defensive).
        """
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if claim_token.int == 0:
            raise ValueError("claim_token must not be the nil UUID")
        _require_aware(now, "now")
        _require_aware(lease_expires_at, "lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be after now")
        return await asyncio.to_thread(
            self._claim_next_sync, worker_id, claim_token, now, lease_expires_at
        )

    async def complete_claim(
        self,
        event_id: EventId,
        *,
        claim_token: UUID,
        completed_at: datetime,
    ) -> InboundEvent:
        """Mark a claimed event `PROCESSED` and clear its lease.

        Raises:
            EventNotFound: no such event.
            StaleEventClaim: the event is not `PROCESSING` under this token any more.
        """
        _require_aware(completed_at, "completed_at")
        return await asyncio.to_thread(
            self._complete_claim_sync, event_id, claim_token, completed_at
        )

    async def fail_claim(
        self,
        event_id: EventId,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
        next_attempt_at: datetime | None,
        dead_letter: bool,
    ) -> InboundEvent:
        """Record a failed attempt: either schedule a retry or dead-letter the event.

        Raises:
            EventNotFound: no such event.
            StaleEventClaim: the event is not `PROCESSING` under this token any more.
            ValueError: `next_attempt_at` is missing for a retry, or set for a dead letter.
            InvalidInboundEvent: the error message is empty.
        """
        _require_aware(failed_at, "failed_at")
        if dead_letter and next_attempt_at is not None:
            raise ValueError("next_attempt_at must be None when dead-lettering an event")
        if not dead_letter:
            if next_attempt_at is None:
                raise ValueError("a retryable failure requires next_attempt_at")
            _require_aware(next_attempt_at, "next_attempt_at")
        return await asyncio.to_thread(
            self._fail_claim_sync,
            event_id,
            claim_token,
            failed_at,
            error,
            next_attempt_at,
            dead_letter,
        )

    # ------------------------------------------------------------ blocking internals

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
                        _optional_iso(event.next_attempt_at),
                        _optional_iso(event.dead_lettered_at),
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
            row = connection.execute(_SELECT_BY_IDENTITY_SQL, (source, external_id)).fetchone()
        return None if row is None else _row_to_event(row)

    def _list_pending_sync(self, limit: int) -> list[InboundEvent]:
        statuses = sorted(str(status) for status in PENDING_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM inbound_events "
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
                """
                UPDATE inbound_events
                   SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?,
                       dead_lettered_at = ?, updated_at = ?
                 WHERE id = ? AND status = ?
                """,
                (
                    str(updated.status),
                    updated.attempts,
                    updated.last_error,
                    _optional_iso(updated.next_attempt_at),
                    _optional_iso(updated.dead_lettered_at),
                    to_utc_iso(self._clock.now()),
                    str(event_id),
                    str(expected),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"inbound event {event_id} changed while it was being updated")
        return updated

    def _claim_next_sync(
        self,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> EventClaim | None:
        now_iso = to_utc_iso(now)
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(_CLAIM_CANDIDATE_SQL, (now_iso, now_iso)).fetchone()
            if row is None:
                return None
            updated = _row_to_event(row).claimed()
            cursor = connection.execute(
                _CLAIM_UPDATE_SQL,
                (
                    updated.attempts,
                    str(claim_token),
                    worker_id,
                    now_iso,
                    to_utc_iso(lease_expires_at),
                    now_iso,
                    str(updated.id),
                    now_iso,
                    now_iso,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"inbound event {updated.id} could not be claimed atomically")
            claimed_row = connection.execute(_SELECT_BY_ID_SQL, (str(updated.id),)).fetchone()
        if claimed_row is None:  # pragma: no cover - defensive
            raise StoreError(f"inbound event {updated.id} disappeared while being claimed")
        return EventClaim(
            event=_row_to_event(claimed_row),
            claim_token=claim_token,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=lease_expires_at,
        )

    def _complete_claim_sync(
        self, event_id: EventId, claim_token: UUID, completed_at: datetime
    ) -> InboundEvent:
        with self._database.connect() as connection, transaction(connection):
            current = _load_leased_event(connection, event_id, claim_token)
            updated = current.completed()
            cursor = connection.execute(
                _COMPLETE_SQL,
                (
                    str(updated.status),
                    updated.attempts,
                    to_utc_iso(completed_at),
                    str(event_id),
                    str(claim_token),
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"inbound event {event_id} could not be completed atomically")
            row = connection.execute(_SELECT_BY_ID_SQL, (str(event_id),)).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise StoreError(f"inbound event {event_id} disappeared while being completed")
        return _row_to_event(row)

    def _fail_claim_sync(
        self,
        event_id: EventId,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
        next_attempt_at: datetime | None,
        dead_letter: bool,
    ) -> InboundEvent:
        with self._database.connect() as connection, transaction(connection):
            current = _load_leased_event(connection, event_id, claim_token)
            if dead_letter:
                updated = current.dead_lettered(error=error, at=failed_at)
                statement = _DEAD_LETTER_SQL
                params: tuple[object, ...] = (
                    str(updated.status),
                    updated.attempts,
                    updated.last_error,
                    to_utc_iso(failed_at),
                    to_utc_iso(failed_at),
                    str(event_id),
                    str(claim_token),
                )
            else:
                if next_attempt_at is None:  # already rejected by the async boundary
                    raise ValueError("a retryable failure requires next_attempt_at")
                updated = current.failed(error=error, next_attempt_at=next_attempt_at)
                statement = _RETRY_SQL
                params = (
                    str(updated.status),
                    updated.attempts,
                    updated.last_error,
                    to_utc_iso(next_attempt_at),
                    to_utc_iso(failed_at),
                    str(event_id),
                    str(claim_token),
                )
            cursor = connection.execute(statement, params)
            if cursor.rowcount != 1:
                raise StoreError(f"inbound event {event_id} could not be failed atomically")
            row = connection.execute(_SELECT_BY_ID_SQL, (str(event_id),)).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise StoreError(f"inbound event {event_id} disappeared while being failed")
        return _row_to_event(row)

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


def _load_leased_event(
    connection: sqlite3.Connection, event_id: EventId, claim_token: UUID
) -> InboundEvent:
    """Read the event, insisting that the caller's claim is still the current one."""
    row = connection.execute(_SELECT_LEASED_BY_ID_SQL, (str(event_id),)).fetchone()
    if row is None:
        raise EventNotFound(event_id)
    if row["status"] != str(EventStatus.PROCESSING) or row["claim_token"] != str(claim_token):
        raise StaleEventClaim(event_id, claim_token)
    return _row_to_event(row)


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
            next_attempt_at=_optional_datetime(row["next_attempt_at"]),
            dead_lettered_at=_optional_datetime(row["dead_lettered_at"]),
        )
    except (ValueError, InvalidInboundEvent, InvalidEventTransition, StoreError) as exc:
        raise StoreError(f"stored inbound event is not readable: {exc}") from exc


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else from_utc_iso(str(value))


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["SqliteEventRepository"]
