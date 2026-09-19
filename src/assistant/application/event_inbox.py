"""Event Inbox: the single ingestion entry point for every external source (ADR-0002).

Source adapters (CLI, IMAP, QQ, website, ...) call `EventInbox.ingest` instead of writing
to the store directly. Two different jobs live in two different places on purpose:

```text
EventInbox      idempotent ingestion semantics   (a repeat is a success)
EventRepository strict persistence semantics     (a repeat is an error)
```

The inbox only *writes* durable `RECEIVED` events. It does not process them: claiming,
retry, backoff, poison handling and crash recovery are a separate design and deliberately
absent here (see the system design spec).

It performs no clever normalisation either: no lowercasing, no trimming semantics, no
content hashing, no identity guessing. Source adapters provide stable `source`,
`external_id` and `event_type`; the domain validates them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from assistant.domain.errors import DomainError, DuplicateInboundEvent
from assistant.domain.inbound_event import EventId, InboundEvent, new_event_id
from assistant.ports.clock import Clock
from assistant.ports.event_repository import EventRepository


class EventInboxConsistencyError(DomainError):
    """An internal invariant broke while ingesting an event.

    Raised when the store reports a duplicate that cannot then be read back, or when an
    event with no external identity collides. Neither may be papered over by creating a
    second event or by returning a fabricated result.
    """


@dataclass(frozen=True, slots=True)
class IngestEvent:
    """What a source adapter is allowed to say about an event.

    Identity and lifecycle fields (`id`, `received_at`, `status`, `attempts`,
    `last_error`) are system-controlled and deliberately not part of this command.
    """

    source: str
    event_type: str
    external_id: str | None = None
    content: str | None = None


class IngestDisposition(StrEnum):
    """Whether an ingestion created a new event or recognised an existing one."""

    CREATED = "created"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """The event that represents this input, plus how it got there."""

    event: InboundEvent
    disposition: IngestDisposition


class EventInbox:
    """Idempotent ingestion of external input into durable `InboundEvent` records."""

    def __init__(
        self,
        repository: EventRepository,
        clock: Clock,
        *,
        new_id: Callable[[], EventId] = new_event_id,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._new_id = new_id

    async def ingest(self, command: IngestEvent) -> IngestResult:
        """Record an external input, returning the durable event and its disposition.

        A repeated `(source, external_id)` is an idempotent success (`DUPLICATE`), not an
        error. Events without an external identity are never deduplicated.

        Raises:
            InvalidInboundEvent: the command breaks a domain invariant (empty source or
                event type, blank external id).
            EventInboxConsistencyError: the store contradicted itself.
        """
        event = InboundEvent(
            id=self._new_id(),
            source=command.source,
            external_id=command.external_id,
            event_type=command.event_type,
            content=command.content,
            received_at=self._clock.now(),
        )
        try:
            stored = await self._repository.add(event)
        except DuplicateInboundEvent:
            return await self._resolve_duplicate(command)
        return IngestResult(event=stored, disposition=IngestDisposition.CREATED)

    async def _resolve_duplicate(self, command: IngestEvent) -> IngestResult:
        if command.external_id is None:
            raise EventInboxConsistencyError(
                "the store rejected an event with no external identity, so the duplicate "
                "cannot correspond to an existing event"
            )
        existing = await self._repository.get_by_external_identity(
            command.source, command.external_id
        )
        if existing is None:
            raise EventInboxConsistencyError(
                f"the store reported {command.source!r}/{command.external_id!r} as a "
                "duplicate, but no event with that identity could be read back"
            )
        return IngestResult(event=existing, disposition=IngestDisposition.DUPLICATE)


__all__ = [
    "EventInbox",
    "EventInboxConsistencyError",
    "IngestDisposition",
    "IngestEvent",
    "IngestResult",
]

