"""ManualInputRepository port: pasted text and its event bridge (ADR-0029).

The input row is written before anything else happens to it, so `pw ingest text` either stores the
text or fails loudly — it never reports success for text that was not persisted. The bridge to
`EventInbox` is a separate, idempotent step, which is what makes the two crash windows (input
stored without an event, event stored without a link) repairable on the next run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from assistant.domain.inbound_event import EventId
from assistant.domain.manual_input import ManualInput, ManualInputId


class ManualInputRepository(Protocol):
    """Durable manual input and its links to the event inbox."""

    async def add_input(self, manual_input: ManualInput) -> ManualInput:
        """Store one manual input."""
        ...

    async def get_input(self, input_id: ManualInputId) -> ManualInput | None:
        """Return one manual input, or `None`."""
        ...

    async def list_inputs(self, *, limit: int | None = 20) -> list[ManualInput]:
        """List manual inputs, newest first."""
        ...

    async def resolve_input_id(self, reference: str) -> ManualInputId:
        """Resolve a full UUID or a unique prefix to a manual input id.

        Raises:
            ManualInputNotFound: nothing matches.
            AmbiguousId: several inputs match.
        """
        ...

    async def link_event(
        self,
        input_id: ManualInputId,
        inbound_event_id: EventId,
        *,
        linked_at: datetime,
    ) -> None:
        """Record the event a manual input was bridged as. Idempotent."""
        ...

    async def get_linked_event_id(self, input_id: ManualInputId) -> EventId | None:
        """Return the event a manual input is linked to, or `None`."""
        ...

    async def list_unlinked_inputs(self, *, limit: int) -> list[ManualInput]:
        """Bounded list of manual inputs with no bridge row, oldest first."""
        ...


__all__ = ["ManualInputRepository"]
