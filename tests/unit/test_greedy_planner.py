"""Unit tests for the pure deterministic greedy planner (ADR-0015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from assistant.application.greedy_planner import GreedyPlanner
from assistant.domain.planning import (
    PlanningIssueCode,
    PlanningRequest,
    PlanningTask,
    PlanningWindow,
)
from assistant.domain.task import TaskPriority

MONDAY = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)  # 09:00 Asia/Shanghai
WINDOW = PlanningWindow(
    starts_at=MONDAY, ends_at=MONDAY + timedelta(days=3), timezone="Asia/Shanghai"
)
DAY_SLOT = (MONDAY, MONDAY + timedelta(hours=4))


def _task(
    *,
    title: str = "task",
    priority: TaskPriority = TaskPriority.NORMAL,
    remaining_minutes: int = 60,
    estimated_minutes: int | None = 60,
    actual_seconds: int = 0,
    deadline: datetime | None = None,
    created_at: datetime = MONDAY,
    task_id: object | None = None,
) -> PlanningTask:
    return PlanningTask(
        task_id=task_id or uuid4(),  # type: ignore[arg-type]
        title=title,
        priority=priority,
        created_at=created_at,
        estimated_minutes=estimated_minutes,
        actual_seconds=actual_seconds,
        remaining_minutes=remaining_minutes,
        deadline=deadline,
    )


def _request(
    tasks: tuple[PlanningTask, ...],
    *,
    availability: tuple[tuple[datetime, datetime], ...] = (DAY_SLOT,),
    busy: tuple[tuple[datetime, datetime], ...] = (),
    min_block: int = 30,
    max_block: int = 120,
    buffer_minutes: int = 120,
) -> PlanningRequest:
    return PlanningRequest(
        window=WINDOW,
        tasks=tasks,
        availability=availability,
        busy_intervals=busy,
        min_block_minutes=min_block,
        max_block_minutes=max_block,
        deadline_buffer_minutes=buffer_minutes,
    )


def test_no_tasks_produces_nothing() -> None:
    result = GreedyPlanner().plan(_request(()))

    assert result.blocks == ()
    assert result.issues == ()


def test_single_task_in_a_single_slot() -> None:
    task = _task(remaining_minutes=60)

    result = GreedyPlanner().plan(_request((task,)))

    assert len(result.blocks) == 1
    assert result.blocks[0].starts_at == MONDAY
    assert result.blocks[0].ends_at == MONDAY + timedelta(minutes=60)
    assert result.blocks[0].ordinal == 0
    assert result.issues == ()


def test_long_task_is_split_at_max_block_size() -> None:
    task = _task(remaining_minutes=300)
    six_hours = (MONDAY, MONDAY + timedelta(hours=6))

    result = GreedyPlanner().plan(_request((task,), availability=(six_hours,), max_block=120))

    durations = [
        int((block.ends_at - block.starts_at).total_seconds() // 60) for block in result.blocks
    ]
    assert durations == [120, 120, 60]
    assert [block.ordinal for block in result.blocks] == [0, 1, 2]
    assert result.issues == ()


def test_minimum_block_size_is_respected_and_tiny_final_blocks_are_allowed() -> None:
    task = _task(remaining_minutes=45)

    small = GreedyPlanner().plan(
        _request((task,), availability=((MONDAY, MONDAY + timedelta(minutes=20)),))
    )
    finishing = GreedyPlanner().plan(_request((task,), min_block=60))

    assert small.blocks == ()
    assert small.issues[0].code is PlanningIssueCode.WINDOW_CAPACITY_EXHAUSTED
    assert len(finishing.blocks) == 1  # 45m < min_block, but it finishes the task
    block = finishing.blocks[0]
    assert int((block.ends_at - block.starts_at).total_seconds()) == 45 * 60


def test_busy_time_is_subtracted_and_adjacent_ranges_merge() -> None:
    task = _task(remaining_minutes=60)
    first_hour = (MONDAY, MONDAY + timedelta(hours=1))
    second_hour = (MONDAY + timedelta(hours=1), MONDAY + timedelta(hours=2))

    result = GreedyPlanner().plan(_request((task,), busy=(first_hour, second_hour)))

    assert len(result.blocks) == 1
    assert result.blocks[0].starts_at == MONDAY + timedelta(hours=2)


def test_blocks_never_cross_an_availability_gap() -> None:
    task = _task(remaining_minutes=90)
    morning = (MONDAY, MONDAY + timedelta(minutes=30))
    afternoon = (MONDAY + timedelta(hours=3), MONDAY + timedelta(hours=4))

    result = GreedyPlanner().plan(_request((task,), availability=(morning, afternoon)))

    assert [block.starts_at for block in result.blocks] == [
        MONDAY,
        MONDAY + timedelta(hours=3),
    ]
    assert all(
        block.starts_at >= MONDAY and block.ends_at <= MONDAY + timedelta(hours=4)
        for block in result.blocks
    )


def test_deadline_is_a_hard_constraint() -> None:
    deadline = MONDAY + timedelta(hours=1)
    task = _task(remaining_minutes=150, deadline=deadline)

    result = GreedyPlanner().plan(_request((task,)))

    assert result.blocks  # it schedules what it can
    assert all(block.ends_at <= deadline for block in result.blocks)
    assert result.issues[-1].code is PlanningIssueCode.INSUFFICIENT_CAPACITY
    assert result.issues[-1].required_minutes == 150
    assert result.issues[-1].scheduled_minutes == 60


def test_deadline_buffer_is_preferred_and_violations_are_reported() -> None:
    deadline = MONDAY + timedelta(hours=3)
    task = _task(remaining_minutes=60, deadline=deadline)

    inside_buffer = GreedyPlanner().plan(_request((task,), buffer_minutes=120))
    using_buffer = GreedyPlanner().plan(
        _request((task,), buffer_minutes=180),  # soft deadline lands before the window
    )

    assert inside_buffer.issues == ()
    assert all(block.ends_at <= deadline - timedelta(hours=2) for block in inside_buffer.blocks)
    assert [issue.code for issue in using_buffer.issues] == [PlanningIssueCode.BUFFER_VIOLATED]
    assert using_buffer.blocks[0].starts_at == MONDAY


def test_deadline_already_passed_is_reported_and_nothing_is_scheduled() -> None:
    task = _task(remaining_minutes=60, deadline=MONDAY - timedelta(minutes=1))

    result = GreedyPlanner().plan(_request((task,)))

    assert result.blocks == ()
    assert result.issues[0].code is PlanningIssueCode.DEADLINE_ALREADY_PASSED


def test_no_availability_is_reported_once() -> None:
    tasks = (_task(remaining_minutes=60), _task(remaining_minutes=30))

    result = GreedyPlanner().plan(_request(tasks, availability=()))

    codes = [issue.code for issue in result.issues]
    assert codes.count(PlanningIssueCode.NO_AVAILABILITY) == 1
    assert result.issues[0].task_id is None
    assert result.blocks == ()


def test_task_without_deadline_uses_remaining_capacity_after_deadline_tasks() -> None:
    urgent = _task(
        title="urgent",
        remaining_minutes=120,
        deadline=MONDAY + timedelta(hours=2),
        priority=TaskPriority.LOW,
    )
    later = _task(title="later", remaining_minutes=60, deadline=None)

    result = GreedyPlanner().plan(_request((later, urgent)))

    by_task = {block.task_id: block for block in result.blocks}
    assert by_task[urgent.task_id].starts_at == MONDAY
    assert by_task[later.task_id].starts_at == MONDAY + timedelta(hours=2)


def test_window_capacity_exhaustion_uses_its_own_issue_code() -> None:
    task = _task(remaining_minutes=300, deadline=None)

    result = GreedyPlanner().plan(_request((task,)))

    assert result.issues[-1].code is PlanningIssueCode.WINDOW_CAPACITY_EXHAUSTED
    assert "planning window" in result.issues[-1].message


def test_task_ordering_prefers_deadline_then_priority_then_creation() -> None:
    earliest = MONDAY
    later = MONDAY + timedelta(minutes=1)
    high = _task(title="high", priority=TaskPriority.HIGH, created_at=later)
    normal = _task(title="normal", priority=TaskPriority.NORMAL, created_at=earliest)
    deadlined = _task(
        title="deadlined",
        priority=TaskPriority.LOW,
        created_at=later,
        deadline=MONDAY + timedelta(days=1),
    )

    result = GreedyPlanner().plan(_request((normal, high, deadlined)))

    order = [block.task_id for block in result.blocks]
    assert order[0] == deadlined.task_id  # deadline tasks always come first
    assert order[1] == high.task_id  # then priority
    assert order[2] == normal.task_id


def test_zero_remaining_tasks_are_skipped() -> None:
    task = _task(remaining_minutes=0, actual_seconds=7200)

    result = GreedyPlanner().plan(_request((task,)))

    assert result.blocks == ()
    assert result.issues == ()


def test_half_open_boundaries_allow_back_to_back_blocks() -> None:
    first = _task(remaining_minutes=60, created_at=MONDAY)
    second = _task(remaining_minutes=60, created_at=MONDAY + timedelta(minutes=1))

    result = GreedyPlanner().plan(_request((first, second)))

    assert result.blocks[0].ends_at == result.blocks[1].starts_at
    assert result.issues == ()


def test_planning_is_deterministic_across_many_runs() -> None:
    tasks = (
        _task(remaining_minutes=150, deadline=MONDAY + timedelta(days=1)),
        _task(remaining_minutes=45, priority=TaskPriority.HIGH),
        _task(
            remaining_minutes=90,
            priority=TaskPriority.LOW,
            deadline=MONDAY + timedelta(hours=6),
        ),
    )
    request = _request(
        tasks,
        busy=((MONDAY + timedelta(hours=1), MONDAY + timedelta(hours=2)),),
    )
    planner = GreedyPlanner()

    baseline = planner.plan(request)
    for _ in range(100):
        repeated = planner.plan(request)
        assert repeated == baseline
