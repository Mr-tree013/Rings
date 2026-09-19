"""CalendarEvent: a fixed stretch of time that is already occupied (ADR-0014).

It answers exactly one question — "is this time already taken?" — so it has no recurrence, no
attendees, no provider and no reminder in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import CalendarEventNotActive, InvalidCalendarEvent

CalendarEventId = UUID
"""Stable identity of a calendar event."""

EVENT_TITLE_MAX_LENGTH = 500


class CalendarEventStatus(StrEnum):
    """Whether the event still occupies time."""

    ACTIVE = "active"
    CANCELLED = "cancelled"


def new_calendar_event_id() -> CalendarEventId:
    """Generate a fresh calendar event identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCalendarEvent(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Time that is already occupied."""

    title: str
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime
    id: CalendarEventId = field(default_factory=new_calendar_event_id)
    description: str | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise InvalidCalendarEvent("calendar event title must not be blank")
        if len(self.title.strip()) > EVENT_TITLE_MAX_LENGTH:
            raise InvalidCalendarEvent(
                f"calendar event title must be at most {EVENT_TITLE_MAX_LENGTH} characters"
            )
        _require_aware(self.starts_at, "starts_at")
        _require_aware(self.ends_at, "ends_at")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.ends_at <= self.starts_at:
            raise InvalidCalendarEvent("ends_at must be after starts_at")
        if self.updated_at < self.created_at:
            raise InvalidCalendarEvent("updated_at must not precede created_at")
        if self.cancelled_at is not None:
            _require_aware(self.cancelled_at, "cancelled_at")

    @property
    def status(self) -> CalendarEventStatus:
        """Derived from `cancelled_at`, which is the single source of truth."""
        return (
            CalendarEventStatus.CANCELLED
            if self.cancelled_at is not None
            else CalendarEventStatus.ACTIVE
        )

    @property
    def is_active(self) -> bool:
        """Whether this event still occupies time."""
        return self.cancelled_at is None

    def cancel(self, at: datetime) -> CalendarEvent:
        """Cancel the event; cancelling twice is an error, not a silent no-op."""
        _require_aware(at, "at")
        if not self.is_active:
            raise CalendarEventNotActive(f"calendar event {self.id} is already cancelled")
        return replace(self, cancelled_at=at, updated_at=at)


__all__ = [
    "EVENT_TITLE_MAX_LENGTH",
    "CalendarEvent",
    "CalendarEventId",
    "CalendarEventStatus",
    "new_calendar_event_id",
]

