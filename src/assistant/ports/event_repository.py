"""EventRepository port: durable storage for `InboundEvent` records (ADR-0002).

The contract, so that implementations and callers agree:

- `add` is the deduplication gate. It raises `DuplicateInboundEvent` when an event with
  the same `(source, external_id)` identity already exists; a duplicate is never
  reported as a successful insert. Uniqueness is enforced by the store (a partial unique
  index), not by a read-then-write check in Python.
- `transition` is a compare-and-set: the caller declares the status it believes the event
  has, and the move fails loudly if reality moved on (`UnexpectedEventStatus`) or the
  move itself is illegal (`InvalidEventTransition`).
- Only these operations exist. No search, delete, arbitrary update or raw SQL is exposed.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.inbound_event import EventId, EventStatus, InboundEvent


class EventRepository(Protocol):
    """Durable storage for inbound events."""

    def add(self, event: InboundEvent) -> InboundEvent:
        """Persist a new event and return it.

        Raises:
            DuplicateInboundEvent: the `(source, external_id)` identity already exists.
        """
        ...

    def get(self, event_id: EventId) -> InboundEvent | None:
        """Return the stored event, or `None` when it does not exist."""
        ...

    def list_pending(self, *, limit: int) -> list[InboundEvent]:
        """Return events a worker still has to process (`RECEIVED`, `FAILED`).

        Ordered by `received_at`, then id, and capped by `limit`.
        """
        ...

    def transition(
        self,
        event_id: EventId,
        *,
        expected: EventStatus,
        target: EventStatus,
        error: str | None = None,
    ) -> InboundEvent:
        """Atomically move an event from `expected` to `target` and return the result.

        Raises:
            EventNotFound: no event has that identity.
            UnexpectedEventStatus: the stored status is not `expected`.
            InvalidEventTransition: the move is not allowed by the state machine.
        """
        ...


__all__ = ["EventRepository"]

