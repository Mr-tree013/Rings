"""SQLite implementation of the scheduler port: claims, fencing and the inbox (ADR-0016).

Everything durable about scheduling lives here:

- `schedule_or_replace` coalesces through the partial unique index on active dedup keys;
- `claim_next` selects and updates the same row inside one `BEGIN IMMEDIATE` transaction, so
  two workers cannot claim the same job and an expired lease is recoverable;
- `complete_claim` / `retry_claim` / `dead_letter_claim` only touch a row that is still
  `PROCESSING` under the caller's claim token, which makes an expired worker harmless;
- `complete_claim_with_notification` writes the notification and finishes the job in the same
  transaction, which is what makes reminder delivery idempotent across a crash.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    NotificationNotFound,
    StaleScheduledJobClaim,
)
from assistant.domain.notification import Notification, NotificationId
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobClaim,
    ScheduledJobId,
    ScheduledJobStatus,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.scheduled_jobs import (
    JOB_FIELDS,
    NOTIFICATION_FIELDS,
    insert_notification_idempotent,
    row_to_job,
    row_to_notification,
    upsert_job_for_dedup,
)
from assistant.store.serialization import from_utc_iso, to_utc_iso

_ELIGIBLE_SQL = (
    "((status = 'pending' AND COALESCE(next_attempt_at, due_at) <= ?) "
    "OR (status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?))"
)


class SqliteSchedulerRepository:
    """Durable scheduled jobs and notifications, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ job lifecycle

    async def schedule_or_replace(self, job: ScheduledJob) -> ScheduledJob:
        return await asyncio.to_thread(self._schedule_or_replace_sync, job)

    async def cancel_active_by_dedup_key(self, dedup_key: str, *, at: datetime) -> int:
        return await asyncio.to_thread(self._cancel_active_by_dedup_key_sync, dedup_key, at)

    async def get_job(self, job_id: ScheduledJobId) -> ScheduledJob | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    async def list_jobs(
        self,
        *,
        statuses: Sequence[ScheduledJobStatus] | None = None,
        limit: int | None = 20,
    ) -> list[ScheduledJob]:
        return await asyncio.to_thread(self._list_jobs_sync, tuple(statuses or ()), limit)

    async def next_wakeup_at(self) -> datetime | None:
        return await asyncio.to_thread(self._next_wakeup_at_sync)

    # ------------------------------------------------------------------------- claims

    async def claim_next(
        self,
        *,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ScheduledJobClaim | None:
        # Validate on the caller's thread so a programming error fails fast and loudly.
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if claim_token.int == 0:
            raise ValueError("claim_token must not be the nil UUID")
        _require_aware(now, "now")
        _require_aware(lease_expires_at, "lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be after now")
        return await asyncio.to_thread(
            self._claim_next_sync, worker_id, claim_token, now, lease_expires_at
        )

    async def complete_claim(
        self, job_id: ScheduledJobId, *, claim_token: UUID, completed_at: datetime
    ) -> ScheduledJob:
        return await asyncio.to_thread(
            self._complete_claim_sync, job_id, claim_token, completed_at
        )

    async def retry_claim(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
        next_attempt_at: datetime,
    ) -> ScheduledJob:
        return await asyncio.to_thread(
            self._retry_claim_sync,
            job_id,
            claim_token,
            failed_at,
            error,
            next_attempt_at,
        )

    async def dead_letter_claim(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> ScheduledJob:
        return await asyncio.to_thread(
            self._dead_letter_claim_sync, job_id, claim_token, failed_at, error
        )

    async def complete_claim_with_notification(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        completed_at: datetime,
        notification: Notification,
    ) -> tuple[ScheduledJob, Notification]:
        return await asyncio.to_thread(
            self._complete_claim_with_notification_sync,
            job_id,
            claim_token,
            completed_at,
            notification,
        )

    # ------------------------------------------------------------------ notifications

    async def create_notification_idempotent(self, notification: Notification) -> Notification:
        return await asyncio.to_thread(self._create_notification_sync, notification)

    async def get_notification(self, notification_id: NotificationId) -> Notification | None:
        return await asyncio.to_thread(self._get_notification_sync, notification_id)

    async def list_notifications(
        self, *, unread_only: bool = False, limit: int | None = 20
    ) -> list[Notification]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_notifications_sync, unread_only, limit)

    async def mark_notification_read(
        self, notification_id: NotificationId, *, at: datetime
    ) -> Notification:
        return await asyncio.to_thread(self._mark_notification_read_sync, notification_id, at)

    async def resolve_notification_id(self, reference: str) -> NotificationId:
        text = reference.strip().lower()
        if not text:
            raise NotificationNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        notifications = await self.list_notifications(limit=None)
        if candidate is not None:
            if all(item.id != candidate for item in notifications):
                raise NotificationNotFound(candidate)
            return candidate
        matching = [item.id for item in notifications if str(item.id).startswith(text)]
        if not matching:
            raise NotificationNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    # ------------------------------------------------------------ blocking internals

    def _schedule_or_replace_sync(self, job: ScheduledJob) -> ScheduledJob:
        with self._database.connect() as connection, transaction(connection):
            return upsert_job_for_dedup(connection, job)

    def _cancel_active_by_dedup_key_sync(self, dedup_key: str, at: datetime) -> int:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE scheduled_jobs SET status = 'cancelled', cancelled_at = ?, "
                "updated_at = ?, next_attempt_at = NULL, claim_token = NULL, claimed_by = NULL, "
                "claimed_at = NULL, lease_expires_at = NULL "
                "WHERE dedup_key = ? AND status IN ('pending', 'processing')",
                (to_utc_iso(at), to_utc_iso(at), dedup_key),
            )
            return int(cursor.rowcount)

    def _get_job_sync(self, job_id: ScheduledJobId) -> ScheduledJob | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {JOB_FIELDS} FROM scheduled_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        return None if row is None else row_to_job(row)

    def _list_jobs_sync(
        self, statuses: tuple[ScheduledJobStatus, ...], limit: int | None
    ) -> list[ScheduledJob]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = f"SELECT {JOB_FIELDS} FROM scheduled_jobs"
        parameters: list[object] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            statement += f" WHERE status IN ({placeholders})"
            parameters.extend(str(status) for status in statuses)
        statement += " ORDER BY COALESCE(next_attempt_at, due_at), created_at, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [row_to_job(row) for row in rows]

    def _next_wakeup_at_sync(self) -> datetime | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT MIN(candidate) AS wakeup FROM ("
                " SELECT COALESCE(next_attempt_at, due_at) AS candidate FROM scheduled_jobs"
                "  WHERE status = 'pending'"
                " UNION ALL"
                " SELECT lease_expires_at AS candidate FROM scheduled_jobs"
                "  WHERE status = 'processing' AND lease_expires_at IS NOT NULL"
                ")"
            ).fetchone()
        if row is None or row["wakeup"] is None:
            return None
        return from_utc_iso(str(row["wakeup"]))

    def _claim_next_sync(
        self,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ScheduledJobClaim | None:
        now_text = to_utc_iso(now)
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {JOB_FIELDS} FROM scheduled_jobs WHERE {_ELIGIBLE_SQL} "
                "ORDER BY COALESCE(next_attempt_at, due_at), created_at, id LIMIT 1",
                (now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            job = row_to_job(row)
            cursor = connection.execute(
                "UPDATE scheduled_jobs SET status = 'processing', attempts = attempts + 1, "
                "next_attempt_at = NULL, claim_token = ?, claimed_by = ?, claimed_at = ?, "
                "lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status IN ('pending', 'processing')",
                (
                    str(claim_token),
                    worker_id,
                    now_text,
                    to_utc_iso(lease_expires_at),
                    now_text,
                    str(job.id),
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - defensive: eligibility was just read
                raise CommitmentStoreError(f"scheduled job {job.id} moved while being claimed")
            claimed_row = connection.execute(
                f"SELECT {JOB_FIELDS} FROM scheduled_jobs WHERE id = ?", (str(job.id),)
            ).fetchone()
        if claimed_row is None:  # pragma: no cover - defensive
            raise CommitmentStoreError(f"scheduled job {job.id} disappeared while being claimed")
        return ScheduledJobClaim(
            job=row_to_job(claimed_row),
            claim_token=claim_token,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=lease_expires_at,
        )

    def _complete_claim_sync(
        self, job_id: ScheduledJobId, claim_token: UUID, completed_at: datetime
    ) -> ScheduledJob:
        with self._database.connect() as connection, transaction(connection):
            row = self._claim_row(connection, job_id, claim_token)
            job = row_to_job(row).complete(completed_at)
            self._write_completed(connection, job, completed_at=completed_at)
        return job

    def _retry_claim_sync(
        self,
        job_id: ScheduledJobId,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
        next_attempt_at: datetime,
    ) -> ScheduledJob:
        with self._database.connect() as connection, transaction(connection):
            row = self._claim_row(connection, job_id, claim_token)
            job = row_to_job(row).release_for_retry(
                next_attempt_at=next_attempt_at, error=error, at=failed_at
            )
            connection.execute(
                "UPDATE scheduled_jobs SET status = 'pending', next_attempt_at = ?, "
                "last_error = ?, updated_at = ?, claim_token = NULL, claimed_by = NULL, "
                "claimed_at = NULL, lease_expires_at = NULL WHERE id = ?",
                (
                    to_utc_iso(job.effective_due_at),
                    job.last_error,
                    to_utc_iso(job.updated_at),
                    str(job_id),
                ),
            )
        return job

    def _dead_letter_claim_sync(
        self,
        job_id: ScheduledJobId,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> ScheduledJob:
        with self._database.connect() as connection, transaction(connection):
            row = self._claim_row(connection, job_id, claim_token)
            job = row_to_job(row).dead_letter(error=error, at=failed_at)
            connection.execute(
                "UPDATE scheduled_jobs SET status = 'dead_lettered', completed_at = ?, "
                "next_attempt_at = NULL, last_error = ?, updated_at = ?, claim_token = NULL, "
                "claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL WHERE id = ?",
                (
                    to_utc_iso(failed_at),
                    job.last_error,
                    to_utc_iso(job.updated_at),
                    str(job_id),
                ),
            )
        return job

    def _complete_claim_with_notification_sync(
        self,
        job_id: ScheduledJobId,
        claim_token: UUID,
        completed_at: datetime,
        notification: Notification,
    ) -> tuple[ScheduledJob, Notification]:
        with self._database.connect() as connection, transaction(connection):
            row = self._claim_row(connection, job_id, claim_token)
            stored_notification = insert_notification_idempotent(connection, notification)
            job = row_to_job(row).complete(completed_at)
            self._write_completed(connection, job, completed_at=completed_at)
        return job, stored_notification

    def _claim_row(
        self, connection: sqlite3.Connection, job_id: ScheduledJobId, claim_token: UUID
    ) -> sqlite3.Row:
        """Read the job only if it is still processing under this exact claim."""
        row: sqlite3.Row | None = connection.execute(
            f"SELECT {JOB_FIELDS} FROM scheduled_jobs "
            "WHERE id = ? AND status = 'processing' AND claim_token = ?",
            (str(job_id), str(claim_token)),
        ).fetchone()
        if row is None:
            raise StaleScheduledJobClaim(job_id, claim_token)
        return row

    def _write_completed(
        self,
        connection: sqlite3.Connection,
        job: ScheduledJob,
        *,
        completed_at: datetime,
    ) -> None:
        cursor = connection.execute(
            "UPDATE scheduled_jobs SET status = 'completed', completed_at = ?, "
            "next_attempt_at = NULL, last_error = NULL, updated_at = ?, claim_token = NULL, "
            "claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL "
            "WHERE id = ? AND status = 'processing'",
            (to_utc_iso(completed_at), to_utc_iso(job.updated_at), str(job.id)),
        )
        if cursor.rowcount != 1:  # pragma: no cover - defensive: the claim was just verified
            raise CommitmentStoreError(f"scheduled job {job.id} was not completed atomically")

    def _create_notification_sync(self, notification: Notification) -> Notification:
        with self._database.connect() as connection, transaction(connection):
            return insert_notification_idempotent(connection, notification)

    def _get_notification_sync(self, notification_id: NotificationId) -> Notification | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {NOTIFICATION_FIELDS} FROM notifications WHERE id = ?",
                (str(notification_id),),
            ).fetchone()
        return None if row is None else row_to_notification(row)

    def _list_notifications_sync(
        self, unread_only: bool, limit: int | None
    ) -> list[Notification]:
        statement = f"SELECT {NOTIFICATION_FIELDS} FROM notifications"
        parameters: list[object] = []
        if unread_only:
            statement += " WHERE status = 'unread'"
        statement += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [row_to_notification(row) for row in rows]

    def _mark_notification_read_sync(
        self, notification_id: NotificationId, at: datetime
    ) -> Notification:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {NOTIFICATION_FIELDS} FROM notifications WHERE id = ?",
                (str(notification_id),),
            ).fetchone()
            if row is None:
                raise NotificationNotFound(notification_id)
            updated = row_to_notification(row).mark_read(at)
            if updated.read_at is not None:
                connection.execute(
                    "UPDATE notifications SET status = 'read', read_at = ? "
                    "WHERE id = ? AND status = 'unread'",
                    (to_utc_iso(updated.read_at), str(notification_id)),
                )
        return updated


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["SqliteSchedulerRepository"]
