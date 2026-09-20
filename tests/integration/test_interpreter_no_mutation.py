"""Interpreting a request changes nothing (ADR-0018).

Real SQLite, real repositories, real interpreter, scripted model: after several READY
interpretations the authoritative and derived state is byte-for-byte what it was before. The
only thing that changes is the fake adapter's record of what it was asked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.interpreter import InterpreterService
from assistant.application.interpreter_context import InterpreterContextBuilder
from assistant.application.structured_model import StructuredModel
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.config import ModelConfig
from assistant.domain.deadline import Deadline
from assistant.domain.notification import Notification, NotificationKind
from assistant.domain.planning import PlanningWindow, PlanProposal
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobKind,
    canonical_payload_json,
)
from assistant.domain.scheduler_payloads import RollingReplanPayload
from assistant.domain.task import Task
from assistant.domain.work_session import WorkSession
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")

_CAPTURED_TABLES = (
    "tasks",
    "deadlines",
    "calendar_events",
    "plan_blocks",
    "work_sessions",
    "plan_proposals",
    "proposed_plan_blocks",
    "planning_issues",
    "scheduled_jobs",
    "notifications",
    "commitment_meta",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


def _snapshot(database: Database) -> dict[str, list[tuple[object, ...]]]:
    """Every durable row, in a stable order, for every table."""
    captured: dict[str, list[tuple[object, ...]]] = {}
    with database.connect() as connection:
        for table in _CAPTURED_TABLES:
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            captured[table] = [tuple(row) for row in rows]
    return captured


def _revision(database: Database) -> int:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value FROM commitment_meta WHERE key = 'revision'"
        ).fetchone()
    return int(str(row["value"]))


async def _seed(
    database: Database,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    jobs: SqliteSchedulerRepository,
    planning: SqlitePlanningRepository,
) -> None:
    """One of everything, including the durable state an interpreter must not touch."""
    task = Task(
        id=TASK_ID,
        title="SE Lab",
        description="extended notes",
        estimated_minutes=300,
        created_at=NOW,
        updated_at=NOW,
    )
    await commitments.add_task(
        task,
        deadline=Deadline(
            task_id=TASK_ID,
            due_at=NOW + timedelta(days=2),
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    await commitments.add_calendar_event(
        CalendarEvent(
            title="Lecture",
            starts_at=NOW + timedelta(hours=3),
            ends_at=NOW + timedelta(hours=4),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await work.add_work_session(
        WorkSession(
            task_id=TASK_ID,
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=30),
            created_at=NOW,
        )
    )
    revision = _revision(database)
    await planning.create_proposal(
        PlanProposal(
            window=PlanningWindow(
                starts_at=NOW,
                ends_at=NOW + timedelta(days=7),
                timezone="Asia/Shanghai",
            ),
            input_fingerprint="f" * 64,
            input_revision=revision,
            created_at=NOW,
        ),
        blocks=(),
        issues=(),
    )
    await jobs.create_notification_idempotent(
        Notification(
            kind=NotificationKind.PLAN_READY,
            title="Updated plan proposal is ready",
            body="Review it.",
            dedup_key="notification:plan-ready:seed",
            created_at=NOW,
        )
    )
    await jobs.schedule_or_replace(
        ScheduledJob(
            kind=ScheduledJobKind.ROLLING_REPLAN,
            due_at=NOW + timedelta(minutes=5),
            dedup_key="rolling-replan:current-week",
            payload_json=canonical_payload_json(
                RollingReplanPayload(timezone="Asia/Shanghai").to_payload()
            ),
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _service(
    clock: FakeClock,
    commitments: SqliteCommitmentRepository,
    *answers: str,
) -> tuple[InterpreterService, FakeModelAdapter]:
    model = FakeModelAdapter()
    for answer in answers:
        model.queue_text(answer)
    return (
        InterpreterService(
            StructuredModel(model),
            InterpreterContextBuilder(commitments, clock, planning_timezone="Asia/Shanghai"),
            ModelConfig(),
        ),
        model,
    )


def _ready(command: dict[str, object]) -> str:
    return json.dumps(
        {"status": "ready", "command": command, "question": None, "reason": None}
    )


async def test_interpreting_ready_commands_changes_nothing(
    database: Database,
    clock: FakeClock,
) -> None:
    commitments = SqliteCommitmentRepository(database)
    work = SqliteWorkRepository(database)
    jobs = SqliteSchedulerRepository(database)
    planning = SqlitePlanningRepository(database)
    await _seed(database, commitments, work, jobs, planning)
    before = _snapshot(database)
    service, model = _service(
        clock,
        commitments,
        _ready({"kind": "complete_task", "task_id": str(TASK_ID)}),
        _ready({"kind": "cancel_task", "task_id": str(TASK_ID)}),
        _ready({"kind": "clear_deadline", "task_id": str(TASK_ID)}),
        _ready(
            {
                "kind": "set_deadline",
                "task_id": str(TASK_ID),
                "due_at": "2026-12-31T23:59:00+08:00",
            }
        ),
        _ready(
            {
                "kind": "create_task",
                "title": "Brand new task",
                "description": None,
                "priority": "high",
                "estimated_minutes": 60,
                "deadline": "2026-12-31T23:59:00+08:00",
            }
        ),
        _ready(
            {
                "kind": "create_calendar_event",
                "title": "New lecture",
                "description": None,
                "starts_at": "2026-12-01T10:00:00+08:00",
                "ends_at": "2026-12-01T12:00:00+08:00",
            }
        ),
        _ready({"kind": "request_week_plan", "next_week": True}),
    )

    results = [
        await service.interpret(text)
        for text in (
            "finish SE Lab",
            "drop SE Lab",
            "remove the deadline from SE Lab",
            "SE Lab is due at the end of the year",
            "add a task to write the OS lab report",
            "class on the 1st of December 10 to 12",
            "plan next week",
        )
    ]

    assert len(model.requests) == 7  # the model was asked...
    assert all(result.command is not None for result in results)
    assert _snapshot(database) == before  # ...and nothing in the database moved


async def test_interpreting_a_clarification_or_refusal_changes_nothing(
    database: Database, clock: FakeClock
) -> None:
    commitments = SqliteCommitmentRepository(database)
    work = SqliteWorkRepository(database)
    jobs = SqliteSchedulerRepository(database)
    planning = SqlitePlanningRepository(database)
    await _seed(database, commitments, work, jobs, planning)
    before = _snapshot(database)
    service, _ = _service(
        clock,
        commitments,
        json.dumps(
            {
                "status": "needs_clarification",
                "command": None,
                "question": "Which task?",
                "reason": None,
            }
        ),
        json.dumps(
            {
                "status": "unsupported",
                "command": None,
                "question": None,
                "reason": "Not supported yet.",
            }
        ),
    )

    await service.interpret("finish something")
    await service.interpret("send an email")

    assert _snapshot(database) == before


async def test_no_new_tables_or_rows_appear(
    database: Database, clock: FakeClock
) -> None:
    """The interpreter writes nothing, not even a transcript of what it was asked."""
    commitments = SqliteCommitmentRepository(database)
    await _seed(
        database,
        commitments,
        SqliteWorkRepository(database),
        SqliteSchedulerRepository(database),
        SqlitePlanningRepository(database),
    )
    with database.connect() as connection:
        tables_before = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        jobs_before = connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0]
    service, _ = _service(
        clock,
        commitments,
        _ready({"kind": "request_week_plan", "next_week": False}),
    )

    await service.interpret("plan my week")

    with database.connect() as connection:
        tables_after = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        jobs_after = connection.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0]
        proposals = connection.execute("SELECT count(*) FROM plan_proposals").fetchone()[0]

    assert tables_after == tables_before  # no migration 0007, no transcript table
    assert jobs_after == jobs_before  # no extra scheduler work was requested
    assert proposals == 1  # the seeded proposal, untouched


def test_the_runtime_database_still_has_no_interpreter_tables(database: Database) -> None:
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    for forbidden in ("command_proposals", "interpretations", "conversation_messages"):
        assert forbidden not in names
