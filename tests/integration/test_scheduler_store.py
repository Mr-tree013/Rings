"""Real-SQLite tests for durable job claims, fencing and notification idempotency (ADR-0016).

These are the properties that would be fabricated by a fake repository: two workers racing for
one job, an expired lease being recovered with a new token, a stale completion being rejected,
and a crash between "notification written" and "job completed" still leaving exactly one
notification.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.errors import (
    NotificationNotFound,
    StaleScheduledJobClaim,
)
from assistant.domain.notification import (
    Notification,
    NotificationKind,
    NotificationStatus,
)
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
)
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
LEASE = NOW + timedelta(minutes=5)
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")
DEADLINE_ID = uuid4()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    # Notifications reference a task, exactly as the schema requires; give them a real one.
    timestamp = "2026-09-20T12:00:00.000000+00:00"
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, 'Write SE lab report', 'open', 'normal', ?, ?)",
            (str(TASK_ID), timestamp, timestamp),
        )
    return db


@pytest.fixture
def scheduler(database: Database) -> SqliteSchedulerRepository:
    return SqliteSchedulerRepository(database)


def _reminder_job(*, due_at: datetime = NOW, offset: int = 120) -> ScheduledJob:
    payload = DeadlineReminderPayload(
        task_id=TASK_ID,
        deadline_id=DEADLINE_ID,
        deadline_due_at=LATER,
        reminder_offset_minutes=offset,
    )
    return ScheduledJob(
        kind=ScheduledJobKind.DEADLINE_REMINDER,
        due_at=due_at,
        dedup_key=f"deadline-reminder:{DEADLINE_ID}:h:{offset}",
        payload_json=canonical_payload_json(payload.to_payload()),
        created_at=NOW,
        updated_at=NOW,
    )


def _replan_job(*, due_at: datetime = NOW) -> ScheduledJob:
    payload = RollingReplanPayload(timezone="Asia/Shanghai")
    return ScheduledJob(
        kind=ScheduledJobKind.ROLLING_REPLAN,
        due_at=due_at,
        dedup_key=ROLLING_REPLAN_DEDUP_KEY,
        payload_json=canonical_payload_json(payload.to_payload()),
        created_at=NOW,
        updated_at=NOW,
    )


def _notification(
    job_id: UUID,
    *,
    title: str = "Deadline approaching",
    created_at: datetime = NOW,
) -> Notification:
    return Notification(
        kind=NotificationKind.DEADLINE_REMINDER,
        title=title,
        body="Due: 2026-09-20T13:00:00+00:00",
        dedup_key=f"notification:deadline:{job_id}",
        created_at=created_at,
        related_task_id=TASK_ID,
    )


async def _claim(
    scheduler: SqliteSchedulerRepository,
    *,
    worker: str = "w1",
    at: datetime = NOW,
    lease_until: datetime | None = None,
):
    return await scheduler.claim_next(
        worker_id=worker,
        claim_token=uuid4(),
        now=at,
        lease_expires_at=lease_until if lease_until is not None else at + timedelta(minutes=5),
    )


# ------------------------------------------------------------------------- schema


def test_schema_enforces_job_and_notification_invariants(database: Database) -> None:
    stamp = "2026-09-20T12:00:00.000000+00:00"
    insert = (
        "INSERT INTO scheduled_jobs "
        "(id, kind, status, due_at, dedup_key, payload_json, attempts, created_at, updated_at"
    )
    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="kind_is_known"):
            connection.execute(
                f"{insert}) VALUES (?, 'send_email', 'pending', ?, 'k', '{{}}', 0, ?, ?)",
                (str(uuid4()), stamp, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="payload_is_object"):
            connection.execute(
                f"{insert}) VALUES (?, 'rolling_replan', 'pending', ?, 'k', '[1]', 0, ?, ?)",
                (str(uuid4()), stamp, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="lease_matches_status"):
            connection.execute(
                f"{insert}, claim_token, claimed_by, claimed_at, lease_expires_at) "
                "VALUES (?, 'rolling_replan', 'pending', ?, 'k', '{}', 0, ?, ?, ?, 'w', ?, ?)",
                (str(uuid4()), stamp, stamp, stamp, str(uuid4()), stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="terminal_timestamps_are_consistent"):
            connection.execute(
                f"{insert}) VALUES (?, 'rolling_replan', 'completed', ?, 'k', '{{}}', 0, ?, ?)",
                (str(uuid4()), stamp, stamp, stamp),
            )


async def test_only_one_active_job_may_own_a_dedup_key(
    scheduler: SqliteSchedulerRepository,
) -> None:
    job = _replan_job()
    await scheduler.schedule_or_replace(job)
    await scheduler.schedule_or_replace(_replan_job(due_at=NOW + timedelta(minutes=1)))

    stored = await scheduler.get_job(job.id)
    assert stored is not None
    assert stored.due_at == NOW + timedelta(minutes=1)  # coalesced in place
    assert len(await scheduler.list_jobs(statuses=None)) == 1

    await scheduler.cancel_active_by_dedup_key(ROLLING_REPLAN_DEDUP_KEY, at=LATER)
    replacement = await scheduler.schedule_or_replace(_replan_job(due_at=LATER))
    assert replacement.id != job.id  # the cancelled job no longer owns the key
    assert len(await scheduler.list_jobs(statuses=None)) == 2


async def test_a_processing_job_is_not_rewritten_underneath_its_worker(
    scheduler: SqliteSchedulerRepository,
) -> None:
    job = _reminder_job()
    await scheduler.schedule_or_replace(job)
    claim = await _claim(scheduler)
    assert claim is not None

    coalesced = await scheduler.schedule_or_replace(_reminder_job(due_at=LATER))

    assert coalesced.id == job.id
    assert coalesced.status is ScheduledJobStatus.PROCESSING
    assert coalesced.due_at == NOW  # untouched


# ------------------------------------------------------------------------- claims


async def test_due_jobs_are_claimed_in_effective_due_order(
    scheduler: SqliteSchedulerRepository,
) -> None:
    later = _reminder_job(due_at=LATER, offset=60)
    earlier = _reminder_job(due_at=NOW, offset=120)
    await scheduler.schedule_or_replace(later)
    await scheduler.schedule_or_replace(earlier)

    claim = await _claim(scheduler)

    assert claim is not None
    assert claim.job.id == earlier.id
    assert claim.job.status is ScheduledJobStatus.PROCESSING
    assert claim.job.attempts == 1
    assert claim.claimed_by == "w1"
    assert claim.job.next_attempt_at is None


async def test_claim_eligibility_rules(scheduler: SqliteSchedulerRepository) -> None:
    future = _reminder_job(due_at=LATER, offset=30)
    await scheduler.schedule_or_replace(future)
    assert await _claim(scheduler) is None  # future PENDING is not eligible

    claimed = await _claim(scheduler, at=LATER, lease_until=LATER + timedelta(minutes=5))
    assert claimed is not None
    assert await _claim(scheduler, at=LATER, lease_until=LATER + timedelta(minutes=5)) is None

    reclaimed = await _claim(
        scheduler,
        worker="w2",
        at=LATER + timedelta(minutes=5),
        lease_until=LATER + timedelta(minutes=10),
    )
    assert reclaimed is not None
    assert reclaimed.job.attempts == 2
    assert reclaimed.claim_token != claimed.claim_token
    assert reclaimed.claimed_by == "w2"

    await scheduler.complete_claim(
        future.id, claim_token=reclaimed.claim_token, completed_at=LATER
    )
    assert await _claim(scheduler, at=LATER + timedelta(hours=1)) is None


async def test_a_retry_time_makes_a_job_eligible_again(
    scheduler: SqliteSchedulerRepository,
) -> None:
    job = _replan_job()
    await scheduler.schedule_or_replace(job)
    claim = await _claim(scheduler)
    assert claim is not None

    retried = await scheduler.retry_claim(
        job.id,
        claim_token=claim.claim_token,
        failed_at=NOW,
        error="boom",
        next_attempt_at=NOW + timedelta(seconds=30),
    )

    assert retried.status is ScheduledJobStatus.PENDING
    assert retried.attempts == 1
    assert retried.last_error == "boom"
    assert await _claim(scheduler, at=NOW + timedelta(seconds=29)) is None
    again = await _claim(scheduler, at=NOW + timedelta(seconds=30))
    assert again is not None
    assert again.job.attempts == 2


async def test_only_one_of_two_workers_claims_a_job(
    scheduler: SqliteSchedulerRepository,
) -> None:
    await scheduler.schedule_or_replace(_replan_job())

    first = await scheduler.claim_next(
        worker_id="w1", claim_token=uuid4(), now=NOW, lease_expires_at=LEASE
    )
    second = await scheduler.claim_next(
        worker_id="w2", claim_token=uuid4(), now=NOW, lease_expires_at=LEASE
    )

    assert first is not None
    assert second is None  # the row is already PROCESSING under a live lease


async def test_stale_completion_is_rejected(scheduler: SqliteSchedulerRepository) -> None:
    job = _replan_job()
    await scheduler.schedule_or_replace(job)
    claim = await _claim(scheduler)
    assert claim is not None

    with pytest.raises(StaleScheduledJobClaim):
        await scheduler.complete_claim(job.id, claim_token=uuid4(), completed_at=LATER)
    with pytest.raises(StaleScheduledJobClaim):
        await scheduler.retry_claim(
            job.id,
            claim_token=uuid4(),
            failed_at=LATER,
            error="boom",
            next_attempt_at=LATER,
        )
    with pytest.raises(StaleScheduledJobClaim):
        await scheduler.dead_letter_claim(
            job.id, claim_token=uuid4(), failed_at=LATER, error="boom"
        )

    stored = await scheduler.get_job(job.id)
    assert stored is not None and stored.status is ScheduledJobStatus.PROCESSING


async def test_terminal_and_cancelled_jobs_are_never_claimed(
    scheduler: SqliteSchedulerRepository,
) -> None:
    cancelled = _reminder_job(offset=10)
    await scheduler.schedule_or_replace(cancelled)
    assert await scheduler.cancel_active_by_dedup_key(cancelled.dedup_key, at=NOW) == 1
    assert await _claim(scheduler) is None

    dead = _reminder_job(offset=20)
    await scheduler.schedule_or_replace(dead)
    claim = await _claim(scheduler)
    assert claim is not None
    await scheduler.dead_letter_claim(
        dead.id, claim_token=claim.claim_token, failed_at=NOW, error="permanent"
    )
    assert await _claim(scheduler, at=LATER) is None

    stored = await scheduler.get_job(dead.id)
    assert stored is not None
    assert stored.status is ScheduledJobStatus.DEAD_LETTERED
    assert stored.last_error == "permanent"


async def test_claim_rejects_nonsense_arguments(scheduler: SqliteSchedulerRepository) -> None:
    with pytest.raises(ValueError):
        await scheduler.claim_next(
            worker_id="  ", claim_token=uuid4(), now=NOW, lease_expires_at=LEASE
        )
    with pytest.raises(ValueError):
        await scheduler.claim_next(
            worker_id="w", claim_token=UUID(int=0), now=NOW, lease_expires_at=LEASE
        )
    with pytest.raises(ValueError):
        await scheduler.claim_next(
            worker_id="w", claim_token=uuid4(), now=NOW, lease_expires_at=NOW
        )


async def test_next_wakeup_reports_the_earliest_pending_moment(
    scheduler: SqliteSchedulerRepository,
) -> None:
    assert await scheduler.next_wakeup_at() is None
    await scheduler.schedule_or_replace(_reminder_job(due_at=LATER, offset=30))
    await scheduler.schedule_or_replace(_replan_job(due_at=NOW + timedelta(minutes=10)))

    assert await scheduler.next_wakeup_at() == NOW + timedelta(minutes=10)

    claim = await _claim(
        scheduler,
        at=NOW + timedelta(minutes=10),
        lease_until=LATER + timedelta(hours=1),
    )
    assert claim is not None
    # The reminder is still pending and due before the claimed job's lease runs out.
    assert await scheduler.next_wakeup_at() == LATER


# ------------------------------------------------------------------ notifications


async def test_notification_creation_is_idempotent_by_dedup_key(
    scheduler: SqliteSchedulerRepository,
) -> None:
    job_id = uuid4()
    first = await scheduler.create_notification_idempotent(_notification(job_id))
    second = await scheduler.create_notification_idempotent(
        _notification(job_id, title="a retry with different text")
    )

    assert first.id == second.id
    assert second.title == "Deadline approaching"  # the original message wins
    assert len(await scheduler.list_notifications(limit=None)) == 1


async def test_completion_with_notification_is_one_transaction(
    scheduler: SqliteSchedulerRepository, database: Database
) -> None:
    job = _reminder_job()
    await scheduler.schedule_or_replace(job)
    claim = await _claim(scheduler)
    assert claim is not None

    completed, notification = await scheduler.complete_claim_with_notification(
        job.id,
        claim_token=claim.claim_token,
        completed_at=LATER,
        notification=_notification(job.id),
    )

    assert completed.status is ScheduledJobStatus.COMPLETED
    assert completed.completed_at == LATER
    assert notification.related_task_id == TASK_ID
    assert await scheduler.get_notification(notification.id) is not None


async def test_completion_with_notification_rolls_back_when_the_claim_is_gone(
    scheduler: SqliteSchedulerRepository,
) -> None:
    job = _reminder_job()
    await scheduler.schedule_or_replace(job)
    claim = await _claim(scheduler)
    assert claim is not None

    with pytest.raises(StaleScheduledJobClaim):
        await scheduler.complete_claim_with_notification(
            job.id,
            claim_token=uuid4(),  # the caller lost the fence
            completed_at=LATER,
            notification=_notification(job.id),
        )

    assert await scheduler.list_notifications(limit=None) == []
    stored = await scheduler.get_job(job.id)
    assert stored is not None and stored.status is ScheduledJobStatus.PROCESSING


async def test_reading_a_notification_is_idempotent_and_ordered(
    scheduler: SqliteSchedulerRepository,
) -> None:
    first = await scheduler.create_notification_idempotent(_notification(uuid4()))
    await scheduler.create_notification_idempotent(
        _notification(uuid4(), title="second", created_at=NOW + timedelta(minutes=1))
    )

    listed = await scheduler.list_notifications(limit=10)
    assert [item.title for item in listed] == ["second", "Deadline approaching"]

    read = await scheduler.mark_notification_read(first.id, at=LATER)
    again = await scheduler.mark_notification_read(first.id, at=LATER + timedelta(hours=1))

    assert read.status is NotificationStatus.READ
    assert again.read_at == LATER  # the first read time is kept
    assert [item.id for item in await scheduler.list_notifications(unread_only=True)] != [
        first.id
    ]
    with pytest.raises(NotificationNotFound):
        await scheduler.mark_notification_read(uuid4(), at=LATER)


async def test_notification_ids_resolve_by_unique_prefix(
    scheduler: SqliteSchedulerRepository,
) -> None:
    notification = await scheduler.create_notification_idempotent(_notification(uuid4()))

    assert await scheduler.resolve_notification_id(str(notification.id)[:8]) == notification.id
    with pytest.raises(NotificationNotFound):
        await scheduler.resolve_notification_id("ffffffff")
