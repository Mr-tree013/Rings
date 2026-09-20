"""Text a person pasted in: stored first, bridged second (ADR-0029).

```text
pw ingest text TEXT --source qq-forward
        ▼
  ManualInput row                       (durable before anything else)
        ▼
  InboundEvent(manual.input.received)   (idempotent, repairable, names only the input)
        ▼
  EventWorker ──► bounded structured analysis
```

The order is the guarantee. If the process dies after the input row is written but before the event
is ingested, the repair round on the next command picks it up; if the event exists but its link
does not, the same idempotent ingest recognises it and links it. What the event carries is the
input's identity and its source name — never the text itself, because the text may be somebody
else's words forwarded through a chat client.

This service does not call a model, does not create a task and does not touch `EventWorker`: it
queues, and the daemon's worker does the rest when a provider is configured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from assistant.application.event_inbox import (
    EventInbox,
    IngestDisposition,
    IngestEvent,
    IngestResult,
)
from assistant.domain.errors import InvalidManualInput, ManualInputNotFound
from assistant.domain.inbound_event import EventId
from assistant.domain.manual_input import (
    ManualInput,
    ManualInputId,
    ManualInputSource,
)
from assistant.ports.clock import Clock
from assistant.ports.event_repository import EventRepository
from assistant.ports.manual_input_repository import ManualInputRepository

LOGGER = logging.getLogger("assistant.manual")

MANUAL_EVENT_TYPE = "manual.input.received"
"""The `InboundEvent` type a manual input is bridged as."""

BRIDGE_REPAIR_LIMIT = 50
"""How many unlinked inputs one round may repair."""


@dataclass(frozen=True, slots=True)
class ManualInputStored:
    """A stored input and the event that queues it for analysis."""

    manual_input: ManualInput
    event_id: EventId
    disposition: IngestDisposition


class ManualInputService:
    """Stores manual input and bridges it into the event inbox."""

    def __init__(
        self,
        repository: ManualInputRepository,
        events: EventRepository,
        clock: Clock,
        *,
        repair_limit: int = BRIDGE_REPAIR_LIMIT,
    ) -> None:
        self._repository = repository
        self._inbox = EventInbox(events, clock)
        self._clock = clock
        self._repair_limit = repair_limit

    async def create_input(
        self, text: str, *, source: ManualInputSource | str = ManualInputSource.MANUAL
    ) -> ManualInputStored:
        """Store one manual input and queue it for analysis.

        Raises:
            InvalidManualInput: the text is blank or longer than the limit.
            InvalidManualInput: the source is not one of the known names.
        """
        if isinstance(source, ManualInputSource):
            chosen = source
        else:
            try:
                chosen = ManualInputSource(source)
            except ValueError as exc:
                allowed = ", ".join(item.value for item in ManualInputSource)
                raise InvalidManualInput(
                    f"unknown manual input source {source!r}; expected one of: {allowed}"
                ) from exc
        manual_input = ManualInput(text=text, source=chosen, created_at=self._clock.now())
        stored = await self._repository.add_input(manual_input)
        result = await self._bridge(stored)
        LOGGER.info("manual input %s queued", stored.id)
        return ManualInputStored(
            manual_input=stored,
            event_id=result.event.id,
            disposition=result.disposition,
        )

    async def list_inputs(self, *, limit: int | None = 20) -> list[ManualInput]:
        """List manual inputs, newest first."""
        return await self._repository.list_inputs(limit=limit)

    async def get_input(self, reference: ManualInputId | str) -> ManualInput:
        """Return one manual input by id or unique prefix.

        Raises:
            ManualInputNotFound: nothing matches.
            AmbiguousId: a prefix matched several inputs.
        """
        input_id = (
            reference
            if not isinstance(reference, str)
            else await self._repository.resolve_input_id(reference)
        )
        manual_input = await self._repository.get_input(input_id)
        if manual_input is None:  # pragma: no cover - resolve already proved it exists
            raise ManualInputNotFound(input_id)
        return manual_input

    async def repair_bridge(self) -> tuple[int, int]:
        """Bridge inputs that have no `InboundEvent` link yet.

        Returns `(events_created, links_repaired)`. Both crash windows close here: an input stored
        without its event produces one, and an event written without its link is recognised as a
        duplicate and linked.
        """
        unlinked = await self._repository.list_unlinked_inputs(limit=self._repair_limit)
        created = repaired = 0
        for manual_input in unlinked:
            result = await self._bridge(manual_input)
            if result.disposition is IngestDisposition.CREATED:
                created += 1
            else:
                repaired += 1
        return created, repaired

    async def _bridge(self, manual_input: ManualInput) -> IngestResult:
        result = await self._inbox.ingest(
            IngestEvent(
                source=f"manual:{manual_input.source.value}",
                event_type=MANUAL_EVENT_TYPE,
                external_id=f"manual-input:{manual_input.id}",
                content=json.dumps(
                    {
                        "manual_input_id": str(manual_input.id),
                        "source": manual_input.source.value,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        await self._repository.link_event(
            manual_input.id, result.event.id, linked_at=self._clock.now()
        )
        return result


__all__ = [
    "BRIDGE_REPAIR_LIMIT",
    "MANUAL_EVENT_TYPE",
    "ManualInputService",
    "ManualInputStored",
]
