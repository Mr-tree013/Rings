"""Test doubles and builders shared by unit and integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from assistant.domain.errors import (
    DuplicateInboundEvent,
    EventNotFound,
    UnexpectedEventStatus,
)
from assistant.domain.inbound_event import (
    PENDING_STATUSES,
    EventId,
    EventStatus,
    InboundEvent,
)
from assistant.store.errors import StoreError

DEFAULT_RECEIVED_AT = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """A Clock whose time only moves when a test moves it."""

    def __init__(self, start: datetime = DEFAULT_RECEIVED_AT) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def make_event(
    *,
    source: str = "cli",
    external_id: str | None = None,
    event_type: str = "local.note",
    content: str | None = "note body",
    received_at: datetime = DEFAULT_RECEIVED_AT,
    status: EventStatus = EventStatus.RECEIVED,
    attempts: int = 0,
    last_error: str | None = None,
    event_id: UUID | None = None,
) -> InboundEvent:
    """Build an InboundEvent with sensible defaults for tests."""
    if status is EventStatus.FAILED and last_error is None:
        last_error = "previous attempt failed"
    return InboundEvent(
        id=uuid4() if event_id is None else event_id,
        source=source,
        external_id=external_id,
        event_type=event_type,
        content=content,
        received_at=received_at,
        status=status,
        attempts=attempts,
        last_error=last_error,
    )


class CountingEventIdFactory:
    """Deterministic EventId factory (1, 2, 3, ...) so tests never patch uuid4."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> EventId:
        self._next += 1
        return UUID(int=self._next)


class FakeEventRepository:
    """In-memory async EventRepository for application-level tests.

    It mimics the strict persistence semantics of the SQLite repository, so an
    `EventInbox` can be tested without a database. It is not a substitute for the
    integration tests: only real SQLite proves database-level uniqueness.
    """

    def __init__(self) -> None:
        self._events: dict[EventId, InboundEvent] = {}

    @property
    def events(self) -> tuple[InboundEvent, ...]:
        """Everything stored, in insertion order."""
        return tuple(self._events.values())

    async def add(self, event: InboundEvent) -> InboundEvent:
        if event.external_id is not None:
            for stored in self._events.values():
                if stored.source == event.source and stored.external_id == event.external_id:
                    raise DuplicateInboundEvent(event.source, event.external_id)
        if event.id in self._events:
            raise StoreError(f"could not persist inbound event {event.id}")
        self._events[event.id] = event
        return event

    async def get(self, event_id: EventId) -> InboundEvent | None:
        return self._events.get(event_id)

    async def get_by_external_identity(
        self, source: str, external_id: str
    ) -> InboundEvent | None:
        if not external_id.strip():
            raise ValueError("external_id must be a non-empty string")
        for stored in self._events.values():
            if stored.source == source and stored.external_id == external_id:
                return stored
        return None

    async def list_pending(self, *, limit: int) -> list[InboundEvent]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        pending = [event for event in self._events.values() if event.status in PENDING_STATUSES]
        pending.sort(key=lambda event: (event.received_at, str(event.id)))
        return pending[:limit]

    async def transition(
        self,
        event_id: EventId,
        *,
        expected: EventStatus,
        target: EventStatus,
        error: str | None = None,
    ) -> InboundEvent:
        stored = self._events.get(event_id)
        if stored is None:
            raise EventNotFound(event_id)
        if stored.status is not expected:
            raise UnexpectedEventStatus(event_id, expected, stored.status)
        updated = stored.transition_to(target, error=error)
        self._events[event_id] = updated
        return updated
