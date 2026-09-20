"""ScheduledJob, payload and Notification invariants (ADR-0016)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.errors import (
    InvalidNotification,
    InvalidScheduledJob,
    InvalidScheduledJobClaim,
    InvalidScheduledJobPayload,
)
from assistant.domain.notification import (
    Notification,
    NotificationKind,
    NotificationStatus,
)
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobClaim,
    ScheduledJobKind,
    ScheduledJobStatus,
    canonical_payload_json,
)
from assistant.domain.scheduler_payloads import (
    ROLLING_REPLAN_DEDUP_KEY,
    RollingReplanPayload,
    RollingReplanWindowKind,
    deadline_reminder_dedup_key,
    deadline_reminder_jobs,
    deadline_reminder_notification_key,
    parse_deadline_reminder_payload,
    parse_rolling_replan_payload,
    plan_ready_notification_key,
    scheduler_warning_notification_key,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=4)
DEADLINE_ID = uuid4()
TASK_ID = uuid4()


def _payload(**overrides: object) -> str:
    values: dict[str, object] = {
        "task_id": str(TASK_ID),
        "deadline_id": str(DEADLINE_ID),
        "deadline_due_at": LATER.isoformat(),
        "reminder_offset_minutes": 120,
    }
    values.update(overrides)
    return canonical_payload_json(values)


def _job(**overrides: object) -> ScheduledJob:
    values: dict[str, object] = {
        "kind": ScheduledJobKind.DEADLINE_REMINDER,
        "due_at": NOW,
        "dedup_key": "deadline-reminder:x:y:120",
        "payload_json": _payload(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return ScheduledJob(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- job


def test_a_job_starts_pending_and_unclaimed() -> None:
    job = _job()

    assert job.status is ScheduledJobStatus.PENDING
    assert job.is_active
    assert job.effective_due_at == job.due_at
    assert job.attempts == 0
    assert job.claim_token is None
    assert job.is_lease_expired(NOW) is False


@pytest.mark.parametrize(
    "payload_json",
    (
        "not json",
        "[1, 2]",
        '{"b": 1, "a": 2}',  # not canonical (keys must be sorted)
        '{"a":1} ',
    ),
)
def test_payloads_must_be_canonical_json_objects(payload_json: str) -> None:
    with pytest.raises(InvalidScheduledJobPayload):
        _job(payload_json=payload_json)


def test_job_invariants_are_checked() -> None:
    with pytest.raises(InvalidScheduledJob):
        _job(dedup_key="   ")
    with pytest.raises(InvalidScheduledJob):
        _job(attempts=-1)
    with pytest.raises(InvalidScheduledJob):
        _job(due_at=datetime(2026, 9, 20, 12, 0))  # naive
    with pytest.raises(InvalidScheduledJob):
        _job(status=ScheduledJobStatus.COMPLETED)  # needs completed_at
    with pytest.raises(InvalidScheduledJob):
        _job(claimed_by="worker")  # a partial claim
    with pytest.raises(InvalidScheduledJob):
        _job(
            status=ScheduledJobStatus.PROCESSING,
            claim_token=uuid4(),
            claimed_by="worker",
            claimed_at=NOW,
            lease_expires_at=LATER,
            next_attempt_at=LATER,  # a processing job has no retry time
        )


def test_claim_increments_attempts_and_fences_by_token() -> None:
    token = uuid4()
    claimed = _job().claim(
        worker_id="scheduler-1", claim_token=token, at=NOW, lease_expires_at=LATER
    )

    assert claimed.status is ScheduledJobStatus.PROCESSING
    assert claimed.attempts == 1
    assert claimed.claim_token == token
    assert claimed.claimed_by == "scheduler-1"
    assert claimed.lease_expires_at == LATER
    assert claimed.is_lease_expired(LATER) is True
    assert claimed.is_lease_expired(LATER - timedelta(seconds=1)) is False

    reclaimed = claimed.claim(
        worker_id="scheduler-2",
        claim_token=uuid4(),
        at=LATER,
        lease_expires_at=LATER + timedelta(hours=1),
    )
    assert reclaimed.attempts == 2
    assert reclaimed.claimed_by == "scheduler-2"
    assert reclaimed.claim_token != token


def test_terminal_transitions_and_retry() -> None:
    completed = (
        _job()
        .claim(worker_id="w", claim_token=uuid4(), at=NOW, lease_expires_at=LATER)
        .complete(LATER)
    )
    assert completed.status is ScheduledJobStatus.COMPLETED
    assert completed.completed_at == LATER
    assert completed.claim_token is None
    assert completed.is_active is False

    retried = (
        _job()
        .claim(worker_id="w", claim_token=uuid4(), at=NOW, lease_expires_at=LATER)
        .release_for_retry(next_attempt_at=LATER, error="boom", at=NOW + timedelta(minutes=1))
    )
    assert retried.status is ScheduledJobStatus.PENDING
    assert retried.attempts == 1
    assert retried.last_error == "boom"
    assert retried.effective_due_at == LATER
    assert retried.claim_token is None

    dead = (
        _job()
        .claim(worker_id="w", claim_token=uuid4(), at=NOW, lease_expires_at=LATER)
        .dead_letter(error="boom", at=LATER)
    )
    assert dead.status is ScheduledJobStatus.DEAD_LETTERED
    assert dead.completed_at == LATER

    cancelled = _job().cancel(LATER)
    assert cancelled.status is ScheduledJobStatus.CANCELLED
    assert cancelled.cancelled_at == LATER
    assert cancelled.is_active is False


def test_illegal_transitions_are_refused() -> None:
    terminal = _job().cancel(NOW)

    with pytest.raises(InvalidScheduledJob):
        terminal.cancel(NOW)
    with pytest.raises(InvalidScheduledJob):
        terminal.claim(worker_id="w", claim_token=uuid4(), at=NOW, lease_expires_at=LATER)
    with pytest.raises(InvalidScheduledJob):
        _job().complete(NOW)  # a pending job is not claimed
    with pytest.raises(InvalidScheduledJob):
        _job().release_for_retry(next_attempt_at=LATER, error="x", at=NOW)
    with pytest.raises(InvalidScheduledJob):
        _job().dead_letter(error="x", at=NOW)
    with pytest.raises(InvalidScheduledJob):
        _job(
            status=ScheduledJobStatus.PROCESSING,
            claim_token=uuid4(),
            claimed_by="w",
            claimed_at=NOW,
            lease_expires_at=LATER,
        ).reschedule(due_at=LATER, payload_json=_payload(), at=NOW)


def test_rescheduling_pushes_a_pending_job_forward() -> None:
    moved = _job().reschedule(due_at=LATER, payload_json=_payload(), at=NOW)

    assert moved.due_at == LATER
    assert moved.updated_at == NOW
    assert moved.status is ScheduledJobStatus.PENDING


def test_claim_type_requires_a_processing_job() -> None:
    with pytest.raises(InvalidScheduledJobClaim):
        ScheduledJobClaim(
            job=_job(),
            claim_token=uuid4(),
            claimed_by="w",
            claimed_at=NOW,
            lease_expires_at=LATER,
        )

    claimed = _job().claim(
        worker_id="w", claim_token=uuid4(), at=NOW, lease_expires_at=LATER
    )
    claim = ScheduledJobClaim(
        job=claimed,
        claim_token=claimed.claim_token or uuid4(),
        claimed_by="w",
        claimed_at=NOW,
        lease_expires_at=LATER,
    )
    assert claim.is_lease_expired(LATER) is True


# ------------------------------------------------------------------------ payloads


def test_deadline_reminder_payload_round_trips() -> None:
    jobs = deadline_reminder_jobs(
        task_id=TASK_ID,
        deadline_id=DEADLINE_ID,
        deadline_due_at=LATER,
        offsets_minutes=(1440, 120),
        now=NOW,
    )

    assert len(jobs) == 2
    parsed = [parse_deadline_reminder_payload(job.payload_json) for job in jobs]
    assert [payload.reminder_offset_minutes for payload in parsed] == [1440, 120]
    assert all(payload.task_id == TASK_ID for payload in parsed)
    assert all(payload.deadline_id == DEADLINE_ID for payload in parsed)
    assert all(payload.deadline_due_at == LATER for payload in parsed)
    # The 24h reminder is already in the past and becomes due now; the 2h reminder stays ahead.
    assert [job.due_at for job in jobs] == [NOW, NOW + timedelta(hours=2)]


def test_reminder_due_times_are_offsets_before_the_deadline() -> None:
    deadline = NOW + timedelta(days=3)
    jobs = deadline_reminder_jobs(
        task_id=TASK_ID,
        deadline_id=DEADLINE_ID,
        deadline_due_at=deadline,
        offsets_minutes=(1440, 120),
        now=NOW,
    )

    assert [job.due_at for job in jobs] == [
        deadline - timedelta(minutes=1440),
        deadline - timedelta(minutes=120),
    ]


def test_reminder_dedup_identity_includes_the_due_instant() -> None:
    same = deadline_reminder_dedup_key(DEADLINE_ID, due_at=LATER, offset_minutes=120)
    moved = deadline_reminder_dedup_key(
        DEADLINE_ID, due_at=LATER + timedelta(minutes=1), offset_minutes=120
    )
    other_offset = deadline_reminder_dedup_key(DEADLINE_ID, due_at=LATER, offset_minutes=60)

    assert len({same, moved, other_offset}) == 3
    assert same == deadline_reminder_dedup_key(DEADLINE_ID, due_at=LATER, offset_minutes=120)
    assert same.startswith("deadline-reminder:")


def test_notification_dedup_keys_are_per_job() -> None:
    job_id = uuid4()

    keys = {
        deadline_reminder_notification_key(job_id),
        plan_ready_notification_key(job_id),
        scheduler_warning_notification_key(job_id),
    }
    assert len(keys) == 3
    assert all(str(job_id) in key for key in keys)


def test_rolling_replan_payload_round_trips() -> None:
    payload = RollingReplanPayload(timezone="Asia/Shanghai")
    assert payload.window_kind is RollingReplanWindowKind.CURRENT_WEEK

    parsed = parse_rolling_replan_payload(canonical_payload_json(payload.to_payload()))

    assert parsed == payload
    assert ROLLING_REPLAN_DEDUP_KEY == "rolling-replan:current-week"


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"timezone": "UTC"},
        {"timezone": "UTC", "window_kind": "next_month"},
        {"timezone": "", "window_kind": "current_week"},
    ),
)
def test_invalid_rolling_replan_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidScheduledJobPayload):
        parse_rolling_replan_payload(canonical_payload_json(payload))


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"task_id": "not-a-uuid", "deadline_id": str(DEADLINE_ID)},
        {
            "task_id": str(TASK_ID),
            "deadline_id": str(DEADLINE_ID),
            "deadline_due_at": "2026-09-20T16:00:00",
            "reminder_offset_minutes": 60,
        },
        {
            "task_id": str(TASK_ID),
            "deadline_id": str(DEADLINE_ID),
            "deadline_due_at": LATER.isoformat(),
            "reminder_offset_minutes": -1,
        },
    ),
)
def test_invalid_reminder_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidScheduledJobPayload):
        parse_deadline_reminder_payload(canonical_payload_json(payload))


# -------------------------------------------------------------------- notifications


def test_notification_starts_unread_and_can_be_read_once() -> None:
    notification = Notification(
        kind=NotificationKind.DEADLINE_REMINDER,
        title="Deadline approaching: SE Lab",
        body="Due: 2026-09-21T13:00:00+08:00",
        dedup_key="notification:deadline:x",
        created_at=NOW,
        related_task_id=TASK_ID,
    )

    assert notification.status is NotificationStatus.UNREAD
    assert notification.is_unread
    assert notification.read_at is None

    read = notification.mark_read(LATER)
    assert read.status is NotificationStatus.READ
    assert read.read_at == LATER
    assert read.is_unread is False
    assert read.mark_read(LATER + timedelta(hours=1)) is read  # idempotent


def test_notification_invariants_are_checked() -> None:
    values: dict[str, object] = {
        "kind": NotificationKind.PLAN_READY,
        "title": "ready",
        "body": "body",
        "dedup_key": "key",
        "created_at": NOW,
    }
    for overrides in (
        {"title": "  "},
        {"body": ""},
        {"dedup_key": " "},
        {"status": NotificationStatus.READ},
        {"status": NotificationStatus.UNREAD, "read_at": LATER},
        {"created_at": datetime(2026, 9, 20, 12, 0)},
    ):
        with pytest.raises(InvalidNotification):
            Notification(**{**values, **overrides})  # type: ignore[arg-type]
