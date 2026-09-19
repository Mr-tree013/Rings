"""Unit tests for the shared datetime serialisation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from assistant.store.errors import StoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso


def test_round_trip_preserves_the_instant() -> None:
    value = datetime(2026, 9, 19, 12, 34, 56, 789_012, tzinfo=UTC)

    restored = from_utc_iso(to_utc_iso(value))

    assert restored == value
    assert restored.tzinfo is UTC


def test_converts_an_offset_datetime_to_utc() -> None:
    beijing = timezone(timedelta(hours=8))

    stored = to_utc_iso(datetime(2026, 9, 19, 20, 0, tzinfo=beijing))

    assert stored == "2026-09-19T12:00:00.000000+00:00"


def test_iso_text_sorts_chronologically() -> None:
    earlier = to_utc_iso(datetime(2026, 9, 19, 12, 0, 0, 0, tzinfo=UTC))
    later = to_utc_iso(datetime(2026, 9, 19, 12, 0, 0, 1, tzinfo=UTC))

    assert earlier < later


def test_rejects_naive_datetime_when_writing() -> None:
    with pytest.raises(StoreError, match="timezone-aware"):
        to_utc_iso(datetime(2026, 9, 19, 12, 0))


def test_rejects_unparseable_text() -> None:
    with pytest.raises(StoreError, match="ISO 8601"):
        from_utc_iso("yesterday")


def test_rejects_naive_text_when_reading() -> None:
    with pytest.raises(StoreError, match="timezone-aware"):
        from_utc_iso("2026-09-19T12:00:00.000000")

