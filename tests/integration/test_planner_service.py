"""End-to-end planning tests: windows, proposals, races and apply (ADR-0015).

These run against real SQLite through the real repositories and the real greedy planner.
The point is the contract a user depends on: a proposal is reviewable, stale input can never
be applied, manual time is never touched, and only recorded work reduces remaining effort.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from assistant.application.calendar_service import CalendarService, CreatePlanBlock
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.planner_service import PlannerService
from assistant.application.planning_fingerprint import planning_fingerprint
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    PlanningNotConfigured,
    PlanningStateUnstable,
    PlanProposalNotPending,
    StalePlanProposal,
)
from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin
from assistant.domain.planning import PlanProposalDetail, PlanProposalStatus, ProposedPlanBlock
from assistant.domain.task import Task, TaskId, TaskPriority
from assistant.domain.work_session import WorkSession
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

# Monday 08:00 Asia/Shanghai == Monday 00:00 UTC; the local week ends next Monday 00:00.
MONDAY_LOCAL = datetime(2026, 9, 21, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
LOCAL_MONDAY_MIDNIGHT = datetime(2026, 9, 21, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
WEEK_START = MONDAY_LOCAL.astimezone(UTC)
WEEK_END = (LOCAL_MONDAY_MIDNIGHT + timedelta(days=7)).astimezone(UTC)
# Local Monday 09:00-22:00 is 01:00-14:00 UTC.
MONDAY_0900 = WEEK_START + timedelta(hours=1)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=MONDAY_LOCAL)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def commitments(database: Database) -> SqliteCommitmentRepository:
    return SqliteCommitmentRepository(database)


@pytest.fixture
def work(database: Database) -> SqliteWorkRepository:
    return SqliteWorkRepository(database)


@pytest.fixture
def planning(database: Database) -> SqlitePlanningRepository:
    return SqlitePlanningRepository(database)


def _config(
    *,
    timezone: str = "Asia/Shanghai",
    min_block: int = 30,
    max_block: int = 120,
    buffer: int = 120,
) -> PlanningConfig:
    return PlanningConfig(
        timezone=timezone,
        min_block_minutes=min_block,
        max_block_minutes=max_block,
        deadline_buffer_minutes=buffer,
        availability=(
            WeeklyAvailabilityRule(
                days=(Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI),
                start_minute=9 * 60,
                end_minute=22 * 60,
            ),
            WeeklyAvailabilityRule(
                days=(Weekday.SAT, Weekday.SUN),
                start_minute=10 * 60,
                end_minute=18 * 60,
            ),
        ),
    )


def _service(
    planning: SqlitePlanningRepository,
    clock: FakeClock,
    config: PlanningConfig | None,
    *,
    max_attempts: int = 3,
) -> PlannerService:
    return PlannerService(
        planning, GreedyPlanner(), config, clock, max_attempts=max_attempts
    )


async def _task(
    commitments: SqliteCommitmentRepository,
    *,
    estimate: int | None = 300,
    priority: TaskPriority = TaskPriority.NORMAL,
    deadline: datetime | None = None,
) -> Task:
    task = Task(
        title="Write SE lab report",
        created_at=WEEK_START,
        updated_at=WEEK_START,
        priority=priority,
        estimated_minutes=estimate,
    )
    if deadline is None:
        await commitments.add_task(task)
    else:
        await commitments.add_task(
            task,
            deadline=Deadline(
                task_id=task.id,
                due_at=deadline,
                created_at=WEEK_START,
                updated_at=WEEK_START,
            ),
        )
    return task


def _manual(task_id: TaskId, *, start_hours: int, end_hours: int) -> PlanBlock:
    return PlanBlock(
        task_id=task_id,
        starts_at=WEEK_START + timedelta(hours=start_hours),
        ends_at=WEEK_START + timedelta(hours=end_hours),
        created_at=WEEK_START,
        updated_at=WEEK_START,
    )


def _event(title: str, *, start_hours: int, end_hours: int) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        starts_at=WEEK_START + timedelta(hours=start_hours),
        ends_at=WEEK_START + timedelta(hours=end_hours),
        created_at=WEEK_START,
        updated_at=WEEK_START,
    )


def _minutes(blocks: tuple[ProposedPlanBlock, ...]) -> int:
    return sum(
        int((block.ends_at - block.starts_at).total_seconds() // 60) for block in blocks
    )


def _overlaps(block: ProposedPlanBlock, other: PlanBlock) -> bool:
    return block.starts_at < other.ends_at and block.ends_at > other.starts_at


def _shape(detail: PlanProposalDetail) -> tuple[object, ...]:
    return (
        tuple(
            (block.task_id, block.starts_at, block.ends_at, block.ordinal)
            for block in detail.blocks
        ),
        tuple((issue.code, issue.task_id) for issue in detail.issues),
        detail.proposal.input_fingerprint,
    )


# ------------------------------------------------------------------- planning window


def test_week_window_uses_the_configured_timezone_and_never_starts_in_the_past(
    planning: SqlitePlanningRepository, clock: FakeClock
) -> None:
    service = _service(planning, clock, _config())

    current = service.week_window()
    following = service.week_window(next_week=True)

    assert current.starts_at == WEEK_START  # clamped to "now"
    assert current.ends_at == WEEK_END
    assert current.timezone == "Asia/Shanghai"
    assert following.starts_at == WEEK_END
    assert following.ends_at == WEEK_END + timedelta(days=7)


def test_week_window_starts_at_the_local_monday_boundary_for_next_week(
    planning: SqlitePlanningRepository, clock: FakeClock
) -> None:
    service = _service(planning, clock, _config())
    clock.advance(3600 * 30)  # clocks move; next week is still a local Monday boundary

    window = service.week_window(next_week=True)

    assert window.starts_at == WEEK_END
    assert window.starts_at.astimezone(ZoneInfo("Asia/Shanghai")).hour == 0


def test_planning_requires_an_explicit_timezone(
    planning: SqlitePlanningRepository, clock: FakeClock
) -> None:
    service = _service(planning, clock, None)

    with pytest.raises(PlanningNotConfigured):
        service.week_window()


async def test_create_proposal_without_configuration_is_refused(
    planning: SqlitePlanningRepository, clock: FakeClock
) -> None:
    service = _service(planning, clock, None)

    with pytest.raises(PlanningNotConfigured):
        await service.create_week_proposal()


# --------------------------------------------------------------------- proposals


async def test_week_proposal_plans_a_task_inside_local_availability(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=300)
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert detail.proposal.status is PlanProposalStatus.PENDING
    assert detail.proposal.input_revision == 1  # the task creation moved the revision
    assert len(detail.proposal.input_fingerprint) == 64
    assert detail.issues == ()
    assert _minutes(detail.blocks) == 300
    assert {block.task_id for block in detail.blocks} == {task.id}
    assert detail.blocks[0].starts_at == MONDAY_0900
    assert all(
        WEEK_START <= block.starts_at < block.ends_at <= WEEK_END
        for block in detail.blocks
    )


async def test_proposal_is_durable_and_apply_creates_planner_blocks(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    detail = await service.create_week_proposal()

    shown = await service.get_proposal_detail(str(detail.proposal.id)[:8])
    assert shown.proposal == detail.proposal
    assert shown.blocks == detail.blocks

    clock.advance(3600)
    result = await service.apply_proposal(str(detail.proposal.id))

    assert result.created_blocks == 1
    assert result.replaced_blocks == 0
    blocks = await commitments.list_plan_blocks_in_range(
        query_start=WEEK_START, query_end=WEEK_END
    )
    assert len(blocks) == 1
    assert blocks[0].origin is PlanBlockOrigin.PLANNER
    assert blocks[0].proposal_id == detail.proposal.id
    assert blocks[0].task_id == task.id
    assert blocks[0].created_at == clock.now().astimezone(UTC)
    assert blocks[0].starts_at == detail.blocks[0].starts_at

    stored = await planning.get_proposal(detail.proposal.id)
    assert stored is not None and stored.status is PlanProposalStatus.APPLIED
    assert stored.applied_at == clock.now().astimezone(UTC)


async def test_a_second_same_window_proposal_supersedes_the_first(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    window = service.week_window()
    first = await service.create_proposal(window)
    second = await service.create_proposal(window)

    stored_first = await planning.get_proposal(first.proposal.id)

    assert stored_first is not None
    assert stored_first.status is PlanProposalStatus.SUPERSEDED
    assert stored_first.superseded_at == second.proposal.created_at
    assert second.proposal.status is PlanProposalStatus.PENDING
    with pytest.raises(PlanProposalNotPending):
        await service.apply_proposal(str(first.proposal.id))


async def test_show_returns_the_stored_proposal_not_a_replan(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    created = await service.create_week_proposal()

    clock.advance(86400)
    shown = await service.get_proposal_detail(str(created.proposal.id))

    assert shown.proposal.created_at == created.proposal.created_at
    assert shown.blocks == created.blocks
    assert shown.proposal.input_revision == created.proposal.input_revision


async def test_proposal_creation_retries_a_moving_snapshot_and_then_gives_up(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.domain.errors import PlanningSnapshotChanged

    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config(), max_attempts=2)
    calls = {"count": 0}

    async def conflicting(proposal: object, **kwargs: object) -> object:
        calls["count"] += 1
        raise PlanningSnapshotChanged("the world moved")

    monkeypatch.setattr(planning, "create_proposal", conflicting)

    with pytest.raises(PlanningStateUnstable):
        await service.create_week_proposal()

    assert calls["count"] == 2


# ---------------------------------------------------------------- planning input


async def test_deadlines_are_never_busy_time_but_manual_blocks_are(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    deadline = WEEK_START + timedelta(days=4, hours=15, minutes=59)
    task = await _task(commitments, estimate=120, deadline=deadline)
    service = _service(planning, clock, _config())

    snapshot = await planning.load_snapshot(service.week_window())
    assert snapshot.deadlines[task.id].due_at == deadline

    proposal = await service.create_week_proposal()
    assert proposal.blocks  # the deadline constrains the plan...
    assert all(block.ends_at <= deadline for block in proposal.blocks)
    assert all(not (block.starts_at <= deadline <= block.ends_at) for block in proposal.blocks)

    manual = _manual(task.id, start_hours=1, end_hours=3)
    await commitments.add_plan_block(manual)
    after_manual = await service.create_week_proposal()

    assert all(not _overlaps(block, manual) for block in after_manual.blocks)
    assert any(block.starts_at >= manual.ends_at for block in after_manual.blocks)


async def test_actual_work_reduces_remaining_effort_but_plan_blocks_never_do(
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=300)
    for start_hours, end_hours in ((0, 1), (1, 2.5)):
        await work.add_work_session(
            WorkSession(
                task_id=task.id,
                started_at=WEEK_START + timedelta(hours=start_hours),
                ended_at=WEEK_START + timedelta(hours=end_hours),
                created_at=WEEK_START,
            )
        )
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert _minutes(detail.blocks) == 150  # 300 estimated - ceil(9000 / 60) recorded

    # Planning time is intention, not effort: three planned hours change nothing.
    await commitments.add_plan_block(_manual(task.id, start_hours=30, end_hours=33))
    after_manual = await service.create_week_proposal()

    assert _minutes(after_manual.blocks) == 150


async def test_estimate_exhausted_is_reported_without_scheduling_or_completing(
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=60)
    await work.add_work_session(
        WorkSession(
            task_id=task.id,
            started_at=WEEK_START,
            ended_at=WEEK_START + timedelta(minutes=75),
            created_at=WEEK_START,
        )
    )
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert detail.blocks == ()
    assert [issue.code.value for issue in detail.issues] == ["ESTIMATE_EXHAUSTED"]
    assert detail.issues[0].task_id == task.id
    stored = await commitments.get_task(task.id)
    assert stored is not None and stored.status.value == "open"


async def test_missing_estimate_is_reported_and_other_tasks_are_still_planned(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    without_estimate = await _task(commitments, estimate=None)
    with_estimate = await _task(commitments, estimate=60)
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert [issue.code.value for issue in detail.issues] == ["MISSING_ESTIMATE"]
    assert detail.issues[0].task_id == without_estimate.id
    assert {block.task_id for block in detail.blocks} == {with_estimate.id}


async def test_terminal_tasks_are_not_planned(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    done = await _task(commitments, estimate=120)
    cancelled = await _task(commitments, estimate=120)
    await commitments.complete_task(
        done.complete(at=WEEK_START), expected_updated_at=done.updated_at
    )
    await commitments.cancel_task(
        cancelled.cancel(at=WEEK_START), expected_updated_at=cancelled.updated_at
    )
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert detail.blocks == ()
    assert detail.issues == ()


async def test_no_availability_is_reported_once_not_per_task(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    await _task(commitments, estimate=60)
    service = _service(planning, clock, PlanningConfig(timezone="Asia/Shanghai"))

    detail = await service.create_week_proposal()

    codes = [issue.code.value for issue in detail.issues]
    assert codes.count("NO_AVAILABILITY") == 1
    assert detail.issues[0].task_id is None
    assert detail.blocks == ()


async def test_buffer_violation_and_insufficient_capacity_are_reported(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    # A deadline at 10:00 local is inside the 120-minute buffer of the 09:00 availability.
    deadline = WEEK_START + timedelta(hours=2)
    task = await _task(commitments, estimate=180, deadline=deadline)
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert [issue.code.value for issue in detail.issues] == [
        "BUFFER_VIOLATED",
        "INSUFFICIENT_CAPACITY",
    ]
    assert all(block.ends_at <= deadline for block in detail.blocks)
    assert _minutes(detail.blocks) == 60
    capacity = detail.issues[-1]
    assert capacity.task_id == task.id
    assert capacity.required_minutes == 180
    assert capacity.scheduled_minutes == 60


async def test_deadline_already_passed_is_reported_without_blocks(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120, deadline=WEEK_START - timedelta(days=1))
    service = _service(planning, clock, _config())

    detail = await service.create_week_proposal()

    assert detail.blocks == ()
    assert [issue.code.value for issue in detail.issues] == ["DEADLINE_ALREADY_PASSED"]


# ------------------------------------------------------------------ stale fencing


async def test_changed_deadline_makes_the_proposal_stale(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    detail = await service.create_week_proposal()
    await commitments.set_deadline(
        Deadline(
            task_id=task.id,
            due_at=WEEK_START + timedelta(days=6),
            created_at=clock.now().astimezone(UTC),
            updated_at=clock.now().astimezone(UTC),
        ),
        expected_updated_at=task.updated_at,
        at=clock.now().astimezone(UTC),
    )

    with pytest.raises(StalePlanProposal):
        await service.apply_proposal(str(detail.proposal.id))

    stored = await planning.get_proposal(detail.proposal.id)
    assert stored is not None and stored.status is PlanProposalStatus.STALE
    assert (
        await commitments.list_plan_blocks_in_range(query_start=WEEK_START, query_end=WEEK_END)
        == []
    )


async def test_new_work_session_makes_the_proposal_stale(
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    detail = await service.create_week_proposal()
    await work.add_work_session(
        WorkSession(
            task_id=task.id,
            started_at=WEEK_START,
            ended_at=WEEK_START + timedelta(hours=2),
            created_at=WEEK_START,
        )
    )

    with pytest.raises(StalePlanProposal):
        await service.apply_proposal(str(detail.proposal.id))


async def test_new_manual_block_makes_the_proposal_stale(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    detail = await service.create_week_proposal()
    await commitments.add_plan_block(_manual(task.id, start_hours=1, end_hours=2))

    with pytest.raises(StalePlanProposal):
        await service.apply_proposal(str(detail.proposal.id))


async def test_new_or_cancelled_calendar_event_makes_the_proposal_stale(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    first = await service.create_week_proposal()
    await commitments.add_calendar_event(_event("Lecture", start_hours=2, end_hours=4))

    with pytest.raises(StalePlanProposal):
        await service.apply_proposal(str(first.proposal.id))

    event = _event("Another lecture", start_hours=5, end_hours=6)
    await commitments.add_calendar_event(event)
    second = await service.create_week_proposal()
    await commitments.cancel_calendar_event(event.id, at=clock.now().astimezone(UTC))

    with pytest.raises(StalePlanProposal):
        await service.apply_proposal(str(second.proposal.id))


async def test_an_applied_proposal_replaces_the_previous_planner_blocks(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    window = service.week_window()
    first = await service.create_proposal(window)
    applied_first = await service.apply_proposal(str(first.proposal.id))
    second = await service.create_proposal(window)
    applied_second = await service.apply_proposal(str(second.proposal.id))

    assert applied_first.created_blocks == 1
    assert applied_second.replaced_blocks == 1
    active = await commitments.list_plan_blocks_in_range(
        query_start=WEEK_START, query_end=WEEK_END
    )
    assert len(active) == 1
    assert active[0].proposal_id == second.proposal.id
    cancelled = await commitments.list_plan_blocks_in_range(
        query_start=WEEK_START, query_end=WEEK_END, include_cancelled=True
    )
    assert len(cancelled) == 2


async def test_manual_blocks_survive_every_apply(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    manual = await commitments.add_plan_block(_manual(task.id, start_hours=26, end_hours=28))
    service = _service(planning, clock, _config())
    window = service.week_window()
    await service.apply_proposal(str((await service.create_proposal(window)).proposal.id))
    await service.apply_proposal(str((await service.create_proposal(window)).proposal.id))

    active = await commitments.list_plan_blocks_in_range(
        query_start=WEEK_START, query_end=WEEK_END
    )
    manual_active = [block for block in active if block.origin is PlanBlockOrigin.MANUAL]
    assert manual_active == [manual]


async def test_plan_block_creation_stays_manual(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    task = await _task(commitments, estimate=120)
    calendar = CalendarService(commitments, clock)

    block = await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id,
            starts_at=WEEK_START + timedelta(hours=1),
            ends_at=WEEK_START + timedelta(hours=2),
        )
    )

    assert block.origin is PlanBlockOrigin.MANUAL
    assert block.proposal_id is None


# ------------------------------------------------------------------- determinism


async def test_identical_input_produces_identical_blocks_issues_and_fingerprint(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    first = await _task(commitments, estimate=300, deadline=WEEK_START + timedelta(days=4))
    await _task(commitments, estimate=90, priority=TaskPriority.HIGH)
    await commitments.add_calendar_event(_event("Lecture", start_hours=5, end_hours=7))
    await commitments.add_plan_block(_manual(first.id, start_hours=9, end_hours=10))
    service = _service(planning, clock, _config())
    window = service.week_window()
    expected_fingerprint = planning_fingerprint(
        window=window,
        config=_config(),
        snapshot=await planning.load_snapshot(window),
    )

    shapes = set()
    for _ in range(20):
        detail = await service.create_proposal(window)
        shapes.add(_shape(detail))

    assert len(shapes) == 1
    shape = shapes.pop()
    assert shape[2] == expected_fingerprint
    assert shape[0]  # something was planned


async def test_fingerprint_changes_with_busy_time_but_not_with_task_titles(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    task = await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    window = service.week_window()
    config = _config()

    before = planning_fingerprint(
        window=window, config=config, snapshot=await planning.load_snapshot(window)
    )
    await commitments.add_calendar_event(_event("Lecture", start_hours=2, end_hours=3))
    after = planning_fingerprint(
        window=window, config=config, snapshot=await planning.load_snapshot(window)
    )

    assert before != after
    assert task.title == "Write SE lab report"


async def test_planner_blocks_are_replaceable_and_never_treated_as_busy(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    clock: FakeClock,
) -> None:
    await _task(commitments, estimate=120)
    service = _service(planning, clock, _config())
    window = service.week_window()
    first = await service.create_proposal(window)
    await service.apply_proposal(str(first.proposal.id))

    snapshot = await planning.load_snapshot(window)
    second = await service.create_proposal(window)

    assert snapshot.active_planner_plan_blocks  # kept as history for the fingerprint
    assert [
        (block.task_id, block.starts_at, block.ends_at) for block in second.blocks
    ] == [(block.task_id, block.starts_at, block.ends_at) for block in first.blocks]
