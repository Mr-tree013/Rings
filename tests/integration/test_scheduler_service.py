"""Real-SQLite tests for the daemon scheduler: reminders, replans, retries, cancellation.

The scheduler is where "at-least-once" becomes real, so these tests exercise the ugly paths:
a crash between notification and completion, an obsolete reminder, a terminal task, retries
with backoff, a dead letter, and cancellation that must not be mistaken for failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.planner_service import PlannerService
from assistant.application.retry import RetryPolicy
from assistant.application.rolling_replan import RollingReplanRequester
from assistant.application.scheduler_service import (
    PLANNING_NOT_CONFIGURED_MESSAGE,
    SchedulerRunResult,
    SchedulerService,
)
from assistant.application.task_service import CreateTask, TaskService
from assistant.application.work_service import WorkService
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.notification import Notification, NotificationKind, NotificationStatus
from assistant.domain.plan_block import PlanBlock
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobKind,
    ScheduledJobStatus,
    canonical_payload_json,
)
from assistant.domain.scheduler_payloads import (
    ROLLING_REPLAN_DEDUP_KEY,
    DeadlineReminderPayload,
    RollingReplanPayload,
    deadline_reminder_notification_key,
    plan_ready_notification_key,
)
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

# Monday 08:00 Asia/Shanghai == Monday 00:00 UTC; the local week ends Sunday 16:00 UTC.
MONDAY = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
WEEK_END = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
REMINDER_OFFSET = 120


def _planning_config(*, availability: bool = True) -> PlanningConfig:
    rules = ()
    if availability:
        rules = (
            WeeklyAvailabilityRule(
                days=(Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI),
                start_minute=9 * 60,
                end_minute=22 * 60,
            ),
        )
    return PlanningConfig(timezone="Asia/Shanghai", availability=rules)


@dataclass
class RecordingWaiter:
    """Records requested intervals and can stop the loop after a number of waits."""

    stop_after_waits: int | None = None
    calls: list[float] = field(default_factory=list)

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        self.calls.append(seconds)
        if self.stop_after_waits is not None and len(self.calls) >= self.stop_after_waits:
            stop_event.set()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=MONDAY)


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


@pytest.fixture
def tasks(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
) -> TaskService:
    """Tasks with reminders but no replan requests, so reminder tests stay focused."""
    return TaskService(
        commitments, clock, reminder_offsets_minutes=(REMINDER_OFFSET,)
    )


@pytest.fixture
def replan_tasks(
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
    jobs: SqliteSchedulerRepository,
) -> TaskService:
    """Tasks whose mutations request a debounced rolling replan."""
    return TaskService(
        commitments,
        clock,
        replan=RollingReplanRequester(
            jobs, clock, debounce_seconds=60, timezone="Asia/Shanghai"
        ),
    )


def _planner(
    database: Database, clock: FakeClock, config: PlanningConfig | None
) -> PlannerService:
    return PlannerService(SqlitePlanningRepository(database), GreedyPlanner(), config, clock)


def _service(
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    clock: FakeClock,
    planner: PlannerService,
    *,
    waiter: RecordingWaiter | None = None,
    policy: RetryPolicy | None = None,
    poll_interval: timedelta = timedelta(seconds=15),
) -> SchedulerService:
    return SchedulerService(
        jobs,
        commitments,
        work,
        planner,
        clock,
        policy if policy is not None else RetryPolicy(),
        waiter if waiter is not None else RecordingWaiter(),  # type: ignore[arg-type]
        worker_id="scheduler-test",
        poll_interval=poll_interval,
        lease_duration=timedelta(minutes=5),
    )


async def _all_jobs(
    jobs: SqliteSchedulerRepository, kind: ScheduledJobKind | None = None
) -> list[ScheduledJob]:
    return [
        job
        for job in await jobs.list_jobs(statuses=None, limit=None)
        if kind is None or job.kind is kind
    ]


async def _reminders(jobs: SqliteSchedulerRepository) -> list[ScheduledJob]:
    return await _all_jobs(jobs, ScheduledJobKind.DEADLINE_REMINDER)


async def _replans(jobs: SqliteSchedulerRepository) -> list[ScheduledJob]:
    return await _all_jobs(jobs, ScheduledJobKind.ROLLING_REPLAN)


def _reminder_job(
    *,
    task_id: UUID,
    deadline_id: UUID,
    due_at: datetime,
    payload_due_at: datetime,
    dedup_suffix: str = "manual",
    now: datetime = MONDAY,
) -> ScheduledJob:
    payload = DeadlineReminderPayload(
        task_id=task_id,
        deadline_id=deadline_id,
        deadline_due_at=payload_due_at,
        reminder_offset_minutes=REMINDER_OFFSET,
    )
    return ScheduledJob(
        kind=ScheduledJobKind.DEADLINE_REMINDER,
        due_at=due_at,
        dedup_key=f"deadline-reminder:{deadline_id}:{dedup_suffix}",
        payload_json=canonical_payload_json(payload.to_payload()),
        created_at=now,
        updated_at=now,
    )


def _replan_job(*, due_at: datetime, now: datetime = MONDAY) -> ScheduledJob:
    payload = RollingReplanPayload(timezone="Asia/Shanghai")
    return ScheduledJob(
        kind=ScheduledJobKind.ROLLING_REPLAN,
        due_at=due_at,
        dedup_key=ROLLING_REPLAN_DEDUP_KEY,
        payload_json=canonical_payload_json(payload.to_payload()),
        created_at=now,
        updated_at=now,
    )


def _notification_for(job_id: UUID, *, created_at: datetime) -> Notification:
    """A notification exactly as the reminder handler would have written it before a crash."""
    return Notification(
        kind=NotificationKind.DEADLINE_REMINDER,
        title="Deadline approaching: Write SE lab report",
        body="Due: earlier\nEstimated remaining work: 300 minutes",
        dedup_key=deadline_reminder_notification_key(job_id),
        created_at=created_at,
    )


# ------------------------------------------------------------------- reminders


async def test_a_due_reminder_becomes_a_notification_and_completes(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    # A deadline inside the reminder offset: the reminder is due immediately.
    due_at = MONDAY + timedelta(minutes=20)
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300, due_at=due_at)
    )
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    notifications = await jobs.list_notifications(limit=None)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.kind is NotificationKind.DEADLINE_REMINDER
    assert notification.status is NotificationStatus.UNREAD
    assert notification.title == "Deadline approaching: Write SE lab report"
    assert notification.related_task_id == task.id
    assert "Due: 2026-09-21T08:20:00+08:00" in notification.body
    assert "Estimated remaining work: 300 minutes" in notification.body

    reminder = (await _reminders(jobs))[0]
    assert reminder.status is ScheduledJobStatus.COMPLETED
    assert reminder.completed_at == MONDAY
    assert reminder.claim_token is None
    assert reminder.dedup_key.startswith("deadline-reminder:")


async def test_an_idle_scheduler_reports_idle(
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    scheduler = _service(jobs, commitments, work, clock, _planner(database, clock, None))

    assert await scheduler.run_once() is SchedulerRunResult.IDLE


async def test_a_reminder_reports_remaining_work_from_work_sessions_only(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    task = await tasks.create_task(
        CreateTask(
            title="Write SE lab report",
            estimated_minutes=300,
            due_at=MONDAY + timedelta(minutes=20),
        )
    )
    await WorkService(work, commitments, clock).record_session(
        task_id=task.id,
        started_at=MONDAY,
        ended_at=MONDAY + timedelta(minutes=90),
    )
    # A plan block is intention, not effort, and must not move the number.
    await commitments.add_plan_block(
        PlanBlock(
            task_id=task.id,
            starts_at=MONDAY + timedelta(hours=1),
            ends_at=MONDAY + timedelta(hours=4),
            created_at=MONDAY,
            updated_at=MONDAY,
        )
    )
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    await scheduler.run_once()

    body = (await jobs.list_notifications(limit=None))[0].body
    assert "Estimated remaining work: 210 minutes" in body


async def test_a_missing_estimate_says_so_instead_of_guessing(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    await tasks.create_task(
        CreateTask(title="Unestimated", due_at=MONDAY + timedelta(minutes=20))
    )
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    await scheduler.run_once()

    body = (await jobs.list_notifications(limit=None))[0].body
    assert "Remaining work estimate is not set." in body


async def test_an_exhausted_estimate_is_reported_without_completing_the_task(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    task = await tasks.create_task(
        CreateTask(
            title="Almost done",
            estimated_minutes=60,
            due_at=MONDAY + timedelta(minutes=20),
        )
    )
    await WorkService(work, commitments, clock).record_session(
        task_id=task.id,
        started_at=MONDAY,
        ended_at=MONDAY + timedelta(minutes=75),
    )
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    await scheduler.run_once()

    body = (await jobs.list_notifications(limit=None))[0].body
    assert "Recorded work has reached the current estimate; task is still open." in body
    stored = await commitments.get_task(task.id)
    assert stored is not None and stored.status.value == "open"


async def test_a_reminder_for_a_moved_deadline_is_completed_as_obsolete(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    original_due_at = MONDAY + timedelta(days=3)
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=original_due_at)
    )
    old_deadline = await commitments.get_deadline(task.id)
    assert old_deadline is not None and old_deadline.due_at == original_due_at
    await tasks.set_deadline(task.id, MONDAY + timedelta(days=5))
    # A reminder that survived a reschedule: its payload names the previous due instant.
    stale = _reminder_job(
        task_id=task.id,
        deadline_id=old_deadline.id,
        due_at=MONDAY,
        payload_due_at=original_due_at,
    )
    await jobs.schedule_or_replace(stale)
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    assert await jobs.list_notifications(limit=None) == []
    stored = await jobs.get_job(stale.id)
    assert stored is not None and stored.status is ScheduledJobStatus.COMPLETED
    assert stored.claim_token is None


async def test_a_reminder_for_a_finished_task_is_obsolete(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(days=3))
    )
    deadline = await commitments.get_deadline(task.id)
    assert deadline is not None
    await tasks.complete_task(task.id)
    # Defense in depth: a reminder row a crash left behind after the cancelling mutation.
    leftover = _reminder_job(
        task_id=task.id,
        deadline_id=deadline.id,
        due_at=MONDAY,
        payload_due_at=deadline.due_at,
    )
    await jobs.schedule_or_replace(leftover)
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    assert await jobs.list_notifications(limit=None) == []
    stored = await jobs.get_job(leftover.id)
    assert stored is not None and stored.status is ScheduledJobStatus.COMPLETED


async def test_a_cleared_deadline_makes_the_reminder_obsolete(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    task = await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(days=3))
    )
    deadline = await commitments.get_deadline(task.id)
    assert deadline is not None
    await tasks.clear_deadline(task.id)
    orphan = _reminder_job(
        task_id=task.id,
        deadline_id=deadline.id,
        due_at=MONDAY,
        payload_due_at=deadline.due_at,
    )
    await jobs.schedule_or_replace(orphan)
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    assert await jobs.list_notifications(limit=None) == []


async def test_reminder_delivery_survives_a_crash_between_notification_and_completion(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    """The dangerous window: the message exists, the job does not know it yet."""
    await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(minutes=20))
    )
    reminder = (await _reminders(jobs))[0]
    crashed = await jobs.claim_next(
        worker_id="crashed-worker",
        claim_token=uuid4(),
        now=MONDAY,
        lease_expires_at=MONDAY + timedelta(minutes=5),
    )
    assert crashed is not None
    # The crashed attempt had already written its notification before it died.
    await jobs.create_notification_idempotent(
        _notification_for(reminder.id, created_at=MONDAY)
    )
    clock.advance(6 * 60)  # the crashed worker's lease has expired
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    notifications = await jobs.list_notifications(limit=None)
    assert len(notifications) == 1  # the retry found the original message
    assert notifications[0].body.startswith("Due: earlier")  # the first copy wins
    stored = await jobs.get_job(reminder.id)
    assert stored is not None
    assert stored.status is ScheduledJobStatus.COMPLETED
    assert stored.attempts == 2  # the reclaimed attempt is counted


# --------------------------------------------------------------------- failures


async def test_a_handler_failure_is_retried_with_deterministic_backoff(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(minutes=20))
    )
    scheduler = _service(
        jobs,
        commitments,
        work,
        clock,
        _planner(database, clock, _planning_config()),
        policy=RetryPolicy(max_attempts=3, base_delay=timedelta(seconds=30)),
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(commitments, "get_task", explode)

    assert await scheduler.run_once() is SchedulerRunResult.RETRY_SCHEDULED
    retried = (await _reminders(jobs))[0]
    assert retried.status is ScheduledJobStatus.PENDING
    assert retried.attempts == 1
    assert retried.next_attempt_at == MONDAY + timedelta(seconds=30)
    assert retried.last_error is not None and "RuntimeError" in retried.last_error

    clock.advance(29)
    assert await scheduler.run_once() is SchedulerRunResult.IDLE  # not due yet
    clock.advance(1)
    assert await scheduler.run_once() is SchedulerRunResult.RETRY_SCHEDULED
    again = await jobs.get_job(retried.id)
    assert again is not None
    assert again.attempts == 2
    assert again.effective_due_at == MONDAY + timedelta(seconds=29 + 1 + 60)


async def test_exhausted_attempts_dead_letter_the_job(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(minutes=20))
    )
    scheduler = _service(
        jobs,
        commitments,
        work,
        clock,
        _planner(database, clock, _planning_config()),
        policy=RetryPolicy(max_attempts=2, base_delay=timedelta(seconds=30)),
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("still broken")

    monkeypatch.setattr(commitments, "get_task", explode)
    await scheduler.run_once()
    clock.advance(30)

    assert await scheduler.run_once() is SchedulerRunResult.DEAD_LETTERED
    dead = [
        job
        for job in await _reminders(jobs)
        if job.status is ScheduledJobStatus.DEAD_LETTERED
    ]
    assert len(dead) == 1
    assert dead[0].attempts == 2
    assert dead[0].last_error is not None and "RuntimeError" in dead[0].last_error
    clock.advance(3600)
    assert await scheduler.run_once() is SchedulerRunResult.IDLE


async def test_a_corrupt_payload_is_dead_lettered_immediately(
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    broken = ScheduledJob(
        kind=ScheduledJobKind.DEADLINE_REMINDER,
        due_at=MONDAY,
        dedup_key="deadline-reminder:broken:0",
        payload_json=canonical_payload_json({"task_id": str(uuid4())}),
        created_at=MONDAY,
        updated_at=MONDAY,
    )
    await jobs.schedule_or_replace(broken)
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.DEAD_LETTERED
    stored = await jobs.get_job(broken.id)
    assert stored is not None
    assert stored.status is ScheduledJobStatus.DEAD_LETTERED
    assert stored.attempts == 1  # no retry budget is spent on an unreadable payload


async def test_cancellation_leaves_the_job_processing_for_lease_recovery(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(minutes=20))
    )
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    async def cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(commitments, "get_task", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_once()

    processing = [
        job
        for job in await _reminders(jobs)
        if job.status is ScheduledJobStatus.PROCESSING
    ]
    assert len(processing) == 1
    assert processing[0].attempts == 1
    assert processing[0].last_error is None  # cancellation is not a failure
    assert processing[0].lease_expires_at == MONDAY + timedelta(minutes=5)
    assert await jobs.list_notifications(limit=None) == []


# ------------------------------------------------------------- rolling replan


async def test_rolling_replan_creates_a_pending_proposal_and_notifies(
    replan_tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    await replan_tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300)
    )
    clock.advance(61)  # past the debounce window
    scheduler = _service(
        jobs, commitments, work, clock, _planner(database, clock, _planning_config())
    )

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    proposals = await SqlitePlanningRepository(database).list_proposals(limit=None)
    assert len(proposals) == 1
    assert proposals[0].status.value == "pending"
    notification = (await jobs.list_notifications(limit=None))[0]
    assert notification.kind is NotificationKind.PLAN_READY
    assert notification.title == "Updated plan proposal is ready"
    assert str(proposals[0].id) in notification.body
    assert notification.related_proposal_id == proposals[0].id
    assert notification.dedup_key == plan_ready_notification_key(
        (await _replans(jobs))[0].id
    )
    # Proposal-only safety: applying is still the user's decision, so nothing is scheduled.
    assert (
        await commitments.list_plan_blocks_in_range(
            query_start=MONDAY, query_end=WEEK_END
        )
        == []
    )


async def test_rolling_replan_with_unchanged_input_creates_nothing(
    replan_tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    planner = _planner(database, clock, _planning_config())
    await replan_tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300)
    )
    existing = await planner.create_week_proposal()
    clock.advance(61)
    scheduler = _service(jobs, commitments, work, clock, planner)

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    assert await jobs.list_notifications(limit=None) == []
    proposals = await SqlitePlanningRepository(database).list_proposals(limit=None)
    assert [proposal.id for proposal in proposals] == [existing.proposal.id]


async def test_rolling_replan_supersedes_a_proposal_whose_input_moved(
    replan_tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    planner = _planner(database, clock, _planning_config())
    task = await replan_tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300)
    )
    first = await planner.create_week_proposal()
    # Recorded work changes remaining effort, which changes the plan.
    await WorkService(work, commitments, clock).record_session(
        task_id=task.id,
        started_at=MONDAY,
        ended_at=MONDAY + timedelta(minutes=60),
    )
    clock.advance(61)
    scheduler = _service(jobs, commitments, work, clock, planner)

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    proposals = await SqlitePlanningRepository(database).list_proposals(limit=None)
    assert len(proposals) == 2
    statuses = {proposal.id: proposal.status.value for proposal in proposals}
    assert statuses[first.proposal.id] == "superseded"
    notification = (await jobs.list_notifications(limit=None))[0]
    assert notification.related_proposal_id != first.proposal.id


async def test_rolling_replan_without_planning_configuration_warns_once(
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    await jobs.schedule_or_replace(_replan_job(due_at=MONDAY))
    scheduler = _service(jobs, commitments, work, clock, _planner(database, clock, None))

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED  # not transient: no retry budget spent
    notifications = await jobs.list_notifications(limit=None)
    assert len(notifications) == 1
    assert notifications[0].kind is NotificationKind.SCHEDULER_WARNING
    assert notifications[0].body == PLANNING_NOT_CONFIGURED_MESSAGE
    assert await SqlitePlanningRepository(database).list_proposals(limit=None) == []


async def test_rolling_replan_without_availability_still_proposes_and_reports_issues(
    replan_tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    await replan_tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300)
    )
    clock.advance(61)
    planner = _planner(database, clock, _planning_config(availability=False))
    scheduler = _service(jobs, commitments, work, clock, planner)

    result = await scheduler.run_once()

    assert result is SchedulerRunResult.COMPLETED
    proposals = await SqlitePlanningRepository(database).list_proposals(limit=1)
    detail = await planner.get_proposal_detail(str(proposals[0].id))
    codes = [issue.code.value for issue in detail.issues]
    assert "NO_AVAILABILITY" in codes
    assert detail.blocks == ()
    assert (await jobs.list_notifications(limit=None))[0].kind is NotificationKind.PLAN_READY


async def test_an_in_flight_replan_job_is_left_to_finish_on_fresh_state(
    replan_tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    task = await replan_tasks.create_task(
        CreateTask(title="Write SE lab report", estimated_minutes=300)
    )
    running = (await _replans(jobs))[0]
    claimed = await jobs.claim_next(
        worker_id="w1",
        claim_token=uuid4(),
        now=MONDAY + timedelta(seconds=60),
        lease_expires_at=MONDAY + timedelta(minutes=5),
    )
    assert claimed is not None and claimed.job.id == running.id

    await replan_tasks.set_deadline(task.id, MONDAY + timedelta(days=2))

    replans = await _replans(jobs)
    assert len(replans) == 1  # one active request; the running one is not rewritten
    assert replans[0].status is ScheduledJobStatus.PROCESSING
    assert replans[0].id == running.id


# ----------------------------------------------------------------- run_forever


async def test_run_forever_consumes_due_jobs_then_exits_on_stop(
    tasks: TaskService,
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    await tasks.create_task(
        CreateTask(title="Write SE lab report", due_at=MONDAY + timedelta(minutes=20))
    )
    waiter = RecordingWaiter(stop_after_waits=1)
    scheduler = _service(
        jobs,
        commitments,
        work,
        clock,
        _planner(database, clock, _planning_config()),
        waiter=waiter,
    )
    stop_event = asyncio.Event()

    await asyncio.wait_for(scheduler.run_forever(stop_event), timeout=5)

    assert stop_event.is_set()
    assert len(await jobs.list_notifications(limit=None)) == 1
    assert waiter.calls and all(0 < value <= 15 for value in waiter.calls)


async def test_run_forever_waits_at_most_the_poll_interval(
    jobs: SqliteSchedulerRepository,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    database: Database,
    clock: FakeClock,
) -> None:
    future = _reminder_job(
        task_id=uuid4(),
        deadline_id=uuid4(),
        due_at=MONDAY + timedelta(seconds=30),
        payload_due_at=MONDAY + timedelta(hours=1),
    )
    await jobs.schedule_or_replace(future)
    waiter = RecordingWaiter(stop_after_waits=1)
    scheduler = _service(
        jobs,
        commitments,
        work,
        clock,
        _planner(database, clock, _planning_config()),
        waiter=waiter,
        poll_interval=timedelta(seconds=15),
    )

    await asyncio.wait_for(scheduler.run_forever(asyncio.Event()), timeout=5)

    assert waiter.calls == [15.0]  # capped by the poll interval, never a 300s sleep
