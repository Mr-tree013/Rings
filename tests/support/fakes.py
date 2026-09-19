"""Test doubles and builders shared by unit and integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from assistant.domain.inbound_event import EventStatus, InboundEvent

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

