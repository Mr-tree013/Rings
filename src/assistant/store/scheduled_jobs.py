"""Row mapping and connection-level helpers for scheduled jobs and notifications (ADR-0016).

Two stores need these helpers: the commitment store, which materializes reminder jobs inside
the same transaction as the deadline or task mutation that changed them, and the scheduler
store, which claims and completes jobs. Sharing the SQL keeps one definition of what a job row
means, while transactions stay owned by the caller — a helper here never opens or commits one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import DomainError
from assistant.domain.notification import (
    Notification,
    NotificationKind,
    NotificationStatus,
)
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobKind,
    ScheduledJobStatus,
)
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

JOB_FIELDS = (
    "id, kind, status, due_at, next_attempt_at, dedup_key, payload_json, attempts, last_error, "
    "created_at, updated_at, completed_at, cancelled_at, claim_token, claimed_by, claimed_at, "
    "lease_expires_at"
)

NOTIFICATION_FIELDS = (
    "id, kind, status, title, body, related_task_id, related_proposal_id, dedup_key, "
    "created_at, read_at"
)

_ACTIVE_STATUS_VALUES = tuple(
    str(status) for status in (ScheduledJobStatus.PENDING, ScheduledJobStatus.PROCESSING)
)


def row_to_job(row: sqlite3.Row) -> ScheduledJob:
    """Rebuild a `ScheduledJob` from its row."""
    try:
        return ScheduledJob(
            id=UUID(str(row["id"])),
            kind=ScheduledJobKind(str(row["kind"])),
            status=ScheduledJobStatus(str(row["status"])),
            due_at=from_utc_iso(str(row["due_at"])),
            next_attempt_at=_optional_instant(row["next_attempt_at"]),
            dedup_key=str(row["dedup_key"]),
            payload_json=str(row["payload_json"]),
            attempts=int(row["attempts"]),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            completed_at=_optional_instant(row["completed_at"]),
            cancelled_at=_optional_instant(row["cancelled_at"]),
            claim_token=(
                None if row["claim_token"] is None else UUID(str(row["claim_token"]))
            ),
            claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
            claimed_at=_optional_instant(row["claimed_at"]),
            lease_expires_at=_optional_instant(row["lease_expires_at"]),
        )
    except (ValueError, DomainError, KeyError) as exc:
        raise CommitmentStoreError(f"stored scheduled job is not readable: {exc}") from exc


def job_parameters(job: ScheduledJob) -> tuple[object, ...]:
    """All persisted job columns, in `JOB_FIELDS` order."""
    return (
        str(job.id),
        str(job.kind),
        str(job.status),
        to_utc_iso(job.due_at),
        None if job.next_attempt_at is None else to_utc_iso(job.next_attempt_at),
        job.dedup_key,
        job.payload_json,
        job.attempts,
        job.last_error,
        to_utc_iso(job.created_at),
        to_utc_iso(job.updated_at),
        None if job.completed_at is None else to_utc_iso(job.completed_at),
        None if job.cancelled_at is None else to_utc_iso(job.cancelled_at),
        None if job.claim_token is None else str(job.claim_token),
        job.claimed_by,
        None if job.claimed_at is None else to_utc_iso(job.claimed_at),
        None if job.lease_expires_at is None else to_utc_iso(job.lease_expires_at),
    )


def insert_job(connection: sqlite3.Connection, job: ScheduledJob) -> None:
    """Insert a brand-new job row."""
    connection.execute(
        f"INSERT INTO scheduled_jobs ({JOB_FIELDS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        job_parameters(job),
    )


def select_active_job_for_dedup(
    connection: sqlite3.Connection, dedup_key: str
) -> sqlite3.Row | None:
    """Return the active row (if any) that currently owns `dedup_key`."""
    placeholders = ", ".join("?" for _ in _ACTIVE_STATUS_VALUES)
    row: sqlite3.Row | None = connection.execute(
        f"SELECT {JOB_FIELDS} FROM scheduled_jobs "
        f"WHERE dedup_key = ? AND status IN ({placeholders})",
        (dedup_key, *_ACTIVE_STATUS_VALUES),
    ).fetchone()
    return row


def upsert_job_for_dedup(
    connection: sqlite3.Connection, job: ScheduledJob
) -> ScheduledJob:
    """Store `job`, coalescing with an active job that already owns its dedup key.

    - no active job: the row is inserted;
    - an active *pending* job: its due time and payload are pushed forward in place, so a
      debounce window stays one job instead of accumulating rows;
    - an active *processing* job: it is left alone. Its handler is already running and reads
      the authoritative state at execution time, so rewriting it underneath would not make
      the outcome fresher.
    """
    existing_row = select_active_job_for_dedup(connection, job.dedup_key)
    if existing_row is None:
        insert_job(connection, job)
        return job
    existing = row_to_job(existing_row)
    if existing.status is not ScheduledJobStatus.PENDING:
        return existing
    updated = existing.reschedule(
        due_at=job.due_at, payload_json=job.payload_json, at=job.updated_at
    )
    connection.execute(
        "UPDATE scheduled_jobs SET due_at = ?, payload_json = ?, updated_at = ?, "
        "next_attempt_at = NULL, last_error = NULL WHERE id = ?",
        (
            to_utc_iso(updated.due_at),
            updated.payload_json,
            to_utc_iso(updated.updated_at),
            str(updated.id),
        ),
    )
    return updated


def cancel_active_jobs_for_task(
    connection: sqlite3.Connection,
    *,
    task_id: UUID,
    kind: ScheduledJobKind,
    keep_dedup_keys: Collection[str],
    at: datetime,
) -> int:
    """Cancel the task's active jobs of `kind` that are not in `keep_dedup_keys`.

    Cancelling also clears the lease, so a worker that is mid-flight loses its claim and its
    completion is rejected (`StaleScheduledJobClaim`) instead of delivering a stale reminder.
    """
    rows = connection.execute(
        f"SELECT {JOB_FIELDS} FROM scheduled_jobs "
        "WHERE kind = ? AND json_extract(payload_json, '$.task_id') = ?",
        (str(kind), str(task_id)),
    ).fetchall()
    cancelled = 0
    for row in rows:
        job = row_to_job(row)
        if not job.is_active or job.dedup_key in keep_dedup_keys:
            continue
        connection.execute(
            "UPDATE scheduled_jobs SET status = ?, cancelled_at = ?, updated_at = ?, "
            "next_attempt_at = NULL, claim_token = NULL, claimed_by = NULL, claimed_at = NULL, "
            "lease_expires_at = NULL WHERE id = ? AND status IN (?, ?)",
            (
                str(ScheduledJobStatus.CANCELLED),
                to_utc_iso(at),
                to_utc_iso(at),
                str(job.id),
                *_ACTIVE_STATUS_VALUES,
            ),
        )
        cancelled += 1
    return cancelled


def row_to_notification(row: sqlite3.Row) -> Notification:
    """Rebuild a `Notification` from its row."""
    try:
        return Notification(
            id=UUID(str(row["id"])),
            kind=NotificationKind(str(row["kind"])),
            status=NotificationStatus(str(row["status"])),
            title=str(row["title"]),
            body=str(row["body"]),
            related_task_id=(
                None if row["related_task_id"] is None else UUID(str(row["related_task_id"]))
            ),
            related_proposal_id=(
                None
                if row["related_proposal_id"] is None
                else UUID(str(row["related_proposal_id"]))
            ),
            dedup_key=str(row["dedup_key"]),
            created_at=from_utc_iso(str(row["created_at"])),
            read_at=_optional_instant(row["read_at"]),
        )
    except (ValueError, DomainError, KeyError) as exc:
        raise CommitmentStoreError(f"stored notification is not readable: {exc}") from exc


def notification_parameters(notification: Notification) -> tuple[object, ...]:
    """All persisted notification columns, in `NOTIFICATION_FIELDS` order."""
    return (
        str(notification.id),
        str(notification.kind),
        str(notification.status),
        notification.title,
        notification.body,
        None if notification.related_task_id is None else str(notification.related_task_id),
        (
            None
            if notification.related_proposal_id is None
            else str(notification.related_proposal_id)
        ),
        notification.dedup_key,
        to_utc_iso(notification.created_at),
        None if notification.read_at is None else to_utc_iso(notification.read_at),
    )


def insert_notification_idempotent(
    connection: sqlite3.Connection, notification: Notification
) -> Notification:
    """Insert the notification, or return the one that already owns its dedup key.

    The database enforces this, not a `SELECT` beforehand: a job that crashes between its
    notification insert and its completion must find the original message when it runs again.
    """
    connection.execute(
        f"INSERT INTO notifications ({NOTIFICATION_FIELDS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (dedup_key) DO NOTHING",
        notification_parameters(notification),
    )
    row = connection.execute(
        f"SELECT {NOTIFICATION_FIELDS} FROM notifications WHERE dedup_key = ?",
        (notification.dedup_key,),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive: the insert or an existing row must exist
        raise CommitmentStoreError(
            f"notification with dedup key {notification.dedup_key!r} could not be stored"
        )
    return row_to_notification(row)


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else from_utc_iso(str(value))


__all__ = [
    "JOB_FIELDS",
    "NOTIFICATION_FIELDS",
    "cancel_active_jobs_for_task",
    "insert_job",
    "insert_notification_idempotent",
    "job_parameters",
    "notification_parameters",
    "row_to_job",
    "row_to_notification",
    "select_active_job_for_dedup",
    "upsert_job_for_dedup",
]
