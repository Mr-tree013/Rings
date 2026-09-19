"""Half-open time intervals and busy-time read models (ADR-0014).

Interval semantics are frozen here: `[start, end)`. Two intervals that merely touch
(`10:00-11:00` and `11:00-12:00`) do **not** overlap, which is what a planner needs.

`BusyInterval` keeps its source kind and identity, so a plan block is never disguised as a
calendar event: a planner can tell "this time is taken by a lecture" apart from "this time is
planned for this task".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from assistant.domain.errors import InvalidTimeInterval


def overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Return whether two half-open intervals `[start, end)` intersect."""
    for value, field_name in (
        (a_start, "a_start"),
        (a_end, "a_end"),
        (b_start, "b_start"),
        (b_end, "b_end"),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidTimeInterval(f"{field_name} must be timezone-aware")
    if a_end <= a_start or b_end <= b_start:
        raise InvalidTimeInterval("interval ends must be after their starts")
    return a_start < b_end and b_start < a_end


class BusyIntervalKind(StrEnum):
    """Where an occupied interval comes from."""

    CALENDAR_EVENT = "calendar_event"
    PLAN_BLOCK = "plan_block"


@dataclass(frozen=True, slots=True)
class BusyInterval:
    """One stretch of time that is occupied, and by what."""

    source_kind: BusyIntervalKind
    source_id: UUID
    starts_at: datetime
    ends_at: datetime
    title: str | None = None
    origin: str | None = None
    """For plan blocks: `manual` or `planner`; never set for calendar events."""

    proposal_id: UUID | None = None
    """For planner-generated plan blocks: the proposal that created them."""

    def __post_init__(self) -> None:
        for value, field_name in ((self.starts_at, "starts_at"), (self.ends_at, "ends_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidTimeInterval(f"{field_name} must be timezone-aware")
        if self.ends_at <= self.starts_at:
            raise InvalidTimeInterval("ends_at must be after starts_at")


__all__ = ["BusyInterval", "BusyIntervalKind", "overlaps"]
