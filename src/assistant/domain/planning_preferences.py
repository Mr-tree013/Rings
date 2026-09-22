"""Durable capacity preferences for the deterministic planner (ADR-0044).

The planner already respected the weekly availability rules. What it could not express was the
shape of the user's own working day: "never after ten", "at most six hours", "don't put five hours
on one task in one sitting". Those are *intention*, they belong to the user, and they are durable —
so they live in the runtime database rather than in a sentence that is re-interpreted every time.

```text
day_start_local / day_end_local   the civil hours the planner may use at all
max_daily_minutes                 how much may be planned inside one local day
preferred_block_minutes           the normal length of one sitting
max_block_minutes                 the ceiling a single sitting may never exceed
```

Two deliberate absences. The **timezone** is not here: it has exactly one authority in
`[planning].timezone`, because two copies of "which day is today" is how a machine starts
disagreeing with itself. And there is no **score** or weight: the planner places time under hard
constraints, and a preference that only nudged a ranking would be a preference nobody could test.

The defaults describe the whole civil day and the planner's existing ceilings, which is why a
runtime with no stored row plans exactly as v1.2 did.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from assistant.domain.errors import InvalidPlanningPreferences

CURRENT_PREFERENCES_ID = "current"
"""The singleton row's id. One runtime has one set of preferences."""

MINUTES_PER_DAY = 24 * 60
DEFAULT_DAY_START_MINUTE = 0
DEFAULT_DAY_END_MINUTE = MINUTES_PER_DAY
DEFAULT_MAX_DAILY_MINUTES = MINUTES_PER_DAY
DEFAULT_PREFERRED_BLOCK_MINUTES = 60
DEFAULT_MAX_BLOCK_MINUTES = 120
"""The ceilings the planner already used, so "no preferences" is not a behaviour change."""

MIN_BLOCK_MINUTES = 15
"""The shortest sitting worth creating. Below this a block is noise on a calendar."""


class ProposalMode(StrEnum):
    """What a proposal intends to do with the plan that already exists."""

    NORMAL = "normal"
    """A fresh weekly plan: it replaces the planner blocks it overlaps, and nothing else."""

    REPLACE_FUTURE = "replace_future"
    """A replan of what is left: it intends to replace the remaining automatic plan, and says so."""


def _require_minutes(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPlanningPreferences(f"{field_name} must be a whole number of minutes")
    return value


@dataclass(frozen=True, slots=True)
class DailyWindow:
    """One local civil day the planner may use, with its own capacity."""

    starts_at: datetime
    ends_at: datetime
    capacity_minutes: int

    def __post_init__(self) -> None:
        for value, name in ((self.starts_at, "starts_at"), (self.ends_at, "ends_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidPlanningPreferences(f"a daily window's {name} must be aware")
        if self.ends_at <= self.starts_at:
            raise InvalidPlanningPreferences("a daily window must end after it starts")
        if _require_minutes(self.capacity_minutes, "capacity_minutes") < 1:
            raise InvalidPlanningPreferences("a daily window needs a positive capacity")

    @property
    def length_minutes(self) -> int:
        """How long the day is, in whole minutes."""
        return int((self.ends_at - self.starts_at).total_seconds() // 60)

    def to_payload(self) -> dict[str, object]:
        """The bounded representation a browser may show."""
        return {
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "capacity_minutes": self.capacity_minutes,
        }


@dataclass(frozen=True, slots=True)
class PlanningPreferences:
    """The durable capacity rules one runtime plans under."""

    day_start_local: int = DEFAULT_DAY_START_MINUTE
    day_end_local: int = DEFAULT_DAY_END_MINUTE
    max_daily_minutes: int = DEFAULT_MAX_DAILY_MINUTES
    preferred_block_minutes: int = DEFAULT_PREFERRED_BLOCK_MINUTES
    max_block_minutes: int = DEFAULT_MAX_BLOCK_MINUTES
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: str = CURRENT_PREFERENCES_ID

    def __post_init__(self) -> None:
        if self.id != CURRENT_PREFERENCES_ID:
            raise InvalidPlanningPreferences(
                "there is exactly one set of planning preferences per runtime"
            )
        start = _require_minutes(self.day_start_local, "day_start_local")
        end = _require_minutes(self.day_end_local, "day_end_local")
        if not 0 <= start < end <= MINUTES_PER_DAY:
            raise InvalidPlanningPreferences(
                "the planning day must satisfy 0 <= start < end <= 24:00; overnight windows are "
                "not supported"
            )
        daily = _require_minutes(self.max_daily_minutes, "max_daily_minutes")
        if not 1 <= daily <= MINUTES_PER_DAY:
            raise InvalidPlanningPreferences("max_daily_minutes must be between 1 and 1440")
        preferred = _require_minutes(self.preferred_block_minutes, "preferred_block_minutes")
        maximum = _require_minutes(self.max_block_minutes, "max_block_minutes")
        if preferred < MIN_BLOCK_MINUTES:
            raise InvalidPlanningPreferences(
                f"a preferred block must be at least {MIN_BLOCK_MINUTES} minutes"
            )
        if maximum < preferred:
            raise InvalidPlanningPreferences(
                "max_block_minutes must not be smaller than preferred_block_minutes"
            )
        if maximum > MINUTES_PER_DAY:
            raise InvalidPlanningPreferences("max_block_minutes must be at most 1440")
        for value, name in ((self.created_at, "created_at"), (self.updated_at, "updated_at")):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise InvalidPlanningPreferences(f"{name} must be timezone-aware")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise InvalidPlanningPreferences("updated_at must not precede created_at")

    @property
    def planned_hours_per_day(self) -> float:
        """The daily capacity in hours, for a settings field that reads better in hours."""
        return self.max_daily_minutes / 60

    @property
    def day_length_minutes(self) -> int:
        """How long the planning day is."""
        return self.day_end_local - self.day_start_local

    def day_bounds(self, day_start: datetime) -> tuple[datetime, datetime]:
        """The civil-day window as instants, anchored on a local midnight."""
        return (
            day_start + timedelta(minutes=self.day_start_local),
            day_start + timedelta(minutes=self.day_end_local),
        )

    def with_changes(self, **changes: object) -> PlanningPreferences:
        """Return the same preferences with fields replaced, revalidated.

        Raises:
            InvalidPlanningPreferences: the result would not be usable.
        """
        return replace(self, **changes)  # type: ignore[arg-type]

    def to_payload(self) -> dict[str, object]:
        """The bounded representation a browser or a conversation may show."""
        return {
            "day_start_local": _clock_text(self.day_start_local),
            "day_end_local": _clock_text(self.day_end_local),
            "max_daily_minutes": self.max_daily_minutes,
            "max_daily_hours": self.planned_hours_per_day,
            "preferred_block_minutes": self.preferred_block_minutes,
            "max_block_minutes": self.max_block_minutes,
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
        }


def _clock_text(minute_of_day: int) -> str:
    """`09:30`, or `24:00` for the end of the day."""
    hours, minutes = divmod(minute_of_day, 60)
    return f"{hours:02d}:{minutes:02d}"


def parse_clock(value: str, field_name: str) -> int:
    """Parse `HH:MM` into minutes from local midnight.

    Raises:
        InvalidPlanningPreferences: the text is not a clock time this project accepts.
    """
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2 or any(
        len(part) != 2 or not part.isdigit() for part in parts
    ):
        raise InvalidPlanningPreferences(f"{field_name} must be HH:MM, not {text!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 24 or minutes > 59 or (hours == 24 and minutes != 0):
        raise InvalidPlanningPreferences(f"{field_name} is not a valid time: {text!r}")
    return hours * 60 + minutes


__all__ = [
    "CURRENT_PREFERENCES_ID",
    "DEFAULT_DAY_END_MINUTE",
    "DEFAULT_DAY_START_MINUTE",
    "DEFAULT_MAX_BLOCK_MINUTES",
    "DEFAULT_MAX_DAILY_MINUTES",
    "DEFAULT_PREFERRED_BLOCK_MINUTES",
    "MINUTES_PER_DAY",
    "MIN_BLOCK_MINUTES",
    "DailyWindow",
    "PlanningPreferences",
    "ProposalMode",
    "parse_clock",
]
