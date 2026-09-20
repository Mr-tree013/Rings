"""ScheduledJob: durable scheduling intent the daemon executes later (ADR-0016).

Process is ephemeral; scheduling intent is durable. A `ScheduledJob` says *when* one of a
small, fixed set of jobs becomes due, and it lives in the runtime database rather than in an
`asyncio` timer, so a restart resumes the schedule instead of dropping it.

A job is not a task, not a plan block and not an inbound event. It carries a typed JSON
payload (never code, never pickle) that names authoritative rows; the handler re-reads those
rows when the job runs and decides whether the intent is still current.

Execution is at-least-once: a claim grants the holder the *current* right to run a job until
its lease expires, fenced by a random claim token. When the lease expires another worker may
reclaim the job with a new token, and the old holder's completion is rejected
(`StaleScheduledJobClaim`) instead of overwriting the newer attempt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from assistant.domain.errors import (
    InvalidScheduledJob,
    InvalidScheduledJobClaim,
    InvalidScheduledJobPayload,
)

ScheduledJobId = UUID
"""Stable identity of one scheduled job."""


def new_scheduled_job_id() -> ScheduledJobId:
    """Generate a fresh scheduled job identity."""
    return uuid4()


class ScheduledJobKind(StrEnum):
    """The fixed set of jobs the scheduler knows how to run."""

    DEADLINE_REMINDER = "deadline_reminder"
    ROLLING_REPLAN = "rolling_replan"


class ScheduledJobStatus(StrEnum):
    """Where a job is in its lifecycle.

    There is deliberately no persistent `FAILED`: a retryable failure returns the job to
    `PENDING` with a new `next_attempt_at`, because the job already owns an explicit due time.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


ACTIVE_JOB_STATUSES: Final[tuple[ScheduledJobStatus, ...]] = (
    ScheduledJobStatus.PENDING,
    ScheduledJobStatus.PROCESSING,
)
"""Statuses that still own their dedup key and may still run."""

TERMINAL_JOB_STATUSES: Final[tuple[ScheduledJobStatus, ...]] = (
    ScheduledJobStatus.COMPLETED,
    ScheduledJobStatus.CANCELLED,
    ScheduledJobStatus.DEAD_LETTERED,
)
"""Statuses a job never leaves."""


def canonical_payload_json(payload: dict[str, object]) -> str:
    """Serialize a job payload canonically: sorted keys, compact separators, UTF-8 text.

    Canonical form is what makes a payload comparable and a dedup key stable.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidScheduledJob(f"{field_name} must be timezone-aware")


def _validate_payload(payload_json: str) -> None:
    try:
        decoded = json.loads(payload_json)
    except ValueError as exc:
        raise InvalidScheduledJobPayload(f"job payload is not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise InvalidScheduledJobPayload("a job payload must be a JSON object")
    if canonical_payload_json(decoded) != payload_json:
        raise InvalidScheduledJobPayload(
            "a job payload must be stored in canonical form (sorted keys, compact separators)"
        )


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One durable intent to run a fixed kind of work at (or after) a time."""

    kind: ScheduledJobKind
    due_at: datetime
    dedup_key: str
    payload_json: str
    created_at: datetime
    updated_at: datetime
    id: ScheduledJobId = field(default_factory=new_scheduled_job_id)
    status: ScheduledJobStatus = ScheduledJobStatus.PENDING
    next_attempt_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    claim_token: UUID | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.due_at, "due_at")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.next_attempt_at is not None:
            _require_aware(self.next_attempt_at, "next_attempt_at")
        if not self.dedup_key.strip():
            raise InvalidScheduledJob("a scheduled job needs a non-blank dedup key")
        _validate_payload(self.payload_json)
        if self.attempts < 0:
            raise InvalidScheduledJob("attempts must not be negative")
        self._validate_lease()
        self._validate_status_timestamps()

    def _validate_lease(self) -> None:
        lease = (
            self.claim_token,
            self.claimed_by,
            self.claimed_at,
            self.lease_expires_at,
        )
        present = [value is not None for value in lease]
        if any(present) and not all(present):
            raise InvalidScheduledJob("a claim is either fully present or fully absent")
        if self.claim_token is not None and self.claim_token.int == 0:
            raise InvalidScheduledJob("claim_token must not be the nil UUID")
        if self.claimed_by is not None and not self.claimed_by.strip():
            raise InvalidScheduledJob("claimed_by must not be blank")
        if self.claimed_at is not None and self.lease_expires_at is not None:
            _require_aware(self.claimed_at, "claimed_at")
            _require_aware(self.lease_expires_at, "lease_expires_at")
            if self.lease_expires_at <= self.claimed_at:
                raise InvalidScheduledJob("lease_expires_at must be after claimed_at")
        if (self.status is ScheduledJobStatus.PROCESSING) != (self.claim_token is not None):
            raise InvalidScheduledJob("only a PROCESSING job may hold a claim")
        if self.status is ScheduledJobStatus.PROCESSING and self.next_attempt_at is not None:
            raise InvalidScheduledJob("a PROCESSING job has no next_attempt_at")

    def _validate_status_timestamps(self) -> None:
        if self.status in (
            ScheduledJobStatus.COMPLETED,
            ScheduledJobStatus.DEAD_LETTERED,
        ):
            if self.completed_at is None or self.cancelled_at is not None:
                raise InvalidScheduledJob(
                    f"a {self.status} job needs completed_at and no cancelled_at"
                )
        elif self.status is ScheduledJobStatus.CANCELLED:
            if self.cancelled_at is None or self.completed_at is not None:
                raise InvalidScheduledJob(
                    "a CANCELLED job needs cancelled_at and no completed_at"
                )
        elif self.completed_at is not None or self.cancelled_at is not None:
            raise InvalidScheduledJob(f"a {self.status} job must not carry terminal timestamps")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
        if self.cancelled_at is not None:
            _require_aware(self.cancelled_at, "cancelled_at")

    @property
    def is_active(self) -> bool:
        """Whether the job still owns its dedup key and may still run."""
        return self.status in ACTIVE_JOB_STATUSES

    @property
    def effective_due_at(self) -> datetime:
        """When the job becomes eligible: its retry time, or its original due time."""
        return self.due_at if self.next_attempt_at is None else self.next_attempt_at

    def is_lease_expired(self, now: datetime) -> bool:
        """Return whether `now` is at or past the lease expiry of a claimed job."""
        _require_aware(now, "now")
        return self.lease_expires_at is not None and now >= self.lease_expires_at

    # ------------------------------------------------------------------ transitions

    def claim(
        self,
        *,
        worker_id: str,
        claim_token: UUID,
        at: datetime,
        lease_expires_at: datetime,
    ) -> ScheduledJob:
        """Take (or retake) the lease, counting one more attempt."""
        if self.status not in ACTIVE_JOB_STATUSES:
            raise InvalidScheduledJob(f"a {self.status} job cannot be claimed")
        if not worker_id.strip():
            raise InvalidScheduledJob("worker_id must not be blank")
        _require_aware(at, "at")
        _require_aware(lease_expires_at, "lease_expires_at")
        if lease_expires_at <= at:
            raise InvalidScheduledJob("lease_expires_at must be after the claim time")
        return replace(
            self,
            status=ScheduledJobStatus.PROCESSING,
            attempts=self.attempts + 1,
            next_attempt_at=None,
            updated_at=at,
            claim_token=claim_token,
            claimed_by=worker_id,
            claimed_at=at,
            lease_expires_at=lease_expires_at,
        )

    def complete(self, at: datetime) -> ScheduledJob:
        """Mark the job done and release its lease."""
        _require_aware(at, "at")
        if self.status is not ScheduledJobStatus.PROCESSING:
            raise InvalidScheduledJob(f"a {self.status} job cannot be completed")
        return replace(
            self,
            status=ScheduledJobStatus.COMPLETED,
            updated_at=at,
            completed_at=at,
            next_attempt_at=None,
            last_error=None,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def release_for_retry(
        self, *, next_attempt_at: datetime, error: str, at: datetime
    ) -> ScheduledJob:
        """Return the job to PENDING with its next attempt time."""
        _require_aware(next_attempt_at, "next_attempt_at")
        _require_aware(at, "at")
        if self.status is not ScheduledJobStatus.PROCESSING:
            raise InvalidScheduledJob(f"a {self.status} job cannot be retried")
        return replace(
            self,
            status=ScheduledJobStatus.PENDING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            last_error=error,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def dead_letter(self, *, error: str, at: datetime) -> ScheduledJob:
        """Give up on the job, keeping its error for inspection."""
        _require_aware(at, "at")
        if self.status is not ScheduledJobStatus.PROCESSING:
            raise InvalidScheduledJob(f"a {self.status} job cannot be dead-lettered")
        return replace(
            self,
            status=ScheduledJobStatus.DEAD_LETTERED,
            updated_at=at,
            completed_at=at,
            next_attempt_at=None,
            last_error=error,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def cancel(self, at: datetime) -> ScheduledJob:
        """Cancel an active job: its intent is obsolete."""
        _require_aware(at, "at")
        if self.status not in ACTIVE_JOB_STATUSES:
            raise InvalidScheduledJob(f"a {self.status} job cannot be cancelled")
        return replace(
            self,
            status=ScheduledJobStatus.CANCELLED,
            updated_at=at,
            cancelled_at=at,
            next_attempt_at=None,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def reschedule(self, *, due_at: datetime, payload_json: str, at: datetime) -> ScheduledJob:
        """Push a PENDING job's due time (and payload) forward in place."""
        _require_aware(due_at, "due_at")
        _require_aware(at, "at")
        if self.status is not ScheduledJobStatus.PENDING:
            raise InvalidScheduledJob(f"a {self.status} job cannot be rescheduled in place")
        _validate_payload(payload_json)
        return replace(
            self,
            due_at=due_at,
            payload_json=payload_json,
            updated_at=at,
            next_attempt_at=None,
        )


@dataclass(frozen=True, slots=True)
class ScheduledJobClaim:
    """The right to run one scheduled job until `lease_expires_at`."""

    job: ScheduledJob
    claim_token: UUID
    claimed_by: str
    claimed_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if self.claim_token.int == 0:
            raise InvalidScheduledJobClaim("claim_token must not be the nil UUID")
        if not self.claimed_by.strip():
            raise InvalidScheduledJobClaim("claimed_by must not be empty")
        _require_aware(self.claimed_at, "claimed_at")
        _require_aware(self.lease_expires_at, "lease_expires_at")
        if self.lease_expires_at <= self.claimed_at:
            raise InvalidScheduledJobClaim("lease_expires_at must be after claimed_at")
        if self.job.status is not ScheduledJobStatus.PROCESSING:
            raise InvalidScheduledJobClaim(
                f"a claim must reference a PROCESSING job, not {self.job.status}"
            )

    def is_lease_expired(self, now: datetime) -> bool:
        """Return whether `now` is at or past the lease expiry."""
        _require_aware(now, "now")
        return now >= self.lease_expires_at


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "ScheduledJob",
    "ScheduledJobClaim",
    "ScheduledJobId",
    "ScheduledJobKind",
    "ScheduledJobStatus",
    "canonical_payload_json",
    "new_scheduled_job_id",
]
