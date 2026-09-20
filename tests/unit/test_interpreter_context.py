"""The bounded interpreter context: ordering, truncation and data minimisation (ADR-0018)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.application.interpreter_context import (
    MAX_INTERPRETER_TASKS,
    InterpreterContextBuilder,
)
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.notification import Notification, NotificationKind
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobKind,
    canonical_payload_json,
)
from assistant.domain.task import Task, TaskPriority
from assistant.domain.work_session import WorkSession
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


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
def jobs(database: Database) -> SqliteSchedulerRepository:
    return SqliteSchedulerRepository(database)


def _builder(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    *,
    timezone: str | None = "Asia/Shanghai",
    max_tasks: int = MAX_INTERPRETER_TASKS,
) -> InterpreterContextBuilder:
    return InterpreterContextBuilder(
        commitments, clock, planning_timezone=timezone, max_tasks=max_tasks
    )


async def _add_task(
    commitments: SqliteCommitmentRepository,
    *,
    title: str,
    priority: TaskPriority = TaskPriority.NORMAL,
    created_at: datetime = NOW,
    description: str | None = None,
    estimated_minutes: int | None = None,
    due_at: datetime | None = None,
) -> Task:
    task = Task(
        title=title,
        description=description,
        priority=priority,
        estimated_minutes=estimated_minutes,
        created_at=created_at,
        updated_at=created_at,
    )
    deadline = (
        None
        if due_at is None
        else Deadline(task_id=task.id, due_at=due_at, created_at=created_at, updated_at=created_at)
    )
    await commitments.add_task(task, deadline=deadline)
    return task


async def test_the_context_holds_only_open_tasks_and_task_metadata(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    open_task = await _add_task(
        commitments,
        title="Write SE lab report",
        priority=TaskPriority.HIGH,
        estimated_minutes=300,
        due_at=NOW + timedelta(days=2),
    )
    done = await _add_task(commitments, title="Already done")
    await commitments.complete_task(
        done.complete(at=NOW + timedelta(minutes=1)), expected_updated_at=done.updated_at
    )

    context = await _builder(commitments, clock).build()

    assert [task.id for task in context.open_tasks] == [open_task.id]
    entry = context.open_tasks[0]
    assert entry.title == "Write SE lab report"
    assert entry.priority is TaskPriority.HIGH
    assert entry.estimated_minutes == 300
    assert entry.deadline == NOW + timedelta(days=2)
    assert entry.updated_at == NOW
    assert context.planning_timezone == "Asia/Shanghai"
    assert context.tasks_truncated is False


async def test_the_context_payload_is_exactly_the_agreed_shape(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    await _add_task(commitments, title="Write SE lab report")

    payload = (await _builder(commitments, clock).build()).to_payload()

    assert set(payload) == {
        "current_time",
        "planning_timezone",
        "tasks_truncated",
        "open_tasks",
    }
    assert set(payload["open_tasks"][0]) == {  # type: ignore[index]
        "id",
        "title",
        "priority",
        "estimated_minutes",
        "deadline",
        "updated_at",
    }


async def test_the_context_never_carries_forbidden_personal_state(
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    jobs: SqliteSchedulerRepository,
    clock: FakeClock,
) -> None:
    """Descriptions, work sessions, events, notifications and job payloads stay local."""
    task = await _add_task(
        commitments,
        title="SE Lab",
        description="TOP-SECRET-DESCRIPTION-SENTINEL",
        due_at=NOW + timedelta(days=1),
    )
    await work.add_work_session(
        WorkSession(
            task_id=task.id,
            started_at=NOW,
            ended_at=NOW + timedelta(hours=1),
            created_at=NOW,
        )
    )
    await commitments.add_calendar_event(
        CalendarEvent(
            title="CALENDAR-SECRET",
            starts_at=NOW + timedelta(hours=3),
            ends_at=NOW + timedelta(hours=4),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await jobs.create_notification_idempotent(
        Notification(
            kind=NotificationKind.SCHEDULER_WARNING,
            title="NOTIFICATION-SECRET",
            body="NOTIFICATION-SECRET",
            dedup_key="notification:test",
            created_at=NOW,
        )
    )
    await jobs.schedule_or_replace(
        ScheduledJob(
            kind=ScheduledJobKind.ROLLING_REPLAN,
            due_at=NOW,
            dedup_key="scheduler-secret",
            payload_json=canonical_payload_json({"secret": "SCHEDULER-SECRET"}),
            created_at=NOW,
            updated_at=NOW,
        )
    )

    rendered = (await _builder(commitments, clock).build()).to_json()

    assert "SE Lab" in rendered
    for forbidden in (
        "TOP-SECRET-DESCRIPTION-SENTINEL",
        "CALENDAR-SECRET",
        "NOTIFICATION-SECRET",
        "SCHEDULER-SECRET",
    ):
        assert forbidden not in rendered


async def test_the_context_is_canonical_json(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    await _add_task(commitments, title="Ünïcode — title")

    rendered = (await _builder(commitments, clock).build()).to_json()
    decoded = json.loads(rendered)

    assert "Ünïcode" in rendered  # not escaped
    assert decoded["open_tasks"][0]["title"] == "Ünïcode — title"
    assert rendered == json.dumps(
        decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


async def test_duplicate_titles_stay_distinguishable_by_id(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    first = await _add_task(commitments, title="SE Lab", created_at=NOW)
    second = await _add_task(commitments, title="SE Lab", created_at=NOW + timedelta(minutes=1))

    context = await _builder(commitments, clock).build()

    assert [entry.title for entry in context.open_tasks] == ["SE Lab", "SE Lab"]
    assert {entry.id for entry in context.open_tasks} == {first.id, second.id}
    assert len(context.task_ids) == 2


async def test_ordering_is_deadline_then_priority_then_creation_then_id(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    later = await _add_task(
        commitments,
        title="later deadline",
        due_at=NOW + timedelta(days=5),
        priority=TaskPriority.HIGH,
    )
    sooner_low = await _add_task(
        commitments,
        title="sooner deadline, low priority",
        due_at=NOW + timedelta(days=1),
        priority=TaskPriority.LOW,
    )
    urgent_no_deadline = await _add_task(
        commitments, title="no deadline, high", priority=TaskPriority.HIGH
    )
    normal_early = await _add_task(
        commitments, title="no deadline, normal", created_at=NOW - timedelta(days=1)
    )

    context = await _builder(commitments, clock).build()

    assert [entry.id for entry in context.open_tasks] == [
        sooner_low.id,  # earliest deadline first, regardless of priority
        later.id,
        urgent_no_deadline.id,  # then no-deadline tasks by priority
        normal_early.id,
    ]


async def test_the_context_is_capped_and_says_so(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    for index in range(MAX_INTERPRETER_TASKS + 5):
        await _add_task(
            commitments,
            title=f"task {index:02d}",
            priority=TaskPriority.HIGH if index < 3 else TaskPriority.NORMAL,
            created_at=NOW + timedelta(minutes=index),
        )

    builder = _builder(commitments, clock)
    context = await builder.build()
    again = await builder.build()

    assert len(context.open_tasks) == MAX_INTERPRETER_TASKS
    assert context.tasks_truncated is True
    assert context.open_tasks[0].title == "task 00"  # high priority first, then creation
    assert [entry.id for entry in context.open_tasks] == [
        entry.id for entry in again.open_tasks
    ]
    assert context.to_json() == again.to_json()  # deterministic, byte for byte


async def test_a_small_cap_marks_truncation(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    await _add_task(commitments, title="first")
    await _add_task(commitments, title="second", created_at=NOW + timedelta(minutes=1))

    context = await _builder(commitments, clock, max_tasks=1).build()

    assert len(context.open_tasks) == 1
    assert context.tasks_truncated is True


async def test_current_time_comes_from_the_clock_not_the_wall_clock(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    builder = _builder(commitments, clock)
    clock.advance(3600)

    context = await builder.build()

    assert context.current_time == NOW + timedelta(hours=1)
    assert context.to_payload()["current_time"] == (NOW + timedelta(hours=1)).isoformat()


async def test_a_missing_planning_timezone_is_reported_as_none(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    context = await _builder(commitments, clock, timezone=None).build()

    assert context.planning_timezone is None
    assert context.to_payload()["planning_timezone"] is None


def test_the_task_cap_is_a_documented_constant() -> None:
    assert MAX_INTERPRETER_TASKS == 50


async def test_the_builder_refuses_a_nonsense_cap(
    commitments: SqliteCommitmentRepository, clock: FakeClock
) -> None:
    with pytest.raises(ValueError):
        InterpreterContextBuilder(commitments, clock, planning_timezone=None, max_tasks=0)
