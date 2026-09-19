"""Pure half-open interval arithmetic used by the planner (ADR-0015).

All functions work on `(start, end)` aware instants and follow the frozen rule: intervals are
half-open `[start, end)`. Adjacent busy intervals are merged, because "10:00-11:00" and
"11:00-12:00" are one continuous block of unavailability.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from assistant.domain.errors import InvalidTimeInterval

Interval = tuple[datetime, datetime]


def _validate(interval: Interval) -> None:
    start, end = interval
    for value, field_name in ((start, "start"), (end, "end")):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTimeInterval(f"interval {field_name} must be timezone-aware")
    if end <= start:
        raise InvalidTimeInterval("interval end must be after start")


def clip_interval(interval: Interval, bounds: Interval) -> Interval | None:
    """Return the part of `interval` inside `bounds`, or `None` when they do not overlap."""
    _validate(interval)
    _validate(bounds)
    start = max(interval[0], bounds[0])
    end = min(interval[1], bounds[1])
    return None if end <= start else (start, end)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping *and* adjacent intervals, sorted by start."""
    ordered = sorted(intervals)
    merged: list[Interval] = []
    for start, end in ordered:
        _validate((start, end))
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(free: Sequence[Interval], busy: Sequence[Interval]) -> list[Interval]:
    """Return the parts of `free` not covered by `busy`, merging adjacent busy intervals."""
    merged_busy = merge_intervals(busy)
    remaining: list[Interval] = []
    for start, end in merge_intervals(free):
        cursor = start
        for busy_start, busy_end in merged_busy:
            if busy_end <= cursor:
                continue
            if busy_start >= end:
                break
            if busy_start > cursor:
                remaining.append((cursor, min(busy_start, end)))
            cursor = max(cursor, busy_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
    return [interval for interval in remaining if interval[1] > interval[0]]


__all__ = [
    "Interval",
    "clip_interval",
    "merge_intervals",
    "subtract_intervals",
]

