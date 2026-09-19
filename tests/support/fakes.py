"""Test doubles and builders shared by unit and integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from assistant.domain.errors import (
    DuplicateInboundEvent,
    EventNotFound,
    PermanentEventError,
    StaleEventClaim,
    UnexpectedEventStatus,
)
from assistant.domain.event_claim import EventClaim
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
    next_attempt_at: datetime | None = None,
    dead_lettered_at: datetime | None = None,
    event_id: UUID | None = None,
) -> InboundEvent:
    """Build an InboundEvent with sensible defaults for tests."""
    if status in {EventStatus.FAILED, EventStatus.DEAD_LETTERED} and last_error is None:
        last_error = "previous attempt failed"
    if status is EventStatus.DEAD_LETTERED and dead_lettered_at is None:
        dead_lettered_at = DEFAULT_RECEIVED_AT
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
        next_attempt_at=next_attempt_at,
        dead_lettered_at=dead_lettered_at,
    )


class CountingEventIdFactory:
    """Deterministic EventId factory (1, 2, 3, ...) so tests never patch uuid4."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> EventId:
        self._next += 1
        return UUID(int=self._next)


class ScriptedHandler:
    """EventHandler test double that follows a script of outcomes.

    Outcomes: `"ok"`, `"fail"` (RuntimeError), `"permanent"` (PermanentEventError) and
    `"block"` (waits forever, for cancellation tests). The script is consumed one outcome
    per call and defaults to `"ok"` when exhausted.

    Handlers run on the asyncio event loop thread, so the recorded list needs no lock.
    """

    def __init__(self, outcomes: Sequence[str] = ("ok",)) -> None:
        self._outcomes = list(outcomes)
        self.handled: list[InboundEvent] = []
        self.started = asyncio.Event()

    async def handle(self, event: InboundEvent) -> None:
        self.handled.append(event)
        self.started.set()
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if outcome == "ok":
            return
        if outcome == "fail":
            raise RuntimeError("boom")
        if outcome == "permanent":
            raise PermanentEventError("cannot ever work")
        if outcome == "block":
            await asyncio.Event().wait()
        raise AssertionError(f"unknown scripted outcome: {outcome}")


class FakeEventRepository:
    """In-memory async EventRepository for application-level tests.

    It mimics the strict persistence semantics of the SQLite repository, so an
    `EventInbox` can be tested without a database. It is not a substitute for the
    integration tests: only real SQLite proves database-level uniqueness.
    """

    def __init__(self) -> None:
        self._events: dict[EventId, InboundEvent] = {}
        self._leases: dict[EventId, tuple[UUID, datetime]] = {}
        self.list_pending_calls = 0
        self.claim_calls = 0

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
        self.list_pending_calls += 1
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        pending = [event for event in self._events.values() if event.status in PENDING_STATUSES]
        pending.sort(key=lambda event: (event.received_at, str(event.id)))
        return pending[:limit]

    async def claim_next(
        self,
        *,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> EventClaim | None:
        self.claim_calls += 1
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if claim_token.int == 0:
            raise ValueError("claim_token must not be the nil UUID")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be after now")
        candidate = self._next_claimable(now)
        if candidate is None:
            return None
        claimed = candidate.claimed()
        self._events[claimed.id] = claimed
        self._leases[claimed.id] = (claim_token, lease_expires_at)
        return EventClaim(
            event=claimed,
            claim_token=claim_token,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=lease_expires_at,
        )

    async def complete_claim(
        self, event_id: EventId, *, claim_token: UUID, completed_at: datetime
    ) -> InboundEvent:
        stored = self._require_current_lease(event_id, claim_token)
        completed = stored.completed()
        self._events[event_id] = completed
        del self._leases[event_id]
        return completed

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
        stored = self._require_current_lease(event_id, claim_token)
        if dead_letter:
            updated = stored.dead_lettered(error=error, at=failed_at)
        else:
            if next_attempt_at is None:
                raise ValueError("a retryable failure requires next_attempt_at")
            updated = stored.failed(error=error, next_attempt_at=next_attempt_at)
        self._events[event_id] = updated
        del self._leases[event_id]
        return updated

    def _next_claimable(self, now: datetime) -> InboundEvent | None:
        candidates = [
            event
            for event in self._events.values()
            if self._is_claimable(event, now)
        ]
        candidates.sort(key=lambda event: (event.received_at, str(event.id)))
        return candidates[0] if candidates else None

    def _is_claimable(self, event: InboundEvent, now: datetime) -> bool:
        if event.status is EventStatus.RECEIVED:
            return True
        if event.status is EventStatus.FAILED:
            return event.next_attempt_at is not None and event.next_attempt_at <= now
        if event.status is EventStatus.PROCESSING:
            return self._lease_expired(event.id, now)
        return False

    def _lease_expired(self, event_id: EventId, now: datetime) -> bool:
        lease = self._leases.get(event_id)
        return lease is not None and lease[1] <= now

    def _require_current_lease(self, event_id: EventId, claim_token: UUID) -> InboundEvent:
        stored = self._events.get(event_id)
        if stored is None:
            raise EventNotFound(event_id)
        lease = self._leases.get(event_id)
        if (
            stored.status is not EventStatus.PROCESSING
            or lease is None
            or lease[0] != claim_token
        ):
            raise StaleEventClaim(event_id, claim_token)
        return stored

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
