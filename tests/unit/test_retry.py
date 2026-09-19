"""Unit tests for the deterministic retry policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.application.retry import (
    DEFAULT_BASE_DELAY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY,
    RetryPolicy,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_documented_defaults() -> None:
    policy = RetryPolicy()

    assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS == 5
    assert policy.base_delay == DEFAULT_BASE_DELAY == timedelta(seconds=30)
    assert policy.max_delay == DEFAULT_MAX_DELAY == timedelta(minutes=30)


def test_rejects_a_non_positive_attempt_budget() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


def test_rejects_a_non_positive_base_delay() -> None:
    with pytest.raises(ValueError, match="base_delay"):
        RetryPolicy(base_delay=timedelta(0))


def test_rejects_a_max_delay_shorter_than_the_base_delay() -> None:
    with pytest.raises(ValueError, match="max_delay"):
        RetryPolicy(base_delay=timedelta(minutes=1), max_delay=timedelta(seconds=30))


def test_delay_grows_exponentially_then_caps() -> None:
    policy = RetryPolicy(
        max_attempts=10,
        base_delay=timedelta(seconds=30),
        max_delay=timedelta(minutes=5),
    )

    delays = [policy.delay_for(attempts) for attempts in range(1, 7)]

    assert delays == [
        timedelta(seconds=30),
        timedelta(seconds=60),
        timedelta(seconds=120),
        timedelta(seconds=240),
        timedelta(minutes=5),
        timedelta(minutes=5),
    ]


def test_delay_is_deterministic_and_jitter_free() -> None:
    policy = RetryPolicy()

    assert policy.delay_for(3) == policy.delay_for(3)
    assert policy.next_attempt_at(attempts=3, now=NOW) == policy.next_attempt_at(
        attempts=3, now=NOW
    )


def test_a_huge_attempt_count_stays_capped() -> None:
    policy = RetryPolicy()

    assert policy.delay_for(10**6) == policy.max_delay


def test_delay_requires_a_started_attempt() -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy().delay_for(0)


def test_next_attempt_at_adds_the_delay_to_now() -> None:
    policy = RetryPolicy(base_delay=timedelta(seconds=30))

    assert policy.next_attempt_at(attempts=1, now=NOW) == NOW + timedelta(seconds=30)
    assert policy.next_attempt_at(attempts=2, now=NOW) == NOW + timedelta(seconds=60)


def test_next_attempt_at_rejects_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RetryPolicy().next_attempt_at(attempts=1, now=datetime(2026, 9, 19, 12, 0))

