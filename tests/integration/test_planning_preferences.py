"""Planning 2.0: capacity rules, splitting and replanning (ADR-0044).

The properties under test are the ones a user would notice: a daily limit that is actually a limit,
a five-hour task that arrives as several ordinary sittings, a replan that changes nothing until it
is applied, and a past or manual block that survives either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.application.greedy_planner import GreedyPlanner
from assistant.domain.errors import InvalidPlanningPreferences
from assistant.domain.planning import (
    PlanningRequest,
    PlanningTask,
    PlanningWindow,
    PlanProposalStatus,
)
from assistant.domain.planning_preferences import (
    DailyWindow,
    PlanningPreferences,
    ProposalMode,
)
from assistant.domain.task import TaskPriority
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning_preferences import SqlitePlanningPreferencesRepository
from tests.support.conversation import build_harness, operation, plan
from tests.support.fakes import FakeClock

MONDAY = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
"""Monday 12:00 in Asia/Shanghai."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=MONDAY)


def _preferences(harness):
    """The preferences service over one harness's runtime."""
    return bootstrap.planning_preferences_service(
        harness.database, harness.clock, harness.config
    )


def _request(
    *,
    start: datetime = MONDAY,
    minutes: int = 180,
    slots: tuple[tuple[datetime, datetime], ...] = (),
    max_block: int = 120,
    preferred: int = 0,
    min_block: int = 30,
    capacity: tuple[DailyWindow, ...] = (),
) -> PlanningRequest:
    return PlanningRequest(
        window=PlanningWindow(starts_at=start, ends_at=start + timedelta(days=7), timezone="UTC"),
        tasks=(
            PlanningTask(
                task_id=__import__("uuid").uuid4(),
                title="写报告",
                priority=TaskPriority.NORMAL,
                created_at=start,
                estimated_minutes=minutes,
                actual_seconds=0,
                remaining_minutes=minutes,
            ),
        ),
        availability=slots,
        busy_intervals=(),
        min_block_minutes=min_block,
        max_block_minutes=max_block,
        deadline_buffer_minutes=0,
        preferred_block_minutes=preferred,
        daily_capacity=capacity,
    )


# ------------------------------------------------------------------- preferences


def test_preferences_refuse_an_unusable_day() -> None:
    with pytest.raises(InvalidPlanningPreferences):
        PlanningPreferences(day_start_local=1320, day_end_local=480)
    with pytest.raises(InvalidPlanningPreferences):
        PlanningPreferences(max_daily_minutes=0)
    with pytest.raises(InvalidPlanningPreferences):
        PlanningPreferences(preferred_block_minutes=180, max_block_minutes=120)
    with pytest.raises(InvalidPlanningPreferences):
        PlanningPreferences(day_end_local=1500)


def test_preferences_default_to_the_whole_day() -> None:
    default = PlanningPreferences()

    assert default.day_start_local == 0
    assert default.day_end_local == 24 * 60
    assert default.to_payload()["day_start_local"] == "00:00"
    assert default.to_payload()["day_end_local"] == "24:00"


async def test_the_preferences_row_is_a_singleton(tmp_path: Path, clock: FakeClock) -> None:
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    repository = SqlitePlanningPreferencesRepository(database, clock)

    assert await repository.get_preferences() is None
    await repository.save_preferences(PlanningPreferences(max_daily_minutes=360))
    clock.advance(300)
    await repository.save_preferences(PlanningPreferences(max_daily_minutes=300))

    stored = await repository.get_preferences()
    assert stored is not None
    assert stored.max_daily_minutes == 300

    import sqlite3

    connection = sqlite3.connect(str(database.path))
    try:
        count = connection.execute("SELECT COUNT(*) FROM planning_preferences").fetchone()[0]
    finally:
        connection.close()
    assert count == 1
    assert stored.created_at is not None and stored.updated_at is not None
    assert stored.updated_at > stored.created_at


async def test_a_host_without_preferences_plans_as_the_configuration_says(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The backward-compatibility promise: no stored row is not a behaviour change."""
    harness = await build_harness(tmp_path)
    service = _preferences(harness)

    effective = await service.effective()

    assert effective.day_start_local == 0
    assert effective.day_end_local == 24 * 60
    assert effective.preferred_block_minutes == harness.config.planning.max_block_minutes
    assert effective.max_block_minutes == harness.config.planning.max_block_minutes


async def test_updating_preferences_reports_the_result(
    tmp_path: Path, clock: FakeClock
) -> None:
    harness = await build_harness(tmp_path)
    service = _preferences(harness)

    updated = await service.update(day_end="22:00", max_daily_minutes=360)

    assert updated.day_end_local == 22 * 60
    assert updated.max_daily_minutes == 360
    assert "每天最多安排 6 小时" in service.describe(updated)
    assert "22:00" in service.describe(updated)
    # The timezone is not one of the fields, by construction.
    assert "timezone" not in updated.to_payload()


# -------------------------------------------------------------------- capacity


async def test_the_daily_limit_is_a_hard_constraint() -> None:
    day = DailyWindow(starts_at=MONDAY, ends_at=MONDAY + timedelta(hours=12), capacity_minutes=60)
    request = _request(
        minutes=180,
        slots=((MONDAY, MONDAY + timedelta(hours=6)),),
        capacity=(day,),
    )

    result = GreedyPlanner().plan(request)

    scheduled = sum(
        int((block.ends_at - block.starts_at).total_seconds() // 60) for block in result.blocks
    )
    assert scheduled == 60
    assert result.issues[-1].code.value == "DAILY_CAPACITY_REACHED"
    assert result.issues[-1].required_minutes == 180


def test_a_large_task_is_split_into_preferred_sittings() -> None:
    request = _request(
        minutes=300,
        preferred=90,
        max_block=120,
        slots=((MONDAY, MONDAY + timedelta(hours=12)),),
    )

    result = GreedyPlanner().plan(request)

    durations = [
        int((block.ends_at - block.starts_at).total_seconds() // 60) for block in result.blocks
    ]
    assert durations == [90, 90, 90, 30]
    assert max(durations) <= 120


def test_no_block_exceeds_the_maximum() -> None:
    request = _request(
        minutes=600,
        preferred=120,
        max_block=120,
        slots=((MONDAY, MONDAY + timedelta(hours=24)),),
    )

    result = GreedyPlanner().plan(request)

    for block in result.blocks:
        minutes = int((block.ends_at - block.starts_at).total_seconds() // 60)
        assert minutes <= 120


def test_preferences_do_not_change_the_answer_without_a_daily_limit() -> None:
    """A host that never set preferences schedules exactly what v1.2 scheduled."""
    plain = _request(minutes=300, slots=((MONDAY, MONDAY + timedelta(hours=12)),))
    with_preference = _request(
        minutes=300,
        preferred=0,
        max_block=120,
        slots=((MONDAY, MONDAY + timedelta(hours=12)),),
    )

    first = GreedyPlanner().plan(plain)
    second = GreedyPlanner().plan(with_preference)

    assert [(b.starts_at, b.ends_at) for b in first.blocks] == [
        (b.starts_at, b.ends_at) for b in second.blocks
    ]


async def _manual_block(harness, task_id):
    """One user-owned block, placed where the planner will try to work."""
    from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin

    block = PlanBlock(
        task_id=task_id,
        starts_at=MONDAY + timedelta(hours=2),
        ends_at=MONDAY + timedelta(hours=3),
        created_at=MONDAY,
        updated_at=MONDAY,
        origin=PlanBlockOrigin.MANUAL,
    )
    return await harness.commitments.add_plan_block(block)


# ----------------------------------------------------------------------- replan


async def test_a_replan_proposes_without_changing_anything(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    await harness.create_task("写报告", estimated_minutes=120)

    detail = await harness.planner.create_replan()

    assert detail.proposal.mode is ProposalMode.REPLACE_FUTURE
    assert detail.proposal.status is PlanProposalStatus.PENDING
    # Nothing is planned until the user says so.
    assert await harness.commitments.list_plan_blocks_in_range(
        query_start=MONDAY - timedelta(days=2), query_end=MONDAY + timedelta(days=14)
    ) == []


async def test_applying_a_replan_supersedes_only_future_planner_blocks(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    task = await harness.create_task("写报告", estimated_minutes=120)
    manual = await _manual_block(harness, task.id)

    first = await harness.planner.create_week_proposal()
    await harness.planner.apply_proposal(str(first.proposal.id))
    before = await harness.commitments.list_plan_blocks_in_range(
        query_start=MONDAY - timedelta(days=2), query_end=MONDAY + timedelta(days=14)
    )
    assert before

    replan = await harness.planner.create_replan()
    await harness.planner.apply_proposal(str(replan.proposal.id))

    after = await harness.commitments.list_plan_blocks_in_range(
        query_start=MONDAY - timedelta(days=2), query_end=MONDAY + timedelta(days=14)
    )
    manual_after = [block for block in after if block.id == manual.id]
    assert manual_after and manual_after[0].is_active, "a manual block is never replaced"

    # Superseded blocks are history, so the *display* query deliberately does not return them; the
    # audit trail is read directly, which is also how integrity reads it.
    import sqlite3

    connection = sqlite3.connect(str(harness.database.path))
    connection.row_factory = sqlite3.Row
    try:
        history = [
            dict(row)
            for row in connection.execute(
                "SELECT origin, cancelled_at, superseded_at, superseded_by_proposal_id "
                "FROM plan_blocks WHERE cancelled_at IS NOT NULL"
            )
        ]
    finally:
        connection.close()
    assert history, "the previous automatic plan was replaced"
    assert {row["origin"] for row in history} == {"planner"}
    assert all(row["superseded_at"] is not None for row in history)
    assert {row["superseded_by_proposal_id"] for row in history} == {str(replan.proposal.id)}

    # And the automatic blocks that are still current belong to the new proposal.
    current = [block for block in after if block.is_active and block.origin.value == "planner"]
    assert current
    assert all(block.proposal_id == replan.proposal.id for block in current)


async def test_applying_the_same_replan_twice_is_refused(tmp_path: Path) -> None:
    from assistant.domain.errors import PlanProposalNotPending

    harness = await build_harness(tmp_path)
    await harness.create_task("写报告", estimated_minutes=120)
    replan = await harness.planner.create_replan()
    await harness.planner.apply_proposal(str(replan.proposal.id))

    with pytest.raises(PlanProposalNotPending):
        await harness.planner.apply_proposal(str(replan.proposal.id))


# -------------------------------------------------------------------- conversation


async def test_the_conversation_can_show_and_change_capacity_rules(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("planning.preferences.update", {"day_end": "22:00"})))

    reply = await harness.service.send(thread.id, "以后每天晚上十点以后不要安排任务")

    assert "22:00" in reply.text
    service = _preferences(harness)
    assert (await service.show()).day_end_local == 22 * 60

    harness.queue(plan(operation("planning.preferences.show", {})))
    shown = await harness.service.send(thread.id, "现在一天最多安排几小时")
    assert shown.error_code is None
    assert "每天最多安排" in shown.text


async def test_a_replan_through_the_conversation_still_needs_confirmation(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    await harness.create_task("写报告", estimated_minutes=120)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("plan.replan_week", {})))

    reply = await harness.service.send(thread.id, "这周太满了，帮我重新安排一下")

    # A proposal is not an application: proposing is a local write, applying is what needs a human.
    assert reply.waiting_for_confirmation is False
    summaries = await harness.planning.list_proposal_summaries(limit=5)
    assert [summary.proposal.mode for summary in summaries] == [ProposalMode.REPLACE_FUTURE]
    assert summaries[0].proposal.status is PlanProposalStatus.PENDING
    assert await harness.commitments.list_plan_blocks_in_range(
        query_start=MONDAY - timedelta(days=2), query_end=MONDAY + timedelta(days=14)
    ) == []
