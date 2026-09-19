"""Integration tests for the commitment application services (ADR-0014).

These are the domain boundaries that matter most: a deadline is not busy time, a plan is not
actual work, terminal tasks cannot be replanned, and id prefixes are never guessed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from assistant.application.calendar_service import (
    CalendarService,
    CreateCalendarEvent,
    CreatePlanBlock,
)
from assistant.application.task_service import CreateTask, EditTask, TaskService
from assistant.application.work_service import WorkService
from assistant.domain.errors import (
    AmbiguousId,
    InvalidTimeInterval,
    StaleTaskUpdate,
    TaskNotFound,
    TaskNotOpen,
)
from assistant.domain.task import TaskPriority, TaskStatus
from assistant.domain.time_interval import BusyIntervalKind
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


@pytest.fixture
def commitments(database: Database) -> SqliteCommitmentRepository:
    return SqliteCommitmentRepository(database)


@pytest.fixture
def tasks(commitments: SqliteCommitmentRepository, clock: FakeClock) -> TaskService:
    return TaskService(commitments, clock)


@pytest.fixture
def calendar(commitments: SqliteCommitmentRepository, clock: FakeClock) -> CalendarService:
    return CalendarService(commitments, clock)


@pytest.fixture
def work(
    database: Database, commitments: SqliteCommitmentRepository, clock: FakeClock
) -> WorkService:
    return WorkService(SqliteWorkRepository(database), commitments, clock)


async def test_task_lifecycle_with_deadline(tasks: TaskService, clock: FakeClock) -> None:
    due_at = NOW + timedelta(days=30)
    task = await tasks.create_task(
        CreateTask(
            title="  Write SE lab report  ",
            description="chapter 3",
            priority=TaskPriority.HIGH,
            estimated_minutes=300,
            due_at=due_at,
        )
    )

    assert task.title == "Write SE lab report"  # the service trims
    assert task.status is TaskStatus.OPEN
    deadline = await tasks.get_deadline(task.id)
    assert deadline is not None and deadline.due_at == due_at

    clock.advance(60)
    edited = await tasks.update_task(
        task.id,
        EditTask(
            title="Write OS lab report",
            description=None,
            priority=TaskPriority.LOW,
            estimated_minutes=120,
        ),
    )
    assert edited.title == "Write OS lab report"
    assert edited.updated_at == clock.now()

    result = await tasks.complete_task(task.id)
    assert result.task.status is TaskStatus.COMPLETED
    assert (await tasks.list_tasks()) == []
    assert len(await tasks.list_tasks(include_terminal=True)) == 1


async def test_second_edit_of_a_stale_task_is_rejected(
    tasks: TaskService, commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    task = await tasks.create_task(CreateTask(title="Original"))
    clock.advance(60)
    await tasks.update_task(
        task.id,
        EditTask(
            title="First edit",
            description=None,
            priority=TaskPriority.NORMAL,
            estimated_minutes=None,
        ),
    )

    with pytest.raises(StaleTaskUpdate):
        # A caller that still holds the original version cannot overwrite the newer one.
        await commitments.update_task(
            task.update_details(
                title="Second edit",
                description=None,
                priority=TaskPriority.NORMAL,
                estimated_minutes=None,
                at=clock.now(),
            ),
            expected_updated_at=task.updated_at,
        )


async def test_completion_cancels_future_plan_blocks_and_keeps_deadline(
    tasks: TaskService,
    calendar: CalendarService,
    clock: FakeClock,
    commitments: SqliteCommitmentRepository,
) -> None:
    due_at = NOW + timedelta(days=30)
    task = await tasks.create_task(CreateTask(title="Write report", due_at=due_at))
    past = await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id,
            starts_at=NOW - timedelta(hours=4),
            ends_at=NOW - timedelta(hours=2),
        )
    )
    future = await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id, starts_at=NOW + timedelta(hours=3), ends_at=NOW + timedelta(hours=5)
        )
    )

    clock.advance(60)
    result = await tasks.complete_task(task.id)

    stored_past = await commitments.get_plan_block(past.id)
    stored_future = await commitments.get_plan_block(future.id)
    assert result.cancelled_plan_blocks == 1
    assert stored_past is not None and stored_past.is_active
    assert stored_future is not None and not stored_future.is_active
    assert stored_future.cancelled_at == clock.now()
    deadline = await tasks.get_deadline(task.id)
    assert deadline is not None and deadline.due_at == due_at


async def test_deadlines_cannot_change_on_terminal_tasks(
    tasks: TaskService, clock: FakeClock
) -> None:
    task = await tasks.create_task(CreateTask(title="Write report"))
    await tasks.complete_task(task.id)
    clock.advance(60)

    with pytest.raises(TaskNotOpen):
        await tasks.set_deadline(task.id, NOW + timedelta(days=1))
    with pytest.raises(TaskNotOpen):
        await tasks.clear_deadline(task.id)


async def test_plan_blocks_require_an_open_task(
    tasks: TaskService, calendar: CalendarService
) -> None:
    task = await tasks.create_task(CreateTask(title="Write report"))
    await tasks.cancel_task(task.id)
    window = CreatePlanBlock(
        task_id=task.id, starts_at=NOW, ends_at=NOW + timedelta(hours=1)
    )

    with pytest.raises(TaskNotOpen):
        await calendar.create_plan_block(window)
    with pytest.raises(TaskNotFound):
        await calendar.create_plan_block(
            CreatePlanBlock(
                task_id=UUID(int=999), starts_at=NOW, ends_at=NOW + timedelta(hours=1)
            )
        )


async def test_work_can_be_backfilled_after_completion(
    tasks: TaskService, work: WorkService, clock: FakeClock
) -> None:
    task = await tasks.create_task(CreateTask(title="Write report", estimated_minutes=300))
    await tasks.complete_task(task.id)
    clock.advance(3600)

    first = await work.record_session(
        task_id=task.id, started_at=NOW, ended_at=NOW + timedelta(minutes=60)
    )
    second = await work.record_session(
        task_id=task.id,
        started_at=NOW + timedelta(hours=2),
        ended_at=NOW + timedelta(hours=3, minutes=30),
    )

    assert [session.id for session in await work.list_task_sessions(task.id)] == [
        first.id,
        second.id,
    ]
    assert await work.get_task_actual_seconds(task.id) == 9000
    with pytest.raises(TaskNotFound):
        await work.record_session(
            task_id=UUID(int=999), started_at=NOW, ended_at=NOW + timedelta(minutes=5)
        )


async def test_actual_effort_ignores_planned_time(
    tasks: TaskService, calendar: CalendarService, work: WorkService
) -> None:
    task = await tasks.create_task(CreateTask(title="Write report", estimated_minutes=300))
    await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id, starts_at=NOW, ends_at=NOW + timedelta(hours=3)
        )
    )

    assert await work.get_task_actual_seconds(task.id) == 0

    await work.record_session(
        task_id=task.id, started_at=NOW, ended_at=NOW + timedelta(minutes=60)
    )
    await work.record_session(
        task_id=task.id,
        started_at=NOW + timedelta(hours=4),
        ended_at=NOW + timedelta(hours=5, minutes=30),
    )

    stored = await tasks.require_task(task.id)
    assert stored.estimated_minutes == 300
    assert await work.get_task_actual_seconds(task.id) == 9000


async def test_busy_intervals_include_events_and_plans_but_never_deadlines(
    tasks: TaskService, calendar: CalendarService
) -> None:
    """The core domain boundary: a deadline occupies no time."""
    friday_deadline = datetime(2026, 9, 25, 23, 59, tzinfo=UTC)
    wednesday_start = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    task = await tasks.create_task(
        CreateTask(title="Write report", due_at=friday_deadline)
    )
    block = await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id,
            starts_at=wednesday_start,
            ends_at=wednesday_start + timedelta(hours=2),
        )
    )
    lecture = await calendar.create_event(
        CreateCalendarEvent(
            title="SE lecture",
            starts_at=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
    )

    intervals = await calendar.get_busy_intervals(
        query_start=datetime(2026, 9, 20, tzinfo=UTC),
        query_end=datetime(2026, 9, 27, tzinfo=UTC),
    )

    kinds = {interval.source_id: interval.source_kind for interval in intervals}
    assert kinds[lecture.id] is BusyIntervalKind.CALENDAR_EVENT
    assert kinds[block.id] is BusyIntervalKind.PLAN_BLOCK
    assert len(intervals) == 2  # the deadline contributes nothing
    assert all(interval.ends_at <= datetime(2026, 9, 27, tzinfo=UTC) for interval in intervals)

    deadline_only_window = await calendar.get_busy_intervals(
        query_start=friday_deadline - timedelta(minutes=1),
        query_end=friday_deadline + timedelta(minutes=1),
    )
    assert deadline_only_window == []


async def test_calendar_range_must_be_positive(calendar: CalendarService) -> None:
    with pytest.raises(InvalidTimeInterval, match="query_end"):
        await calendar.get_busy_intervals(query_start=NOW, query_end=NOW)


async def test_id_prefixes_resolve_unambiguously(
    commitments: SqliteCommitmentRepository, database: Database, clock: FakeClock
) -> None:
    # The two ids share the "0000000" prefix and differ from its eighth character on.
    ids = iter([UUID(int=1), UUID(int=2**96)])
    service = TaskService(commitments, clock, new_task_id_factory=lambda: next(ids))
    first = await service.create_task(CreateTask(title="First"))
    second = await service.create_task(CreateTask(title="Second"))

    assert await service.resolve_task_id(str(first.id)) == first.id
    assert await service.resolve_task_id("00000000") == first.id  # unique prefix
    assert await service.resolve_task_id("00000001") == second.id
    with pytest.raises(TaskNotFound):
        await service.resolve_task_id("ffffffff")
    with pytest.raises(AmbiguousId):
        # Both ids share this prefix, so resolution refuses to guess.
        await service.resolve_task_id("0000000")
    assert second.id != first.id
