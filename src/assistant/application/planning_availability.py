"""Weekly availability expanded into instants, with a frozen DST policy (ADR-0015).

Local wall-clock windows come from `PlanningConfig`; converting them to instants is done by
`zoneinfo`, never by hand-computed offsets. Two DST cases need an explicit, deterministic
policy:

- **ambiguous** local times (fall-back): the start boundary uses `fold=0` (the earlier
  instant) and the end boundary uses `fold=1` (the later instant), so the repeated hour stays
  inside availability;
- **nonexistent** local times (spring-forward gap): the window is skipped rather than guessed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistant.domain.config import WEEKDAY_ORDER, PlanningConfig
from assistant.domain.planning import PlanningWindow
from assistant.domain.planning_intervals import Interval, clip_interval, merge_intervals


def resolve_local_wall_time(
    naive_local: datetime, timezone: ZoneInfo, *, fold: int
) -> datetime | None:
    """Return the aware instant for a local wall time, or `None` when it does not exist.

    Validation is a round trip (`local → UTC → local`) rather than trusting the fold flag.
    """
    if naive_local.tzinfo is not None:
        raise ValueError("resolve_local_wall_time expects a naive local datetime")
    candidate = naive_local.replace(tzinfo=timezone, fold=fold)
    round_trip = candidate.astimezone(UTC).astimezone(timezone)
    if round_trip.replace(tzinfo=None) != naive_local:
        # The wall time never happens (spring-forward gap), so nothing is guessed.
        return None
    return candidate


def generate_availability(
    window: PlanningWindow, config: PlanningConfig
) -> tuple[Interval, ...]:
    """Expand weekly rules into merged, window-clipped UTC instants.

    Every interval is normalised to UTC before it leaves this function. Two datetimes that
    share a `ZoneInfo` tzinfo object are compared and subtracted as *wall clock* by the
    stdlib, so keeping local tzinfo around would silently make a DST week look an hour longer
    (or shorter) than it really is.
    """
    timezone = ZoneInfo(config.timezone)
    first_day = window.starts_at.astimezone(timezone).date()
    last_day = window.ends_at.astimezone(timezone).date()
    bounds: Interval = (
        window.starts_at.astimezone(UTC),
        window.ends_at.astimezone(UTC),
    )
    intervals: list[Interval] = []
    day = first_day
    while day <= last_day:
        weekday = WEEKDAY_ORDER[day.weekday()]
        for rule in config.availability:
            if weekday not in rule.days:
                continue
            local_start = resolve_local_wall_time(
                _local_datetime(day, rule.start_minute), timezone, fold=0
            )
            local_end = resolve_local_wall_time(
                _local_datetime(day, rule.end_minute), timezone, fold=1
            )
            if local_start is None or local_end is None:
                # A wall time that never happens is skipped, never guessed.
                continue
            start = local_start.astimezone(UTC)
            end = local_end.astimezone(UTC)
            if end <= start:
                continue
            clipped = clip_interval((start, end), bounds)
            if clipped is not None:
                intervals.append(clipped)
        day += timedelta(days=1)
    return tuple(merge_intervals(intervals))


def _local_datetime(day: date, minute_of_day: int) -> datetime:
    return datetime.combine(day, time(0, 0)) + timedelta(minutes=minute_of_day)


__all__ = ["generate_availability", "resolve_local_wall_time"]
