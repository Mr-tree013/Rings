"""Reminder jobs are materialized by the mutation that made them due (ADR-0016).

Everything here is about one invariant: the reminder schedule is never a second step. If the
deadline is stored, its reminders are stored; if the deadline moves, the old reminders are
cancelled and the new ones exist; if the transaction fails, neither the deadline nor the
schedule changed. The commitment revision still moves exactly once per mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.application.calendar_service import (
    CalendarService,
    CreateCalendarEvent,
    CreatePlanBlock,
)
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.planner_service import PlannerService
from assistant.application.planning_fingerprint import planning_fingerprint
from assistant.application.rolling_replan import RollingReplanRequester
from assistant.application.task_service import CreateTask, EditTask, TaskService
from assistant.application.work_service import WorkService
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.errors import StaleScheduledJobClaim
from assistant.domain.planning import PlanningWindow
from assistant.domain.scheduled_job import ScheduledJobKind, ScheduledJobStatus
from assistant.domain.task import TaskPriority
from assistant.store import scheduled_jobs as scheduled_jobs_store
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
OFFSETS = (1440, 120)
DEBOUNCE = 60


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
def scheduler(database: Database) -> SqliteSchedulerRepository:
    return SqliteSchedulerRepository(database)


@pytest.fixture
def replan(
    scheduler: SqliteSchedulerRepository, clock: FakeClock
) -> RollingReplanRequester:
    return RollingReplanRequester(
        scheduler, clock, debounce_seconds=DEBOUNCE, timezone="Asia/Shanghai"
    )


@pytest.fixture
def tasks(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    replan: RollingReplanRequester,
) -> TaskService:
    return TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=OFFSETS,
        replan=replan,
    )


@pytest.fixture
def calendar(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    replan: RollingReplanRequester,
) -> CalendarService:
    return CalendarService(commitments, clock, replan=replan)


@pytest.fixture
def work_service(
    work: SqliteWorkRepository,
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    replan: RollingReplanRequester,
) -> WorkService:
    return WorkService(work, commitments, clock, replan=replan)


def _revision(database: Database) -> int:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value FROM commitment_meta WHERE key = 'revision'"
        ).fetchone()
    return int(str(row["value"]))


async def _jobs(
    scheduler: SqliteSchedulerRepository,
    *,
    kind: ScheduledJobKind | None = None,
) -> list:
    return [
        job
        for job in await scheduler.list_jobs(statuses=None, limit=None)
        if kind is None or job.kind is kind
    ]


async def _reminders(scheduler: SqliteSchedulerRepository) -> list:
    return await _jobs(scheduler, kind=ScheduledJobKind.DEADLINE_REMINDER)


async def _replans(scheduler: SqliteSchedulerRepository) -> list:
    return await _jobs(scheduler, kind=ScheduledJobKind.ROLLING_REPLAN)


# --------------------------------------------------------- deadline materialization


async def test_a_new_deadline_materializes_one_reminder_per_offset(
    tasks: TaskService, scheduler: SqliteSchedulerRepository, database: Database
) -> None:
    due_at = NOW + timedelta(days=3)

    task = await tasks.create_task(CreateTask(title="Write SE lab report", due_at=due_at))
    reminders = await _reminders(scheduler)

    assert len(reminders) == 2
    assert {job.status for job in reminders} == {ScheduledJobStatus.PENDING}
    assert sorted(job.due_at for job in reminders) == sorted(
        [due_at - timedelta(minutes=1440), due_at - timedelta(minutes=120)]
    )
    assert len({job.dedup_key for job in reminders}) == 2
    assert all(str(task.id) in job.payload_json for job in reminders)
    assert _revision(database) == 1  # one mutation, one increment, however many jobs


async def test_a_task_without_a_deadline_has_no_reminders(
    tasks: TaskService, scheduler: SqliteSchedulerRepository
) -> None:
    await tasks.create_task(CreateTask(title="Someday"))

    assert await _reminders(scheduler) == []


async def test_setting_a_deadline_materializes_reminders(
    tasks: TaskService, scheduler: SqliteSchedulerRepository
) -> None:
    task = await tasks.create_task(CreateTask(title="Write SE lab report"))
    assert await _reminders(scheduler) == []

    await tasks.set_deadline(task.id, NOW + timedelta(days=2))

    assert len(await _reminders(scheduler)) == 2


async def test_past_offsets_are_due_immediately_not_dropped(
    tasks: TaskService, scheduler: SqliteSchedulerRepository
) -> None:
    # A deadline in 20 minutes is already inside both reminder offsets.
    await tasks.create_task(
        CreateTask(title="Urgent", due_at=NOW + timedelta(minutes=20))
    )

    reminders = await _reminders(scheduler)

    assert len(reminders) == 2
    assert {job.due_at for job in reminders} == {NOW}


async def test_rescheduling_a_deadline_replaces_only_its_active_reminders(
    tasks: TaskService, scheduler: SqliteSchedulerRepository, database: Database
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(days=3))
    )
    original = await _reminders(scheduler)
    revision_before = _revision(database)

    moved_due_at = NOW + timedelta(days=4)
    await tasks.set_deadline(task.id, moved_due_at)
    reminders = await _reminders(scheduler)

    active = [job for job in reminders if job.is_active]
    cancelled = [job for job in reminders if job.status is ScheduledJobStatus.CANCELLED]
    assert len(active) == 2
    assert len(cancelled) == 2
    assert {job.id for job in cancelled} == {job.id for job in original}
    assert sorted(job.due_at for job in active) == sorted(
        [moved_due_at - timedelta(minutes=1440), moved_due_at - timedelta(minutes=120)]
    )
    assert _revision(database) == revision_before + 1


async def test_rescheduling_cancels_a_reminder_that_is_already_in_flight(
    tasks: TaskService, scheduler: SqliteSchedulerRepository
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(minutes=1))
    )
    in_flight = (await _reminders(scheduler))[0]
    claimed = await scheduler.claim_next(
        worker_id="w1",
        claim_token=uuid4(),
        now=NOW,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert claimed is not None

    await tasks.set_deadline(task.id, NOW + timedelta(days=5))

    stored = await scheduler.get_job(in_flight.id)
    assert stored is not None
    assert stored.status is ScheduledJobStatus.CANCELLED
    assert stored.claim_token is None  # the old worker can no longer finish it
    with pytest.raises(StaleScheduledJobClaim):
        await scheduler.complete_claim(
            in_flight.id, claim_token=claimed.claim_token, completed_at=NOW
        )


async def test_clearing_a_deadline_cancels_its_reminders(
    tasks: TaskService, scheduler: SqliteSchedulerRepository, database: Database
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(days=3))
    )
    revision_before = _revision(database)

    await tasks.clear_deadline(task.id)

    reminders = await _reminders(scheduler)
    assert len(reminders) == 2
    assert all(job.status is ScheduledJobStatus.CANCELLED for job in reminders)
    assert _revision(database) == revision_before + 1


async def test_terminal_tasks_cancel_their_reminders(
    tasks: TaskService, scheduler: SqliteSchedulerRepository
) -> None:
    completed = await tasks.create_task(
        CreateTask(title="Done soon", due_at=NOW + timedelta(days=1))
    )
    cancelled = await tasks.create_task(
        CreateTask(title="Never mind", due_at=NOW + timedelta(days=1))
    )

    await tasks.complete_task(completed.id)
    await tasks.cancel_task(cancelled.id)

    reminders = await _reminders(scheduler)
    assert len(reminders) == 4
    assert all(job.status is ScheduledJobStatus.CANCELLED for job in reminders)


async def test_a_failed_reminder_insert_rolls_back_the_deadline_mutation(
    tasks: TaskService,
    commitments: SqliteCommitmentRepository,
    scheduler: SqliteSchedulerRepository,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(days=3))
    )
    original_deadline = await commitments.get_deadline(task.id)
    original_jobs = await _reminders(scheduler)
    revision_before = _revision(database)

    def exploding_insert(connection: object, job: object) -> None:
        raise RuntimeError("injected failure while inserting a reminder job")

    monkeypatch.setattr(scheduled_jobs_store, "insert_job", exploding_insert)
    try:
        with pytest.raises(RuntimeError):
            await tasks.set_deadline(task.id, NOW + timedelta(days=9))
    finally:
        monkeypatch.setattr(
            scheduled_jobs_store, "insert_job", scheduled_jobs_store.insert_job
        )

    assert await commitments.get_deadline(task.id) == original_deadline
    assert await _reminders(scheduler) == original_jobs
    assert _revision(database) == revision_before


async def test_reminder_schedule_matches_the_configured_offsets(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    scheduler: SqliteSchedulerRepository,
) -> None:
    legacy = TaskService(commitments, clock)  # no reminder offsets configured
    task = await legacy.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(days=3))
    )

    assert await _reminders(scheduler) == []
    assert await legacy.get_deadline(task.id) is not None


# ------------------------------------------------------------------- rolling replan


async def test_repeated_mutations_coalesce_into_one_debounced_replan_job(
    tasks: TaskService, scheduler: SqliteSchedulerRepository, clock: FakeClock
) -> None:
    task = await tasks.create_task(CreateTask(title="Write SE lab report"))
    first = await _replans(scheduler)
    assert len(first) == 1
    assert first[0].due_at == NOW + timedelta(seconds=DEBOUNCE)

    clock.advance(10)
    await tasks.update_task(
        task.id,
        EditTask(
            title="Write SE lab report",
            description=None,
            priority=TaskPriority.HIGH,
            estimated_minutes=120,
        ),
    )
    clock.advance(10)
    await tasks.set_deadline(task.id, NOW + timedelta(days=2))

    replans = await _replans(scheduler)
    assert len(replans) == 1  # one active request, not three
    assert replans[0].id == first[0].id
    assert replans[0].due_at == NOW + timedelta(seconds=20 + DEBOUNCE)


async def test_every_commitment_mutation_requests_a_replan(
    tasks: TaskService,
    calendar: CalendarService,
    work_service: WorkService,
    scheduler: SqliteSchedulerRepository,
    clock: FakeClock,
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=NOW + timedelta(days=2))
    )
    event = await calendar.create_event(
        CreateCalendarEvent(
            title="Lecture",
            starts_at=NOW + timedelta(hours=1),
            ends_at=NOW + timedelta(hours=2),
        )
    )
    block = await calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id,
            starts_at=NOW + timedelta(hours=3),
            ends_at=NOW + timedelta(hours=4),
        )
    )
    await work_service.record_session(
        task_id=task.id,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=30),
    )

    replans = await _replans(scheduler)
    assert len(replans) == 1
    for mutation in (
        calendar.cancel_event(event.id),
        calendar.cancel_plan_block(block.id),
        tasks.clear_deadline(task.id),
    ):
        clock.advance(10)
        await mutation

    assert len(await _replans(scheduler)) == 1


async def test_no_replan_is_scheduled_without_a_planning_timezone(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    scheduler: SqliteSchedulerRepository,
) -> None:
    requester = RollingReplanRequester(
        scheduler, clock, debounce_seconds=DEBOUNCE, timezone=None
    )
    service = TaskService(
        commitments, clock, reminder_offsets_minutes=OFFSETS, replan=requester
    )

    await service.create_task(CreateTask(title="Write SE lab report"))

    assert await _replans(scheduler) == []
    assert len(await _reminders(scheduler)) == 0


async def test_plan_proposals_do_not_trigger_replanning(
    tasks: TaskService,
    scheduler: SqliteSchedulerRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    """Applying a proposal must never ask for another proposal (no feedback loop)."""
    config = PlanningConfig(
        timezone="Asia/Shanghai",
        availability=(
            WeeklyAvailabilityRule(
                days=(Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI),
                start_minute=9 * 60,
                end_minute=22 * 60,
            ),
        ),
    )
    planner = PlannerService(SqlitePlanningRepository(database), GreedyPlanner(), config, clock)
    await tasks.create_task(CreateTask(title="Write SE lab report", estimated_minutes=120))
    before = len(await _replans(scheduler))

    detail = await planner.create_week_proposal()
    await planner.apply_proposal(str(detail.proposal.id))

    assert len(await _replans(scheduler)) == before


async def test_reminders_do_not_enter_the_planning_fingerprint(
    tasks: TaskService,
    database: Database,
    clock: FakeClock,
    scheduler: SqliteSchedulerRepository,
) -> None:
    """Scheduler state is not planning input: the same commitment state keeps its digest."""
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=120)
    )
    planning = SqlitePlanningRepository(database)
    config = PlanningConfig(
        timezone="Asia/Shanghai",
        availability=(
            WeeklyAvailabilityRule(
                days=(Weekday.MON,),
                start_minute=9 * 60,
                end_minute=17 * 60,
            ),
        ),
    )
    window = PlanningWindow(
        starts_at=NOW, ends_at=NOW + timedelta(days=7), timezone="Asia/Shanghai"
    )
    before = planning_fingerprint(
        window=window, config=config, snapshot=await planning.load_snapshot(window)
    )

    await tasks.set_deadline(task.id, NOW + timedelta(days=3))
    after = planning_fingerprint(
        window=window, config=config, snapshot=await planning.load_snapshot(window)
    )
    without_reminders = planning_fingerprint(
        window=window, config=config, snapshot=await planning.load_snapshot(window)
    )

    assert await _reminders(scheduler) != []
    assert after == without_reminders  # reminder rows do not change the digest
    assert before != after  # but the deadline itself does
