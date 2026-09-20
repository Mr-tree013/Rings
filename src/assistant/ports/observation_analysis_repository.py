"""ObservationAnalysisRepository port: one durable analysis per inbound event (ADR-0029).

`inbound_event_id` is unique, and that is the whole idempotency story: the EventWorker may retry an
event after a crash, but the analysis it already paid for is still there, keyed by the event. The
handler compares the stored fingerprint before it decides to call a provider again.

Analyses are append-only in practice: a row is written once and replaced only when the same event
is analyzed under a new fingerprint.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.inbound_event import EventId
from assistant.domain.observation_analysis import ObservationAnalysis


class ObservationAnalysisRepository(Protocol):
    """Durable analyses of web changes and manual input."""

    async def get_analysis(self, event_id: EventId) -> ObservationAnalysis | None:
        """Return the analysis recorded for one inbound event, or `None`."""
        ...

    async def persist_analysis(
        self, analysis: ObservationAnalysis
    ) -> ObservationAnalysis:
        """Store one analysis, replacing any earlier row for the same event."""
        ...


__all__ = ["ObservationAnalysisRepository"]
