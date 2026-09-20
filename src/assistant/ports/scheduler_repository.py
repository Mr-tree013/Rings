"""SchedulerRepository port: durable jobs, fenced claims and the notification inbox (ADR-0016).

This port exists because scheduling has its own transaction vocabulary: a claim is a
compare-and-set, a completion is fenced by the claim token, and a reminder must be able to
write its notification and finish its job in *one* transaction. None of that belongs in the
commitment port, and none of it is generic CRUD.

Two invariants are worth naming:

- one dedup key has at most one active job, enforced by a partial unique index;
- one notification dedup key has at most one row, enforced by a unique constraint, so a job
  that is re-executed after a crash finds the message it already produced.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from assistant.domain.notification import Notification, NotificationId
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobClaim,
    ScheduledJobId,
    ScheduledJobStatus,
)


class SchedulerRepository(Protocol):
    """Durable scheduled jobs, their claims, and the notification inbox."""

    # ------------------------------------------------------------------ job lifecycle

    async def schedule_or_replace(self, job: ScheduledJob) -> ScheduledJob:
        """Store `job`, coalescing with the active job that owns its dedup key.

        An active pending job is pushed to the new due time (debounce); an active processing
        job is left alone, because its handler reads authoritative state when it runs.
        """
        ...

    async def cancel_active_by_dedup_key(self, dedup_key: str, *, at: datetime) -> int:
        """Cancel the active job with this dedup key, returning how many rows changed."""
        ...

    async def get_job(self, job_id: ScheduledJobId) -> ScheduledJob | None:
        """Return one job, or `None`."""
        ...

    async def list_jobs(
        self,
        *,
        statuses: Sequence[ScheduledJobStatus] | None = None,
        limit: int | None = 20,
    ) -> list[ScheduledJob]:
        """List jobs ordered by effective due time, then creation, then id."""
        ...

    async def next_wakeup_at(self) -> datetime | None:
        """When the scheduler next has something to do, or `None` when nothing is pending."""
        ...

    # ------------------------------------------------------------------------- claims

    async def claim_next(
        self,
        *,
        worker_id: str,
        claim_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ScheduledJobClaim | None:
        """Claim the earliest eligible job, or return `None` when there is nothing to do.

        Eligible means: `PENDING` and due (`next_attempt_at` or `due_at`), or `PROCESSING`
        with an expired lease. Eligibility and the update happen in one write transaction, so
        two workers can never claim the same job.
        """
        ...

    async def complete_claim(
        self, job_id: ScheduledJobId, *, claim_token: UUID, completed_at: datetime
    ) -> ScheduledJob:
        """Mark a claimed job `COMPLETED`.

        Raises:
            StaleScheduledJobClaim: the job is not `PROCESSING` under this token any more.
        """
        ...

    async def retry_claim(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
        next_attempt_at: datetime,
    ) -> ScheduledJob:
        """Return a claimed job to `PENDING` with its next attempt time."""
        ...

    async def dead_letter_claim(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        failed_at: datetime,
        error: str,
    ) -> ScheduledJob:
        """Give up on a claimed job."""
        ...

    async def complete_claim_with_notification(
        self,
        job_id: ScheduledJobId,
        *,
        claim_token: UUID,
        completed_at: datetime,
        notification: Notification,
    ) -> tuple[ScheduledJob, Notification]:
        """Write the notification and finish the job in one transaction.

        The notification is inserted idempotently (its dedup key is unique), so a job that
        crashed after writing it still finishes cleanly when it runs again.

        Raises:
            StaleScheduledJobClaim: the job is not `PROCESSING` under this token any more;
                the notification is rolled back with it.
        """
        ...

    # ------------------------------------------------------------------ notifications

    async def create_notification_idempotent(self, notification: Notification) -> Notification:
        """Insert the notification, or return the existing one with the same dedup key."""
        ...

    async def get_notification(self, notification_id: NotificationId) -> Notification | None:
        """Return one notification, or `None`."""
        ...

    async def list_notifications(
        self,
        *,
        unread_only: bool = False,
        limit: int | None = 20,
    ) -> list[Notification]:
        """List notifications, newest first."""
        ...

    async def mark_notification_read(
        self, notification_id: NotificationId, *, at: datetime
    ) -> Notification:
        """Mark a notification read. Reading an already-read one is a no-op.

        Raises:
            NotificationNotFound: no such notification.
        """
        ...

    async def resolve_notification_id(self, reference: str) -> NotificationId:
        """Resolve a full UUID or a unique prefix to a notification id.

        Raises:
            NotificationNotFound: nothing matches.
            AmbiguousId: several notifications match; the CLI never guesses.
        """
        ...


__all__ = ["SchedulerRepository"]
