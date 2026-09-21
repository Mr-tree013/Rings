"""Weekly recurring commitments and their effect on planning (ADR-0036).

The rule is durable state; occurrences are derived; the deterministic planner treats them as busy
time. Nothing here materializes calendar events, and nothing here reads the host timezone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.planner_service import PlannerService, busy_intervals
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.errors import InvalidTimeInterval, RecurringRuleNotFound
from assistant.domain.planning import PlanningWindow
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.recurring_calendar import SqliteRecurringCalendarRepository
from tests.support.fakes import FakeClock

MONDAY = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=MONDAY)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    return database


def _service(database: Database, clock: FakeClock, timezone: str | None = "Asia/Shanghai"):
    return RecurringCalendarService(
        SqliteRecurringCalendarRepository(database), clock, default_timezone=timezone
    )


async def test_a_weekly_rule_is_created_once_and_is_idempotent(
    database: Database, clock: FakeClock
) -> None:
    service = _service(database, clock)

    first = await service.create_weekly(
        title="计算机系统基础课", weekday=1, start="10:00", end="12:00"
    )
    again = await service.create_weekly(
        title="计算机系统基础课", weekday=1, start="10:00", end="12:00"
    )

    assert first.id == again.id
    assert len(await service.list_active()) == 1


async def test_two_weekdays_are_two_rules(database: Database, clock: FakeClock) -> None:
    service = _service(database, clock)

    await service.create_weekly(title="软件工程课", weekday=1, start="14:00", end="16:00")
    await service.create_weekly(title="软件工程课", weekday=3, start="14:00", end="16:00")

    assert [rule.weekday for rule in await service.list_active()] == [1, 3]


async def test_expansion_is_deterministic_and_timezone_fixed(
    database: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule means the same instants whatever the host timezone claims to be."""
    service = _service(database, clock)
    rule = await service.create_weekly(
        title="课", weekday=1, start="10:00", end="12:00"
    )
    observed = []
    for host_zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        monkeypatch.setenv("TZ", host_zone)
        occurrences = rule.expand(
            window_start=MONDAY, window_end=MONDAY + timedelta(days=7)
        )
        observed.append([occurrence.starts_at.astimezone(UTC) for occurrence in occurrences])

    assert observed[0] == observed[1] == observed[2]
    # Monday 10:00 Asia/Shanghai is 02:00 UTC.
    assert observed[0] == [datetime(2026, 9, 21, 2, 0, tzinfo=UTC)]


async def test_expansion_respects_starts_on_and_ends_on(
    database: Database, clock: FakeClock
) -> None:
    service = _service(database, clock)
    rule = await service.create_weekly(
        title="课",
        weekday=1,
        start="10:00",
        end="12:00",
        starts_on=date(2026, 10, 5),
        ends_on=date(2026, 10, 12),
    )

    occurrences = rule.expand(
        window_start=MONDAY, window_end=MONDAY + timedelta(days=40)
    )

    assert [occurrence.starts_at.date() for occurrence in occurrences] == [
        date(2026, 10, 5),
        date(2026, 10, 12),
    ]


async def test_edit_keeps_identity_and_retire_stops_occurrences(
    database: Database, clock: FakeClock
) -> None:
    service = _service(database, clock)
    rule = await service.create_weekly(
        title="课", weekday=1, start="10:00", end="12:00"
    )

    edited = await service.edit(str(rule.id), start="09:00", end="11:00")
    retired = await service.retire(str(rule.id))

    assert edited.id == rule.id
    assert edited.fingerprint != rule.fingerprint  # a different meaning is a different rule
    assert retired.status.value == "retired"
    assert await service.list_active() == []
    assert (
        await service.expand_range(
            window_start=MONDAY, window_end=MONDAY + timedelta(days=7)
        )
        == ()
    )


async def test_invalid_rules_are_refused(database: Database, clock: FakeClock) -> None:
    service = _service(database, clock)

    with pytest.raises(InvalidTimeInterval):
        await service.create_weekly(title="课", weekday=1, start="12:00", end="10:00")
    with pytest.raises(InvalidTimeInterval):
        await service.create_weekly(title="课", weekday=9, start="10:00", end="12:00")
    with pytest.raises(RecurringRuleNotFound):
        await service.require_rule("does-not-exist")


async def test_missing_planning_timezone_asks_instead_of_guessing(
    database: Database, clock: FakeClock
) -> None:
    service = _service(database, clock, timezone=None)

    with pytest.raises(InvalidTimeInterval):
        await service.create_weekly(title="课", weekday=1, start="10:00", end="12:00")


async def test_the_planner_treats_a_class_as_busy_time(
    database: Database, clock: FakeClock
) -> None:
    """The headline behaviour: no plan block is ever placed on top of a recurring class."""
    recurring = _service(database, clock)
    rule = await recurring.create_weekly(
        title="计算机系统基础课", weekday=1, start="10:00", end="12:00"
    )
    planning = SqlitePlanningRepository(database)
    planner = PlannerService(
        planning,
        GreedyPlanner(),
        PlanningConfig(
            timezone="Asia/Shanghai",
            availability=(
                WeeklyAvailabilityRule(
                    days=(Weekday.MON,),
                    start_minute=9 * 60,
                    end_minute=22 * 60,
                ),
            ),
        ),
        clock,
        recurring=recurring,
    )
    occurrences = rule.expand(window_start=MONDAY, window_end=MONDAY + timedelta(days=7))
    assert occurrences, "the rule must produce an occurrence in this window"

    proposal = await planner.create_proposal(planner.week_window())
    class_window = (
        occurrences[0].starts_at.astimezone(UTC),
        occurrences[0].ends_at.astimezone(UTC),
    )

    for block in proposal.blocks:
        assert not (
            block.starts_at < class_window[1] and block.ends_at > class_window[0]
        ), f"a plan block overlaps the class: {block.starts_at}-{block.ends_at}"


async def test_recurring_time_is_merged_with_overlapping_events(
    database: Database, clock: FakeClock
) -> None:
    """Busy time is a set, not a list: an event over a class is one interval (ADR-0036 §13)."""
    recurring = _service(database, clock)
    await recurring.create_weekly(
        title="计算机系统基础课", weekday=1, start="10:00", end="12:00"
    )
    commitments = SqliteCommitmentRepository(database)
    await commitments.add_calendar_event(
        CalendarEvent(
            title="临时的课",
            starts_at=datetime(2026, 9, 21, 3, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 21, 6, 0, tzinfo=UTC),
            created_at=MONDAY,
            updated_at=MONDAY,
        )
    )
    window = PlanningWindow(
        starts_at=datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
        ends_at=datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )
    snapshot = await SqlitePlanningRepository(database).load_snapshot(window)
    occurrences = await recurring.expand_range(
        window_start=window.starts_at, window_end=window.ends_at
    )

    merged = busy_intervals(
        snapshot,
        tuple((item.starts_at, item.ends_at) for item in occurrences),
    )

    assert len(merged) == 1
    assert merged[0][0] == datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
    assert merged[0][1] == datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
