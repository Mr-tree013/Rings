"""Unit tests for the Clock port implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.adapters.system_clock import SystemClock
from assistant.ports.clock import Clock
from tests.support.fakes import FakeClock


def test_system_clock_returns_aware_utc_time() -> None:
    clock: Clock = SystemClock()

    now = clock.now()

    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_system_clock_tracks_real_time() -> None:
    clock: Clock = SystemClock()

    first = clock.now()
    second = clock.now()

    assert second >= first


def test_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock(start=datetime(2026, 9, 19, 12, 0, tzinfo=UTC))

    assert clock.now() == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    clock.advance(90)
    assert clock.now() == datetime(2026, 9, 19, 12, 1, 30, tzinfo=UTC)

