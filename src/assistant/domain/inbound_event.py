"""The `InboundEvent` model and its state machine (ADR-0002, ADR-0010).

Every external input enters the system as one of these records; the record is what makes
processing replayable, auditable and deduplicable. This module is pure: no I/O, no
database, no framework — enforced by tests/unit/test_architecture.py.

Full lifecycle:

```text
RECEIVED ──► PROCESSING ──► PROCESSED
FAILED   ──► PROCESSING ──► FAILED          (retry, scheduled by next_attempt_at)
                        └─► DEAD_LETTERED   (attempts exhausted or permanent failure)
```

`DEAD_LETTERED` is terminal: there is deliberately no domain path back out of it, and the
store never claims a dead-lettered event. Manual replay, if it is ever wanted, is its own
design.

`last_error` semantics: required on `FAILED` and `DEAD_LETTERED`, preserved while a retry
is `PROCESSING` (so a crash mid-retry still shows why it previously failed), cleared on
`PROCESSED`.

Time is always timezone-aware. Reading the clock is the caller's job (see
`assistant.ports.clock.Clock`), so this model stays deterministic.
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
    DEAD_LETTERED = "DEAD_LETTERED"


ALLOWED_TRANSITIONS: Final[dict[EventStatus, frozenset[EventStatus]]] = {
    EventStatus.RECEIVED: frozenset({EventStatus.PROCESSING}),
    EventStatus.PROCESSING: frozenset(
        {EventStatus.PROCESSED, EventStatus.FAILED, EventStatus.DEAD_LETTERED}
    ),
    EventStatus.FAILED: frozenset({EventStatus.PROCESSING}),
    EventStatus.PROCESSED: frozenset(),
    EventStatus.DEAD_LETTERED: frozenset(),
}

PENDING_STATUSES: Final[frozenset[EventStatus]] = frozenset(
    {EventStatus.RECEIVED, EventStatus.FAILED}
)
"""Statuses `list_pending` reports. Not a work-claim API — see ADR-0010."""

CLAIMABLE_STATUSES: Final[frozenset[EventStatus]] = frozenset(
    {EventStatus.RECEIVED, EventStatus.FAILED, EventStatus.PROCESSING}
)
"""Statuses a claim may start from.

`PROCESSING` is included only for lease recovery: the store reclaims such an event when,
and only when, its lease has expired. Retry timing (`next_attempt_at`) and lease expiry
are decided by the store's atomic claim query, not here.
"""


def is_allowed_transition(current: EventStatus, target: EventStatus) -> bool:
    """Return whether `current -> target` is a legal transition."""
    return target in ALLOWED_TRANSITIONS[current]


def new_event_id() -> EventId:
    """Generate a fresh event identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInboundEvent(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


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
    next_attempt_at: datetime | None = None
    dead_lettered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise InvalidInboundEvent("source must not be empty")
        if not self.event_type.strip():
            raise InvalidInboundEvent("event_type must not be empty")
        _require_aware(self.received_at, "received_at")
        _require_optional_aware(self.next_attempt_at, "next_attempt_at")
        _require_optional_aware(self.dead_lettered_at, "dead_lettered_at")
        if self.attempts < 0:
            raise InvalidInboundEvent("attempts must not be negative")
        if self.external_id is not None and not self.external_id.strip():
            raise InvalidInboundEvent("external_id must be None or a non-empty string")
        if self.status in {EventStatus.FAILED, EventStatus.DEAD_LETTERED}:
            if self.last_error is None or not self.last_error.strip():
                raise InvalidInboundEvent(
                    f"a {self.status} event must carry a non-empty last_error"
                )
        elif self.status is not EventStatus.PROCESSING and self.last_error is not None:
            raise InvalidInboundEvent(
                f"last_error is only meaningful for FAILED, DEAD_LETTERED or PROCESSING, "
                f"not {self.status}"
            )
        if self.next_attempt_at is not None and self.status is not EventStatus.FAILED:
            raise InvalidInboundEvent(
                f"next_attempt_at is only meaningful for FAILED, not {self.status}"
            )
        if self.status is EventStatus.DEAD_LETTERED:
            if self.dead_lettered_at is None:
                raise InvalidInboundEvent("a DEAD_LETTERED event must record dead_lettered_at")
        elif self.dead_lettered_at is not None:
            raise InvalidInboundEvent(
                f"dead_lettered_at is only meaningful for DEAD_LETTERED, not {self.status}"
            )

    def transition_to(
        self,
        target: EventStatus,
        *,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        dead_lettered_at: datetime | None = None,
    ) -> InboundEvent:
        """Return a copy of this event moved to `target`.

        Raises:
            InvalidEventTransition: the move is not in `ALLOWED_TRANSITIONS`.
            InvalidInboundEvent: the move lacks something it requires (an error message
                for `FAILED`/`DEAD_LETTERED`, a timestamp for dead lettering) or carries
                something it must not.
        """
        if not is_allowed_transition(self.status, target):
            raise InvalidEventTransition(self.status, target)
        _require_optional_aware(next_attempt_at, "next_attempt_at")
        _require_optional_aware(dead_lettered_at, "dead_lettered_at")

        detail: str | None
        terminal_at: datetime | None
        if target is EventStatus.FAILED:
            detail = _required_error(error, target)
            if dead_lettered_at is not None:
                raise InvalidInboundEvent("dead_lettered_at is not valid when moving to FAILED")
            deadline = next_attempt_at
            terminal_at = None
        elif target is EventStatus.DEAD_LETTERED:
            detail = _required_error(error, target)
            if dead_lettered_at is None:
                raise InvalidInboundEvent(
                    "moving an event to DEAD_LETTERED requires dead_lettered_at"
                )
            if next_attempt_at is not None:
                raise InvalidInboundEvent(
                    "next_attempt_at is not valid when moving to DEAD_LETTERED"
                )
            deadline = None
            terminal_at = dead_lettered_at
        else:
            if error is not None:
                raise InvalidInboundEvent(f"an error message is not valid when moving to {target}")
            if next_attempt_at is not None:
                raise InvalidInboundEvent(f"next_attempt_at is not valid when moving to {target}")
            if dead_lettered_at is not None:
                raise InvalidInboundEvent(f"dead_lettered_at is not valid when moving to {target}")
            detail = self.last_error if target is EventStatus.PROCESSING else None
            deadline = None
            terminal_at = None

        attempts = self.attempts + 1 if target is EventStatus.PROCESSING else self.attempts
        return replace(
            self,
            status=target,
            attempts=attempts,
            last_error=detail,
            next_attempt_at=deadline,
            dead_lettered_at=terminal_at,
        )

    def claimed(self) -> InboundEvent:
        """Start (or restart) processing this event.

        Allowed from `RECEIVED`, `FAILED` and `PROCESSING`. The store calls it on a
        `PROCESSING` event only when that event's lease has expired — the crash recovery
        path — so the attempt counter grows and the previous error stays visible for
        diagnosis.

        The lease itself (token, owner, expiry) is deliberately not part of the event: it
        belongs to `EventClaim`.

        Raises:
            InvalidEventTransition: the event is `PROCESSED` or `DEAD_LETTERED`.
        """
        if self.status not in CLAIMABLE_STATUSES:
            raise InvalidEventTransition(self.status, EventStatus.PROCESSING)
        return replace(
            self,
            status=EventStatus.PROCESSING,
            attempts=self.attempts + 1,
            next_attempt_at=None,
        )

    def completed(self) -> InboundEvent:
        """Move a `PROCESSING` event to `PROCESSED`, clearing the failure history."""
        return self.transition_to(EventStatus.PROCESSED)

    def failed(self, *, error: str, next_attempt_at: datetime) -> InboundEvent:
        """Move a `PROCESSING` event to `FAILED` and schedule its retry."""
        return self.transition_to(
            EventStatus.FAILED, error=error, next_attempt_at=next_attempt_at
        )

    def dead_lettered(self, *, error: str, at: datetime) -> InboundEvent:
        """Move a `PROCESSING` event to the terminal `DEAD_LETTERED` state."""
        return self.transition_to(EventStatus.DEAD_LETTERED, error=error, dead_lettered_at=at)


def _required_error(error: str | None, target: EventStatus) -> str:
    if error is None or not error.strip():
        raise InvalidInboundEvent(f"moving an event to {target} requires a non-empty error")
    return error


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLAIMABLE_STATUSES",
    "PENDING_STATUSES",
    "EventId",
    "EventStatus",
    "InboundEvent",
    "is_allowed_transition",
    "new_event_id",
]
