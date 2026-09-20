"""WebWatchRepository port: target state, versioned observations and the event bridge (ADR-0029).

One operation is atomic and it is the one that matters: **recording a change** inserts the
observation and moves the target state in the same transaction. If it committed one without the
other, a crash could leave a baseline that never advances (so the same change is reported forever)
or an observation the state has never heard of (so the next poll reports it again).

The bridge is deliberately separate and idempotent, mirroring the mail pipeline: an observation is
committed first, the `InboundEvent` is ingested second, and the link is written third. Every crash
window between those steps is repairable by re-running the ingest, which is what
`list_unlinked_observations` exists for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from assistant.domain.inbound_event import EventId
from assistant.domain.web_watch import (
    WebObservation,
    WebObservationId,
    WebTargetId,
    WebWatchState,
)


class WebWatchRepository(Protocol):
    """Durable watcher state, observations and their links to the event inbox."""

    async def get_state(self, target_id: WebTargetId) -> WebWatchState | None:
        """Return the stored state of one target, or `None`."""
        ...

    async def record_observation(
        self, observation: WebObservation, state: WebWatchState
    ) -> WebObservation:
        """Insert an observation and move the target state, atomically."""
        ...

    async def save_state(self, state: WebWatchState) -> WebWatchState:
        """Store state for a fetch that produced no new observation."""
        ...

    async def get_observation(
        self, observation_id: WebObservationId
    ) -> WebObservation | None:
        """Return one observation, or `None`."""
        ...

    async def list_observations(
        self, *, target_id: WebTargetId | None = None, limit: int | None = 20
    ) -> list[WebObservation]:
        """List observations, newest first, optionally for one target."""
        ...

    async def resolve_observation_id(self, reference: str) -> WebObservationId:
        """Resolve a full UUID or a unique prefix to an observation id.

        Raises:
            WebObservationNotFound: nothing matches.
            AmbiguousId: several observations match.
        """
        ...

    async def link_event(
        self,
        observation_id: WebObservationId,
        inbound_event_id: EventId,
        *,
        linked_at: datetime,
    ) -> None:
        """Record the event an observation was bridged as. Idempotent."""
        ...

    async def get_linked_event_id(
        self, observation_id: WebObservationId
    ) -> EventId | None:
        """Return the event an observation is linked to, or `None`."""
        ...

    async def list_unlinked_observations(self, *, limit: int) -> list[WebObservation]:
        """Bounded list of observations with no bridge row, oldest first."""
        ...


__all__ = ["WebWatchRepository"]
