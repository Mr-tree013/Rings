"""Unit tests for the EventClaim lease value object."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from assistant.domain.errors import InvalidEventClaim
from assistant.domain.event_claim import EventClaim
from assistant.domain.inbound_event import EventStatus, InboundEvent
from tests.support.fakes import make_event

CLAIMED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
EXPIRES_AT = CLAIMED_AT + timedelta(minutes=5)


def _claim(
    *,
    event: InboundEvent | None = None,
    claim_token: UUID | None = None,
    claimed_by: str = "worker-a",
    claimed_at: datetime = CLAIMED_AT,
    lease_expires_at: datetime = EXPIRES_AT,
) -> EventClaim:
    return EventClaim(
        event=event
        if event is not None
        else make_event(status=EventStatus.PROCESSING, attempts=1),
        claim_token=claim_token if claim_token is not None else uuid4(),
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )


def test_accepts_a_well_formed_claim() -> None:
    claim = _claim()

    assert claim.claimed_by == "worker-a"
    assert claim.event.status is EventStatus.PROCESSING


def test_rejects_the_nil_token() -> None:
    with pytest.raises(InvalidEventClaim, match="nil UUID"):
        _claim(claim_token=UUID(int=0))


def test_rejects_a_blank_owner() -> None:
    with pytest.raises(InvalidEventClaim, match="claimed_by"):
        _claim(claimed_by="   ")


@pytest.mark.parametrize("field", ["claimed_at", "lease_expires_at"])
def test_rejects_naive_timestamps(field: str) -> None:
    with pytest.raises(InvalidEventClaim, match="timezone-aware"):
        _claim(**{field: datetime(2026, 9, 19, 12, 0)})


def test_rejects_a_lease_that_does_not_extend_past_the_claim() -> None:
    with pytest.raises(InvalidEventClaim, match="after claimed_at"):
        _claim(lease_expires_at=CLAIMED_AT)


def test_rejects_a_non_processing_event() -> None:
    with pytest.raises(InvalidEventClaim, match="PROCESSING"):
        _claim(event=make_event(status=EventStatus.RECEIVED))


def test_lease_expiry_is_inclusive_of_the_expiry_instant() -> None:
    claim = _claim()

    assert not claim.is_lease_expired(EXPIRES_AT - timedelta(seconds=1))
    assert claim.is_lease_expired(EXPIRES_AT)
    assert claim.is_lease_expired(EXPIRES_AT + timedelta(seconds=1))
