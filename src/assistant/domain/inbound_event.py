"""The `InboundEvent` model and its state machine (ADR-0002).

Every external input enters the system as one of these records; the record is what makes
processing replayable, auditable and deduplicable. This module is pure: no I/O, no
database, no framework — enforced by tests/unit/test_architecture.py.

Time is always timezone-aware. Reading the clock is the caller's job (see
`assistant.ports.clock.Clock`), so this model stays deterministic.

`last_error` semantics: required on `FAILED`, preserved while a retry is `PROCESSING`
(so a crash mid-retry still shows why it previously failed), and cleared on `PROCESSED`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidEventTransition, InvalidInboundEvent

EventId = UUID
"""Stable identity of an inbound event."""


class EventStatus(StrEnum):
    """Lifecycle of an inbound event.

    The legal moves are listed in `ALLOWED_TRANSITIONS`; nothing else is permitted, and
    the store refuses to write any other combination.
    """

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: Final[dict[EventStatus, frozenset[EventStatus]]] = {
    EventStatus.RECEIVED: frozenset({EventStatus.PROCESSING}),
    EventStatus.PROCESSING: frozenset({EventStatus.PROCESSED, EventStatus.FAILED}),
    EventStatus.FAILED: frozenset({EventStatus.PROCESSING}),
    EventStatus.PROCESSED: frozenset(),
}

PENDING_STATUSES: Final[frozenset[EventStatus]] = frozenset(
    {EventStatus.RECEIVED, EventStatus.FAILED}
)
"""Statuses a future worker still has to process."""


def is_allowed_transition(current: EventStatus, target: EventStatus) -> bool:
    """Return whether `current -> target` is a legal transition."""
    return target in ALLOWED_TRANSITIONS[current]


def new_event_id() -> EventId:
    """Generate a fresh event identity."""
    return uuid4()


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """A single external input, persisted before it is processed."""

    source: str
    event_type: str
    received_at: datetime
    id: EventId = field(default_factory=new_event_id)
    external_id: str | None = None
    content: str | None = None
    status: EventStatus = EventStatus.RECEIVED
    attempts: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise InvalidInboundEvent("source must not be empty")
        if not self.event_type.strip():
            raise InvalidInboundEvent("event_type must not be empty")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise InvalidInboundEvent("received_at must be timezone-aware")
        if self.attempts < 0:
            raise InvalidInboundEvent("attempts must not be negative")
        if self.external_id is not None and not self.external_id.strip():
            raise InvalidInboundEvent("external_id must be None or a non-empty string")
        if self.status is EventStatus.FAILED:
            if self.last_error is None or not self.last_error.strip():
                raise InvalidInboundEvent("a FAILED event must carry a non-empty last_error")
        elif self.status is not EventStatus.PROCESSING and self.last_error is not None:
            raise InvalidInboundEvent(
                f"last_error is only meaningful for FAILED or PROCESSING, not {self.status}"
            )

    def transition_to(self, target: EventStatus, *, error: str | None = None) -> InboundEvent:
        """Return a copy of this event moved to `target`.

        Raises:
            InvalidEventTransition: the move is not in `ALLOWED_TRANSITIONS`.
            InvalidInboundEvent: the move needs an error message it did not get, or was
                given one it must not carry.
        """
        if not is_allowed_transition(self.status, target):
            raise InvalidEventTransition(self.status, target)
        detail: str | None
        if target is EventStatus.FAILED:
            if error is None or not error.strip():
                raise InvalidInboundEvent("moving an event to FAILED requires a non-empty error")
            detail = error
        elif error is not None:
            raise InvalidInboundEvent(f"an error message is not valid when moving to {target}")
        elif target is EventStatus.PROCESSING:
            detail = self.last_error
        else:
            detail = None
        attempts = self.attempts + 1 if target is EventStatus.PROCESSING else self.attempts
        return replace(self, status=target, attempts=attempts, last_error=detail)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "PENDING_STATUSES",
    "EventId",
    "EventStatus",
    "InboundEvent",
    "is_allowed_transition",
    "new_event_id",
]
