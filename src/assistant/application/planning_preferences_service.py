"""Reading, changing and reporting the planner's capacity preferences (ADR-0044 §43).

```text
show()      stored row, or the defaults the configuration already implied
update()    validated change, written with this moment's Clock
effective() what the planner will actually use, given the host's `[planning]` section
```

Three rules are worth naming.

**No stored row is not a zero.** A runtime that never opened the settings page has no preferences
row, and `effective()` then returns exactly what the configuration says — including a preferred
block length equal to the configured maximum, so upgrading does not silently halve every block.

**The timezone is not here.** `[planning].timezone` remains the only authority, and this service
refuses to accept a second one.

**A change is reported exactly.** Every write returns the resulting preferences and the sentence a
user would read, so "以后每天晚上十点后不要安排任务" produces the exact resulting day rather than a
claim that something changed.
"""

from __future__ import annotations

from assistant.domain.config import PlanningConfig
from assistant.domain.planning_preferences import (
    DEFAULT_DAY_END_MINUTE,
    DEFAULT_DAY_START_MINUTE,
    DEFAULT_MAX_DAILY_MINUTES,
    PlanningPreferences,
    parse_clock,
)
from assistant.ports.clock import Clock
from assistant.ports.planning_preferences_repository import PlanningPreferencesRepository


class PlanningPreferencesService:
    """Durable capacity preferences, with the configuration as the fallback."""

    def __init__(
        self,
        preferences: PlanningPreferencesRepository,
        clock: Clock,
        *,
        config: PlanningConfig | None = None,
    ) -> None:
        self._preferences = preferences
        self._clock = clock
        self._config = config

    async def show(self) -> PlanningPreferences:
        """What the user has set, or the defaults."""
        return await self._preferences.get_preferences() or PlanningPreferences()

    async def effective(self) -> PlanningPreferences:
        """What the planner will actually use on this host.

        With no stored row the answer comes from `[planning]`: the day is unfiltered, the daily
        capacity is unlimited, and one sitting is as long as the configuration already allowed.
        """
        stored = await self._preferences.get_preferences()
        if stored is not None or self._config is None:
            return stored or PlanningPreferences()
        ceiling = self._config.max_block_minutes
        return PlanningPreferences(
            day_start_local=DEFAULT_DAY_START_MINUTE,
            day_end_local=DEFAULT_DAY_END_MINUTE,
            max_daily_minutes=DEFAULT_MAX_DAILY_MINUTES,
            preferred_block_minutes=ceiling,
            max_block_minutes=ceiling,
        )

    async def update(self, **changes: object) -> PlanningPreferences:
        """Store a change, revalidated, stamped by the Clock.

        `day_start` and `day_end` may be given as `HH:MM` strings, which is what a person types;
        every other field is already minutes.

        Raises:
            InvalidPlanningPreferences: the result would not be a usable day.
        """
        current = await self.show()
        resolved: dict[str, object] = {}
        for key, value in changes.items():
            if value is None:
                continue
            if key in ("day_start", "day_end"):
                field_name = "day_start_local" if key == "day_start" else "day_end_local"
                resolved[field_name] = parse_clock(str(value), key)
                continue
            resolved[key] = value
        updated = current.with_changes(**resolved)
        return await self._preferences.save_preferences(updated)

    def describe(self, preferences: PlanningPreferences) -> str:
        """What the preferences mean, in one sentence a user can check."""
        hours = preferences.max_daily_minutes / 60
        return (
            f"计划时段 {_clock(preferences.day_start_local)}–{_clock(preferences.day_end_local)}，"
            f"每天最多安排 {_hours(hours)}，"
            f"单次通常 {preferences.preferred_block_minutes} 分钟"
            f"（最长 {preferences.max_block_minutes} 分钟）。"
        )


def _clock(minute_of_day: int) -> str:
    hours, minutes = divmod(minute_of_day, 60)
    return f"{hours:02d}:{minutes:02d}"


def _hours(value: float) -> str:
    """`6 小时` or `5.5 小时`, without a trailing `.0`."""
    return f"{int(value)} 小时" if value == int(value) else f"{value:g} 小时"


__all__ = ["PlanningPreferencesService"]
