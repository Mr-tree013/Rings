"""Unit tests for the InboundEvent model and its state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from assistant.domain.errors import InvalidEventTransition, InvalidInboundEvent
from assistant.domain.inbound_event import (
    ALLOWED_TRANSITIONS,
    CLAIMABLE_STATUSES,
    PENDING_STATUSES,
    EventStatus,
    InboundEvent,
    is_allowed_transition,
)
from tests.support.fakes import make_event

RECEIVED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
DEAD_LETTERED_AT = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)


def _event_in_status(status: EventStatus) -> InboundEvent:
    carries_error = status in {
        EventStatus.FAILED,
        EventStatus.PROCESSING,
        EventStatus.DEAD_LETTERED,
    }
    return make_event(
        status=status, attempts=1, last_error="boom" if carries_error else None
    )


def test_new_event_defaults_to_received_with_no_attempts() -> None:
    event = InboundEvent(source="cli", event_type="local.note", received_at=RECEIVED_AT)

    assert event.status is EventStatus.RECEIVED
    assert event.attempts == 0
    assert event.last_error is None
    assert event.external_id is None
    assert event.content is None


def test_every_event_gets_its_own_id() -> None:
    assert make_event().id != make_event().id


def test_accepts_timezone_aware_received_at() -> None:
    beijing = timezone(timedelta(hours=8))
    event = InboundEvent(
        source="smail",
        event_type="mail.received",
        received_at=datetime(2026, 9, 19, 20, 0, tzinfo=beijing),
    )

    assert event.received_at.utcoffset() == timedelta(hours=8)


def test_rejects_naive_received_at() -> None:
    with pytest.raises(InvalidInboundEvent, match="timezone-aware"):
        InboundEvent(
            source="cli",
            event_type="local.note",
            received_at=datetime(2026, 9, 19, 12, 0),
        )


@pytest.mark.parametrize("source", ["", "   "])
def test_rejects_blank_source(source: str) -> None:
    with pytest.raises(InvalidInboundEvent, match="source"):
        make_event(source=source)


@pytest.mark.parametrize("event_type", ["", " \t "])
def test_rejects_blank_event_type(event_type: str) -> None:
    with pytest.raises(InvalidInboundEvent, match="event_type"):
        make_event(event_type=event_type)


def test_rejects_negative_attempts() -> None:
    with pytest.raises(InvalidInboundEvent, match="attempts"):
        make_event(attempts=-1)


def test_rejects_blank_external_id() -> None:
    with pytest.raises(InvalidInboundEvent, match="external_id"):
        make_event(external_id=" ")


def test_failed_event_requires_last_error() -> None:
    with pytest.raises(InvalidInboundEvent, match="FAILED"):
        InboundEvent(
            source="smail",
            event_type="mail.received",
            received_at=RECEIVED_AT,
            status=EventStatus.FAILED,
        )


@pytest.mark.parametrize("status", [EventStatus.RECEIVED, EventStatus.PROCESSED])
def test_last_error_is_only_meaningful_for_failed_or_processing(status: EventStatus) -> None:
    with pytest.raises(InvalidInboundEvent, match="last_error"):
        make_event(status=status, attempts=1, last_error="stale error")


def test_processing_event_may_carry_the_error_of_the_failed_attempt() -> None:
    event = make_event(status=EventStatus.PROCESSING, attempts=1, last_error="boom")

    assert event.last_error == "boom"


def test_pending_statuses_are_received_and_failed() -> None:
    assert set(PENDING_STATUSES) == {EventStatus.RECEIVED, EventStatus.FAILED}


def test_claimable_statuses_cover_recovery_but_not_terminal_states() -> None:
    assert set(CLAIMABLE_STATUSES) == {
        EventStatus.RECEIVED,
        EventStatus.FAILED,
        EventStatus.PROCESSING,
    }
    assert EventStatus.DEAD_LETTERED not in CLAIMABLE_STATUSES
    assert EventStatus.PROCESSED not in CLAIMABLE_STATUSES


def test_dead_lettered_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[EventStatus.DEAD_LETTERED] == frozenset()
    assert not is_allowed_transition(EventStatus.PROCESSED, EventStatus.DEAD_LETTERED)
    assert is_allowed_transition(EventStatus.PROCESSING, EventStatus.DEAD_LETTERED)


def test_every_allowed_transition_succeeds() -> None:
    for current, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            moved = _event_in_status(current).transition_to(
                target,
                error="boom"
                if target in {EventStatus.FAILED, EventStatus.DEAD_LETTERED}
                else None,
                dead_lettered_at=DEAD_LETTERED_AT
                if target is EventStatus.DEAD_LETTERED
                else None,
            )
            assert moved.status is target


def test_every_forbidden_transition_is_rejected() -> None:
    for current in EventStatus:
        for target in EventStatus:
            if is_allowed_transition(current, target):
                continue
            with pytest.raises(InvalidEventTransition):
                _event_in_status(current).transition_to(
                    target,
                    error="boom"
                    if target in {EventStatus.FAILED, EventStatus.DEAD_LETTERED}
                    else None,
                    dead_lettered_at=DEAD_LETTERED_AT
                    if target is EventStatus.DEAD_LETTERED
                    else None,
                )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (EventStatus.PROCESSED, EventStatus.PROCESSING),
        (EventStatus.RECEIVED, EventStatus.PROCESSED),
        (EventStatus.FAILED, EventStatus.PROCESSED),
        (EventStatus.RECEIVED, EventStatus.FAILED),
        (EventStatus.DEAD_LETTERED, EventStatus.PROCESSING),
        (EventStatus.DEAD_LETTERED, EventStatus.PROCESSED),
        (EventStatus.PROCESSED, EventStatus.DEAD_LETTERED),
        (EventStatus.RECEIVED, EventStatus.DEAD_LETTERED),
        (EventStatus.FAILED, EventStatus.DEAD_LETTERED),
    ],
)
def test_documented_forbidden_transitions(current: EventStatus, target: EventStatus) -> None:
    with pytest.raises(InvalidEventTransition) as excinfo:
        _event_in_status(current).transition_to(target)

    assert excinfo.value.current is current
    assert excinfo.value.target is target


def test_entering_processing_increments_attempts() -> None:
    event = make_event(attempts=0)

    moved = event.transition_to(EventStatus.PROCESSING)

    assert moved.attempts == 1
    assert moved.status is EventStatus.PROCESSING


def test_retry_keeps_last_error_until_the_event_succeeds() -> None:
    failed = _event_in_status(EventStatus.FAILED)

    retrying = failed.transition_to(EventStatus.PROCESSING)

    assert retrying.attempts == 2
    assert retrying.last_error == failed.last_error


def test_success_clears_last_error() -> None:
    retrying = make_event(status=EventStatus.PROCESSING, attempts=1, last_error="boom")

    processed = retrying.transition_to(EventStatus.PROCESSED)

    assert processed.status is EventStatus.PROCESSED
    assert processed.last_error is None
    assert processed.attempts == 1


def test_failure_requires_an_error_message() -> None:
    with pytest.raises(InvalidInboundEvent, match="requires a non-empty error"):
        make_event(status=EventStatus.PROCESSING, attempts=1).transition_to(EventStatus.FAILED)


def test_success_rejects_an_error_message() -> None:
    with pytest.raises(InvalidInboundEvent, match="not valid when moving to PROCESSED"):
        make_event(status=EventStatus.PROCESSING, attempts=1).transition_to(
            EventStatus.PROCESSED, error="boom"
        )


def test_transition_returns_a_new_object_and_leaves_the_original_untouched() -> None:
    event = make_event()

    moved = event.transition_to(EventStatus.PROCESSING)

    assert moved is not event
    assert event.status is EventStatus.RECEIVED
    assert event.attempts == 0


@pytest.mark.parametrize(
    "status",
    [EventStatus.RECEIVED, EventStatus.FAILED, EventStatus.PROCESSING],
)
def test_claimed_starts_or_restarts_processing(status: EventStatus) -> None:
    event = make_event(status=status, attempts=2, next_attempt_at=None)

    claimed = event.claimed()

    assert claimed.status is EventStatus.PROCESSING
    assert claimed.attempts == 3
    assert claimed.next_attempt_at is None


@pytest.mark.parametrize("status", [EventStatus.PROCESSED, EventStatus.DEAD_LETTERED])
def test_claimed_refuses_terminal_events(status: EventStatus) -> None:
    with pytest.raises(InvalidEventTransition):
        _event_in_status(status).claimed()


def test_claiming_clears_a_pending_retry_time() -> None:
    failed = make_event(
        status=EventStatus.FAILED,
        attempts=1,
        next_attempt_at=DEAD_LETTERED_AT,
    )

    claimed = failed.claimed()

    assert claimed.next_attempt_at is None
    assert claimed.last_error == failed.last_error


def test_completed_clears_every_failure_marker() -> None:
    retrying = make_event(status=EventStatus.PROCESSING, attempts=2, last_error="boom")

    processed = retrying.completed()

    assert processed.status is EventStatus.PROCESSED
    assert processed.last_error is None
    assert processed.next_attempt_at is None
    assert processed.dead_lettered_at is None
    assert processed.attempts == 2


def test_failed_records_the_retry_time() -> None:
    retrying = make_event(status=EventStatus.PROCESSING, attempts=1)

    failed = retrying.failed(error="timeout", next_attempt_at=DEAD_LETTERED_AT)

    assert failed.status is EventStatus.FAILED
    assert failed.last_error == "timeout"
    assert failed.next_attempt_at == DEAD_LETTERED_AT
    assert failed.dead_lettered_at is None


def test_dead_lettered_records_the_terminal_timestamp() -> None:
    retrying = make_event(status=EventStatus.PROCESSING, attempts=5)

    dead = retrying.dead_lettered(error="poison", at=DEAD_LETTERED_AT)

    assert dead.status is EventStatus.DEAD_LETTERED
    assert dead.last_error == "poison"
    assert dead.dead_lettered_at == DEAD_LETTERED_AT
    assert dead.next_attempt_at is None
    assert dead.attempts == 5


def test_dead_lettering_requires_a_timestamp() -> None:
    with pytest.raises(InvalidInboundEvent, match="requires dead_lettered_at"):
        make_event(status=EventStatus.PROCESSING, attempts=1).transition_to(
            EventStatus.DEAD_LETTERED, error="poison"
        )


def test_dead_lettered_event_must_record_when() -> None:
    with pytest.raises(InvalidInboundEvent, match="dead_lettered_at"):
        InboundEvent(
            source="smail",
            event_type="mail.received",
            received_at=RECEIVED_AT,
            status=EventStatus.DEAD_LETTERED,
            last_error="poison",
        )


def test_next_attempt_at_is_only_meaningful_for_failed_events() -> None:
    with pytest.raises(InvalidInboundEvent, match="next_attempt_at"):
        make_event(status=EventStatus.PROCESSING, attempts=1, next_attempt_at=DEAD_LETTERED_AT)


def test_dead_lettered_rejects_a_pending_retry_time() -> None:
    with pytest.raises(InvalidInboundEvent, match="next_attempt_at"):
        make_event(
            status=EventStatus.PROCESSING,
            attempts=1,
        ).transition_to(
            EventStatus.DEAD_LETTERED,
            error="poison",
            dead_lettered_at=DEAD_LETTERED_AT,
            next_attempt_at=DEAD_LETTERED_AT,
        )


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(InvalidInboundEvent, match="timezone-aware"):
        make_event(next_attempt_at=datetime(2026, 9, 19, 12, 0))
