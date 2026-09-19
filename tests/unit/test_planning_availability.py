"""Weekly availability expansion, including the frozen DST policy (ADR-0015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.application.planning_availability import (
    generate_availability,
    resolve_local_wall_time,
)
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.planning import PlanningWindow


def _config(
    timezone: str,
    rules: tuple[WeeklyAvailabilityRule, ...],
) -> PlanningConfig:
    return PlanningConfig(timezone=timezone, availability=rules)


def _rule(days: tuple[Weekday, ...], start: str, end: str) -> WeeklyAvailabilityRule:
    start_hour, start_minute = (int(part) for part in start.split(":"))
    end_hour, end_minute = (int(part) for part in end.split(":"))
    return WeeklyAvailabilityRule(
        days=days,
        start_minute=start_hour * 60 + start_minute,
        end_minute=end_hour * 60 + end_minute,
    )


def _window(start: str, end: str, timezone: str = "Asia/Shanghai") -> PlanningWindow:
    return PlanningWindow(
        starts_at=datetime.fromisoformat(start),
        ends_at=datetime.fromisoformat(end),
        timezone=timezone,
    )


WEEKDAYS = (Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI)


def test_weekday_rules_expand_to_each_matching_local_day() -> None:
    config = _config("Asia/Shanghai", (_rule(WEEKDAYS, "09:00", "12:00"),))
    # 2026-09-21 is a Monday; the window spans Monday 00:00 to Thursday 00:00 local.
    window = _window("2026-09-21T00:00:00+08:00", "2026-09-24T00:00:00+08:00")

    availability = generate_availability(window, config)

    assert availability == (
        (
            datetime(2026, 9, 21, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 22, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 23, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 23, 4, 0, tzinfo=UTC),
        ),
    )


def test_multiple_windows_on_one_day_stay_separate() -> None:
    config = _config(
        "Asia/Shanghai",
        (
            _rule((Weekday.MON,), "09:00", "12:00"),
            _rule((Weekday.MON,), "14:00", "18:00"),
        ),
    )
    window = _window("2026-09-21T00:00:00+08:00", "2026-09-22T00:00:00+08:00")

    availability = generate_availability(window, config)

    assert availability == (
        (
            datetime(2026, 9, 21, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 21, 6, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
        ),
    )


def test_weekend_rules_apply_only_to_the_weekend() -> None:
    config = _config("Asia/Shanghai", (_rule((Weekday.SAT, Weekday.SUN), "10:00", "18:00"),))
    window = _window("2026-09-25T00:00:00+08:00", "2026-09-28T00:00:00+08:00")

    availability = generate_availability(window, config)

    local_days = [
        start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        for start, _ in availability
    ]
    assert local_days == ["2026-09-26", "2026-09-27"]


def test_availability_is_clipped_to_the_planning_window() -> None:
    config = _config("Asia/Shanghai", (_rule(WEEKDAYS, "09:00", "22:00"),))
    window = _window("2026-09-21T12:00:00+08:00", "2026-09-21T20:00:00+08:00")

    availability = generate_availability(window, config)

    assert availability == (
        (
            datetime(2026, 9, 21, 4, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        ),
    )


def test_no_availability_rules_means_no_availability() -> None:
    window = _window("2026-09-21T00:00:00+08:00", "2026-09-22T00:00:00+08:00")

    assert generate_availability(window, _config("Asia/Shanghai", ())) == ()


def test_naive_wall_times_never_leak_out() -> None:
    config = _config("America/New_York", (_rule(WEEKDAYS, "09:00", "17:00"),))
    window = _window(
        "2026-03-02T00:00:00-05:00", "2026-03-09T00:00:00-04:00", timezone="America/New_York"
    )

    availability = generate_availability(window, config)

    assert availability
    for start, end in availability:
        assert start.tzinfo is not None and start.utcoffset() is not None
        assert end.tzinfo is not None and end.utcoffset() is not None
        assert end > start


def test_spring_forward_local_window_skips_the_nonexistent_hour() -> None:
    # 2026-03-08 02:00 America/New_York does not exist: local time jumps 02:00 -> 03:00.
    config = _config("America/New_York", (_rule((Weekday.SUN,), "02:00", "04:00"),))
    window = _window(
        "2026-03-08T00:00:00-05:00",
        "2026-03-09T00:00:00-04:00",
        timezone="America/New_York",
    )

    assert generate_availability(window, config) == ()


def test_spring_forward_elapsed_duration_is_real() -> None:
    # 2026-03-08 01:00-04:00 local is only two real hours, not three.
    config = _config("America/New_York", (_rule((Weekday.SUN,), "01:00", "04:00"),))
    window = _window(
        "2026-03-08T00:00:00-05:00",
        "2026-03-09T00:00:00-04:00",
        timezone="America/New_York",
    )

    availability = generate_availability(window, config)

    assert len(availability) == 1
    start, end = availability[0]
    assert (end - start).total_seconds() == 2 * 3600
    assert start == datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


def test_fall_back_ambiguous_boundaries_use_the_fold_policy() -> None:
    # 2026-11-01 01:00-02:00 America/New_York happens twice; start takes fold=0, end fold=1.
    config = _config("America/New_York", (_rule((Weekday.SUN,), "01:00", "02:00"),))
    window = _window(
        "2026-11-01T00:00:00-04:00",
        "2026-11-02T00:00:00-05:00",
        timezone="America/New_York",
    )

    availability = generate_availability(window, config)

    assert availability == (
        (
            datetime(2026, 11, 1, 5, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        ),
    )
    start, end = availability[0]
    assert (end - start).total_seconds() == 2 * 3600


def test_resolve_local_wall_time_reports_nonexistent_and_ambiguous_times() -> None:
    new_york = ZoneInfo("America/New_York")

    assert resolve_local_wall_time(datetime(2026, 3, 8, 2, 30), new_york, fold=0) is None
    early = resolve_local_wall_time(datetime(2026, 11, 1, 1, 30), new_york, fold=0)
    late = resolve_local_wall_time(datetime(2026, 11, 1, 1, 30), new_york, fold=1)
    assert early is not None and late is not None
    # Same tzinfo means the stdlib subtracts wall clock, so compare instants in UTC.
    assert (late.astimezone(UTC) - early.astimezone(UTC)) == timedelta(hours=1)


def test_resolve_local_wall_time_rejects_aware_input() -> None:
    with pytest.raises(ValueError):
        resolve_local_wall_time(
            datetime(2026, 3, 8, 2, 30, tzinfo=UTC), ZoneInfo("America/New_York"), fold=0
        )
