"""A worker's lease on one inbound event (ADR-0010).

Processing is at-least-once: a claim grants the holder the *current* right to work on an
event, and that right is fenced by a random `claim_token`. When the lease expires another
worker may claim the same event with a new token; after that, the old holder's attempts to
complete or fail the event are rejected (`StaleEventClaim`) instead of overwriting the
newer attempt's state.

The lease is deliberately a separate type. `InboundEvent` describes what arrived and where
it is in its lifecycle; `EventClaim` describes who is working on it right now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import InvalidEventClaim
from assistant.domain.inbound_event import EventStatus, InboundEvent


@dataclass(frozen=True, slots=True)
class EventClaim:
    """The right to process one event until `lease_expires_at`."""

    event: InboundEvent
    claim_token: UUID
    claimed_by: str
    claimed_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if self.claim_token.int == 0:
            raise InvalidEventClaim("claim_token must not be the nil UUID")
        if not self.claimed_by.strip():
            raise InvalidEventClaim("claimed_by must not be empty")
        _require_aware(self.claimed_at, "claimed_at")
        _require_aware(self.lease_expires_at, "lease_expires_at")
        if self.lease_expires_at <= self.claimed_at:
            raise InvalidEventClaim("lease_expires_at must be after claimed_at")
        if self.event.status is not EventStatus.PROCESSING:
            raise InvalidEventClaim(
                f"a claim must reference a PROCESSING event, not {self.event.status}"
            )

    def is_lease_expired(self, now: datetime) -> bool:
        """Return whether `now` is at or past the lease expiry."""
        _require_aware(now, "now")
        return now >= self.lease_expires_at


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEventClaim(f"{field_name} must be timezone-aware")


__all__ = ["EventClaim"]

