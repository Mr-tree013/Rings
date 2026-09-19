"""Integration tests for durable commitment persistence (ADR-0014).

Real SQLite: optimistic concurrency, the atomic terminal transition, half-open range queries
and the database-level guarantees are exactly what a mock would fabricate.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    CalendarEventNotFound,
    DeadlineNotFound,
    DuplicateCommitment,
    PlanBlockNotFound,
    StaleTaskUpdate,
    TaskNotFound,
    TaskNotOpen,
)
from assistant.domain.plan_block import PlanBlock
from assistant.domain.task import Task, TaskPriority, TaskStatus
from assistant.domain.work_session import WorkSession
from assistant.store import commitment as commitment_store
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
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def repository(database: Database) -> SqliteCommitmentRepository:
    return SqliteCommitmentRepository(database)


@pytest.fixture
def work(database: Database) -> SqliteWorkRepository:
    return SqliteWorkRepository(database)


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "title": "Write SE lab report",
        "created_at": NOW,
        "updated_at": NOW,
        "priority": TaskPriority.HIGH,
        "estimated_minutes": 300,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _block(task_id: object, start_hours: int, end_hours: int) -> PlanBlock:
    starts_at = NOW + timedelta(hours=start_hours)
    return PlanBlock(
        task_id=task_id,  # type: ignore[arg-type]
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=end_hours - start_hours),
        created_at=NOW,
        updated_at=NOW,
    )


async def test_task_round_trip_survives_reopen(
    database: Database, repository: SqliteCommitmentRepository
) -> None:
    task = _task()
    await repository.add_task(task)

    reopened = SqliteCommitmentRepository(Database.at(database.path))
    stored = await reopened.get_task(task.id)

    assert stored == task
    assert (await reopened.list_tasks()) == [task]
    assert await reopened.list_tasks(statuses=(TaskStatus.COMPLETED,)) == []


async def test_task_with_deadline_is_created_atomically(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    deadline = Deadline(
        task_id=task.id, due_at=NOW + timedelta(days=5), created_at=NOW, updated_at=NOW
    )

    await repository.add_task(task, deadline=deadline)

    assert await repository.get_deadline(task.id) == deadline
    listed = await repository.list_deadlines([task.id, uuid4()])
    assert listed == {task.id: deadline}


async def test_duplicate_task_identity_is_rejected(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)

    with pytest.raises(DuplicateCommitment):
        await repository.add_task(task)


async def test_update_task_uses_optimistic_concurrency(
    repository: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    task = _task()
    await repository.add_task(task)
    clock.advance(60)
    edited = task.update_details(
        title="Write OS lab report",
        description=None,
        priority=TaskPriority.LOW,
        estimated_minutes=120,
        at=clock.now(),
    )

    stored = await repository.update_task(edited, expected_updated_at=task.updated_at)

    assert stored.title == "Write OS lab report"
    with pytest.raises(StaleTaskUpdate):
        await repository.update_task(edited, expected_updated_at=task.updated_at)


async def test_terminal_tasks_reject_updates(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    completed = task.complete(NOW + timedelta(hours=1))
    await repository.complete_task(completed, expected_updated_at=task.updated_at)

    with pytest.raises(TaskNotOpen):
        await repository.update_task(completed, expected_updated_at=completed.updated_at)


async def test_completion_cancels_unfinished_plan_blocks_atomically(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    past = _block(task.id, -4, -2)
    current = _block(task.id, -1, 1)
    future = _block(task.id, 3, 5)
    for block in (past, current, future):
        await repository.add_plan_block(block)
    completed_at = NOW

    result = await repository.complete_task(
        task.complete(completed_at), expected_updated_at=task.updated_at
    )

    blocks = {
        block.id: block
        for block in await repository.list_plan_blocks_for_task(
            task.id, include_cancelled=True
        )
    }
    assert result.cancelled_plan_blocks == 2
    assert result.task.status is TaskStatus.COMPLETED
    assert blocks[past.id].is_active  # already finished: planning history
    assert blocks[current.id].cancelled_at == completed_at
    assert blocks[future.id].cancelled_at == completed_at


async def test_cancellation_cancels_unfinished_plan_blocks(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    future = _block(task.id, 3, 5)
    await repository.add_plan_block(future)
    cancelled_at = NOW

    result = await repository.cancel_task(
        task.cancel(cancelled_at), expected_updated_at=task.updated_at
    )

    assert result.task.status is TaskStatus.CANCELLED
    assert result.cancelled_plan_blocks == 1
    stored = await repository.get_plan_block(future.id)
    assert stored is not None and stored.cancelled_at == cancelled_at


async def test_terminal_transition_rolls_back_entirely_on_failure(
    repository: SqliteCommitmentRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task()
    await repository.add_task(task)
    future = _block(task.id, 3, 5)
    await repository.add_plan_block(future)
    monkeypatch.setattr(
        commitment_store,
        "_CANCEL_FUTURE_PLAN_BLOCKS_SQL",
        "UPDATE plan_blocks SET nonexistent = 1",
    )

    with pytest.raises(sqlite3.OperationalError):
        await repository.complete_task(
            task.complete(NOW), expected_updated_at=task.updated_at
        )

    stored_task = await repository.get_task(task.id)
    stored_block = await repository.get_plan_block(future.id)
    assert stored_task is not None and stored_task.status is TaskStatus.OPEN
    assert stored_block is not None and stored_block.is_active


async def test_stale_terminal_transition_changes_nothing(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    future = _block(task.id, 3, 5)
    await repository.add_plan_block(future)

    with pytest.raises(StaleTaskUpdate):
        await repository.complete_task(
            task.complete(NOW), expected_updated_at=NOW - timedelta(minutes=1)
        )

    stored_task = await repository.get_task(task.id)
    stored_block = await repository.get_plan_block(future.id)
    assert stored_task is not None and stored_task.status is TaskStatus.OPEN
    assert stored_block is not None and stored_block.is_active


async def test_deadline_can_be_set_moved_and_cleared(
    repository: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    task = _task()
    await repository.add_task(task)
    first_due = NOW + timedelta(days=5)
    created = await repository.set_deadline(
        Deadline(task_id=task.id, due_at=first_due, created_at=NOW, updated_at=NOW),
        expected_updated_at=task.updated_at,
        at=NOW,
    )

    clock.advance(60)
    moved = await repository.set_deadline(
        Deadline(
            task_id=task.id,
            due_at=first_due + timedelta(days=1),
            created_at=clock.now(),
            updated_at=clock.now(),
        ),
        expected_updated_at=NOW,
        at=clock.now(),
    )

    assert moved.id == created.id  # rescheduling keeps the deadline identity
    assert moved.due_at == first_due + timedelta(days=1)
    assert moved.created_at == created.created_at
    stored_task = await repository.get_task(task.id)
    assert stored_task is not None and stored_task.updated_at == clock.now()

    await repository.clear_deadline(
        task_id=task.id, expected_updated_at=clock.now(), at=clock.now()
    )
    assert await repository.get_deadline(task.id) is None
    with pytest.raises(DeadlineNotFound):
        await repository.clear_deadline(
            task_id=task.id, expected_updated_at=clock.now(), at=clock.now()
        )


async def test_deadline_survives_completion(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    deadline = Deadline(
        task_id=task.id, due_at=NOW + timedelta(days=5), created_at=NOW, updated_at=NOW
    )
    await repository.add_task(task, deadline=deadline)

    await repository.complete_task(task.complete(NOW), expected_updated_at=task.updated_at)

    assert await repository.get_deadline(task.id) == deadline


async def test_terminal_tasks_cannot_change_their_deadline(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    completed = task.complete(NOW)
    await repository.complete_task(completed, expected_updated_at=task.updated_at)

    with pytest.raises(TaskNotOpen):
        await repository.set_deadline(
            Deadline(task_id=task.id, due_at=NOW, created_at=NOW, updated_at=NOW),
            expected_updated_at=completed.updated_at,
            at=NOW,
        )


async def test_calendar_events_use_half_open_ranges(
    repository: SqliteCommitmentRepository,
) -> None:
    ten = NOW.replace(hour=10)
    eleven = ten + timedelta(hours=1)
    twelve = ten + timedelta(hours=2)
    first = CalendarEvent(
        title="Lecture", starts_at=ten, ends_at=eleven, created_at=NOW, updated_at=NOW
    )
    second = CalendarEvent(
        title="Travel", starts_at=eleven, ends_at=twelve, created_at=NOW, updated_at=NOW
    )
    for event in (first, second):
        await repository.add_calendar_event(event)

    back_to_back = await repository.list_calendar_events(
        query_start=eleven, query_end=twelve
    )
    spanning = await repository.list_calendar_events(query_start=ten, query_end=twelve)

    assert [event.id for event in back_to_back] == [second.id]
    assert [event.id for event in spanning] == [first.id, second.id]
    assert await repository.get_calendar_event(first.id) == first


async def test_cancelling_a_calendar_event_hides_it_from_active_queries(
    repository: SqliteCommitmentRepository,
) -> None:
    event = CalendarEvent(
        title="Lecture",
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=2),
        created_at=NOW,
        updated_at=NOW,
    )
    await repository.add_calendar_event(event)

    cancelled = await repository.cancel_calendar_event(event.id, at=NOW)

    assert cancelled.cancelled_at == NOW
    active = await repository.list_calendar_events(
        query_start=NOW, query_end=NOW + timedelta(hours=3)
    )
    assert active == []
    including = await repository.list_calendar_events(
        query_start=NOW, query_end=NOW + timedelta(hours=3), include_cancelled=True
    )
    assert [item.id for item in including] == [event.id]
    with pytest.raises(CalendarEventNotFound):
        await repository.cancel_calendar_event(uuid4(), at=NOW)


async def test_plan_block_range_queries_and_cancellation(
    repository: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await repository.add_task(task)
    block = _block(task.id, 1, 3)
    await repository.add_plan_block(block)

    before = await repository.list_plan_blocks_in_range(
        query_start=NOW, query_end=NOW + timedelta(hours=1)
    )
    covering = await repository.list_plan_blocks_in_range(
        query_start=NOW, query_end=NOW + timedelta(hours=4)
    )

    assert before == []
    assert [item.id for item in covering] == [block.id]
    cancelled = await repository.cancel_plan_block(block.id, at=NOW)
    assert cancelled.cancelled_at == NOW
    assert (
        await repository.list_plan_blocks_in_range(
            query_start=NOW, query_end=NOW + timedelta(hours=4)
        )
        == []
    )
    with pytest.raises(PlanBlockNotFound):
        await repository.cancel_plan_block(uuid4(), at=NOW)


async def test_plan_block_for_unknown_task_is_rejected(
    repository: SqliteCommitmentRepository,
) -> None:
    with pytest.raises(TaskNotFound):
        await repository.add_plan_block(_block(uuid4(), 1, 2))


async def test_work_sessions_persist_and_total_exact_seconds(
    repository: SqliteCommitmentRepository, work: SqliteWorkRepository
) -> None:
    task = _task()
    await repository.add_task(task)
    other = _task(title="Other task")
    await repository.add_task(other)
    first = WorkSession(
        task_id=task.id,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=60),
        created_at=NOW,
    )
    second = WorkSession(
        task_id=task.id,
        started_at=NOW + timedelta(hours=2),
        ended_at=NOW + timedelta(hours=3, minutes=30),
        created_at=NOW,
    )
    for session in (first, second):
        await work.add_work_session(session)

    sessions = await work.list_work_sessions_for_task(task.id)
    totals = await work.total_work_seconds_for_tasks([task.id, other.id])

    assert [session.id for session in sessions] == [first.id, second.id]
    assert await work.total_work_seconds(task.id) == 9000
    assert totals == {task.id: 9000}
    assert await work.get_work_session(first.id) == first
    assert await work.get_work_session(uuid4()) is None


async def test_work_session_for_unknown_task_is_rejected(work: SqliteWorkRepository) -> None:
    with pytest.raises(TaskNotFound):
        await work.add_work_session(
            WorkSession(
                task_id=uuid4(),
                started_at=NOW,
                ended_at=NOW + timedelta(minutes=30),
                created_at=NOW,
            )
        )


async def test_database_constraints_are_enforced(
    database: Database, repository: SqliteCommitmentRepository
) -> None:
    task = _task()
    await repository.add_task(task)
    deadline = Deadline(task_id=task.id, due_at=NOW, created_at=NOW, updated_at=NOW)
    await repository.set_deadline(deadline, expected_updated_at=task.updated_at, at=NOW)

    def execute(statement: str, parameters: tuple[object, ...]) -> None:
        with database.connect() as connection:
            connection.execute(statement, parameters)

    with pytest.raises(sqlite3.IntegrityError, match="status_is_known"):
        execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, 'x', 'blocked', 'normal', ?, ?)",
            (str(uuid4()), "2026-09-20T09:00:00.000000+00:00", "2026-09-20T09:00:00.000000+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="priority_is_known"):
        execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, 'x', 'open', 'urgent', ?, ?)",
            (str(uuid4()), "2026-09-20T09:00:00.000000+00:00", "2026-09-20T09:00:00.000000+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="estimate_at_least_one_minute"):
        execute(
            "INSERT INTO tasks (id, title, status, priority, estimated_minutes, created_at, "
            "updated_at) VALUES (?, 'x', 'open', 'normal', 0, ?, ?)",
            (str(uuid4()), "2026-09-20T09:00:00.000000+00:00", "2026-09-20T09:00:00.000000+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="tasks_open_has_no_terminal_time"):
        execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            ("2026-09-20T09:00:00.000000+00:00", str(task.id)),
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        execute(
            "INSERT INTO deadlines (id, task_id, due_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(task.id),
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="calendar_events_positive_duration"):
        execute(
            "INSERT INTO calendar_events (id, title, starts_at, ends_at, created_at, updated_at) "
            "VALUES (?, 'x', ?, ?, ?, ?)",
            (
                str(uuid4()),
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
            "origin, proposal_id) VALUES (?, ?, ?, ?, ?, ?, 'manual', NULL)",
            (
                str(uuid4()),
                str(uuid4()),
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T10:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="work_sessions_positive_duration"):
        execute(
            "INSERT INTO work_sessions (id, task_id, started_at, ended_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(task.id),
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
            ),
        )
