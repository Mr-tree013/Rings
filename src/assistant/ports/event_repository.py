"""EventRepository port: durable storage for `InboundEvent` records (ADR-0002, ADR-0009).

The API is async because today's only implementation blocks: it talks to SQLite. That
async boundary is what keeps the daemon's event loop free (ADR-0009), and it is the
interface application code depends on.

The contract, so implementations and callers agree:

- `add` is the deduplication gate. It raises `DuplicateInboundEvent` when an event with
  the same `(source, external_id)` identity already exists; a duplicate is never reported
  as a successful insert. Uniqueness is enforced by the store (a partial unique index),
  not by a read-then-write check in Python.
- `get_by_external_identity` is the read side of that identity, used by idempotent
  ingestion. `external_id` must be a non-empty string: `None` is not an identity, and
  passing it is a programming error (`ValueError`).
- `transition` is a compare-and-set: the caller declares the status it believes the event
  has, and the move fails loudly if reality moved on (`UnexpectedEventStatus`) or the move
  itself is illegal (`InvalidEventTransition`).
- Only these operations exist. No claim, worker, delete, arbitrary update or raw SQL is
  exposed; those semantics have to be designed before they are written.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.inbound_event import EventId, EventStatus, InboundEvent


class EventRepository(Protocol):
    """Durable storage for inbound events."""

    async def add(self, event: InboundEvent) -> InboundEvent:
        """Persist a new event and return it.

        Raises:
            DuplicateInboundEvent: the `(source, external_id)` identity already exists.
        """
        ...

    async def get(self, event_id: EventId) -> InboundEvent | None:
        """Return the stored event, or `None` when it does not exist."""
        ...

    async def get_by_external_identity(
        self, source: str, external_id: str
    ) -> InboundEvent | None:
        """Return the event stored for `(source, external_id)`, or `None`.

        Raises:
            ValueError: `external_id` is empty or blank.
        """
        ...

    async def list_pending(self, *, limit: int) -> list[InboundEvent]:
        """Return events a worker still has to process (`RECEIVED`, `FAILED`).

        Ordered by `received_at`, then id, and capped by `limit`.
        """
        ...

    async def transition(
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

