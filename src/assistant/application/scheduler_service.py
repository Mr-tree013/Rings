"""The daemon scheduler: claim due jobs, run fixed handlers, finish under a fence (ADR-0016).

One call to `run_once` performs at most one attempt:

```text
claim_next ──► None ──► IDLE
    │ claim (attempts += 1, new claim token)
    ▼
dispatch by kind
    ├── DEADLINE_REMINDER ─► notification (or "obsolete, nothing to say")
    └── ROLLING_REPLAN    ─► new PENDING proposal (never applied) + PLAN_READY
    ├── success ─────────────────► complete [with notification] ──► COMPLETED
    ├── PermanentScheduledJobError ► dead letter ──► DEAD_LETTERED
    ├── StaleScheduledJobClaim ────► log, do nothing (someone else owns the job now)
    └── Exception ────────────────► retry with backoff, or dead letter when exhausted
```

The scheduler materializes nothing: reminders are created by task and deadline mutations, and
replan requests by commitment changes. This service only executes what is already durable, so
a restart resumes exactly where the database left off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from assistant.application.event_failures import format_event_failure
from assistant.application.planner_service import PlannerService
from assistant.application.planning_effort import compute_remaining_effort
from assistant.application.retry import RetryPolicy
from assistant.domain.errors import (
    InvalidScheduledJobPayload,
    PermanentScheduledJobError,
    StaleScheduledJobClaim,
)
from assistant.domain.notification import Notification, NotificationKind
from assistant.domain.planning import PlanningIssueCode
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobClaim,
    ScheduledJobKind,
)
from assistant.domain.scheduler_payloads import (
    DeadlineReminderPayload,
    deadline_reminder_notification_key,
    parse_deadline_reminder_payload,
    parse_rolling_replan_payload,
    plan_ready_notification_key,
    scheduler_warning_notification_key,
)
from assistant.domain.task import Task, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.interval_waiter import IntervalWaiter
from assistant.ports.scheduler_repository import SchedulerRepository
from assistant.ports.work_repository import WorkRepository

LOGGER = logging.getLogger("assistant.scheduler")

DEFAULT_LEASE_DURATION = timedelta(minutes=5)
"""How long a claim stays valid before another worker may recover the job."""

DEFAULT_POLL_INTERVAL = timedelta(seconds=15)
"""How long `run_forever` waits when there is nothing due."""

PLANNING_NOT_CONFIGURED_MESSAGE = (
    "Rolling replanning skipped because [planning] is not configured."
)


class SchedulerRunResult(StrEnum):
    """What one `run_once` call did."""

    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


class SchedulerService:
    """Executes durable scheduled jobs under fenced leases.

    Dependencies are injected, never read from globals: scheduler repository, commitment and
    work reads, planner service, clock, retry policy, worker identity, claim-token factory,
    lease duration, poll interval and interval waiter.
    """

    name = "scheduler"

    def __init__(
        self,
        scheduler: SchedulerRepository,
        commitments: CommitmentRepository,
        work: WorkRepository,
        planner: PlannerService,
        clock: Clock,
        policy: RetryPolicy,
        waiter: IntervalWaiter,
        *,
        worker_id: str = "scheduler",
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        claim_token_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if poll_interval < timedelta(0):
            raise ValueError("poll_interval must not be negative")
        self._scheduler = scheduler
        self._commitments = commitments
        self._work = work
        self._planner = planner
        self._clock = clock
        self._policy = policy
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._lease_duration = lease_duration
        self._claim_token_factory = claim_token_factory
        self._waiter = waiter

    @property
    def worker_id(self) -> str:
        """This worker's identity, as recorded on its claims."""
        return self._worker_id

    async def run_once(self) -> SchedulerRunResult:
        """Attempt at most one due job.

        `CancelledError` propagates untouched: the job stays `PROCESSING` under its current
        lease, and a later attempt recovers it once the lease expires. Cancellation is not a
        business failure.
        """
        now = self._clock.now()
        claim = await self._scheduler.claim_next(
            worker_id=self._worker_id,
            claim_token=self._claim_token_factory(),
            now=now,
            lease_expires_at=now + self._lease_duration,
        )
        if claim is None:
            return SchedulerRunResult.IDLE
        try:
            notification = await self._dispatch(claim.job)
            if notification is None:
                await self._scheduler.complete_claim(
                    claim.job.id,
                    claim_token=claim.claim_token,
                    completed_at=self._clock.now(),
                )
            else:
                await self._scheduler.complete_claim_with_notification(
                    claim.job.id,
                    claim_token=claim.claim_token,
                    completed_at=self._clock.now(),
                    notification=notification,
                )
        except asyncio.CancelledError:
            raise
        except StaleScheduledJobClaim:
            LOGGER.warning(
                "scheduled job %s is no longer ours (cancelled or reclaimed); skipping",
                claim.job.id,
            )
            return SchedulerRunResult.IDLE
        except (PermanentScheduledJobError, InvalidScheduledJobPayload) as exc:
            await self._dead_letter(claim, exc)
            return SchedulerRunResult.DEAD_LETTERED
        except Exception as exc:
            return await self._record_failure(claim, exc)
        return SchedulerRunResult.COMPLETED

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run due jobs until `stop_event` is set.

        Startup itself is recovery: the first `run_once` picks up whatever the database says is
        due, including jobs whose lease expired while the process was down. An idle cycle waits
        for the next wake-up or the poll interval, whichever comes first, so the loop never
        spins. Infrastructure errors are not swallowed here; restart policy belongs to the
        daemon supervisor.
        """
        while not stop_event.is_set():
            result = await self.run_once()
            if result is not SchedulerRunResult.IDLE:
                continue
            await self._wait_for_work(stop_event)

    # ----------------------------------------------------------------- handlers

    async def _dispatch(self, job: ScheduledJob) -> Notification | None:
        """Run the fixed handler for `job.kind`, returning a notification to store, if any."""
        if job.kind is ScheduledJobKind.DEADLINE_REMINDER:
            return await self._handle_deadline_reminder(job)
        if job.kind is ScheduledJobKind.ROLLING_REPLAN:
            return await self._handle_rolling_replan(job)
        raise PermanentScheduledJobError(  # pragma: no cover - kinds are fixed at the DB
            f"unknown scheduled job kind {job.kind!r}"
        )

    async def _handle_deadline_reminder(self, job: ScheduledJob) -> Notification | None:
        payload = parse_deadline_reminder_payload(job.payload_json)
        task = await self._commitments.get_task(payload.task_id)
        if task is None or task.status is not TaskStatus.OPEN:
            return None  # the task is gone or finished: nothing to remind about
        deadline = await self._commitments.get_deadline(payload.task_id)
        if (
            deadline is None
            or deadline.id != payload.deadline_id
            or deadline.due_at != payload.deadline_due_at
        ):
            return None  # the deadline moved or was cleared since this job was created
        return await self._deadline_notification(job, task, payload)

    async def _handle_rolling_replan(self, job: ScheduledJob) -> Notification | None:
        parse_rolling_replan_payload(job.payload_json)
        if self._planner.config is None:
            return self._warning_notification(job, PLANNING_NOT_CONFIGURED_MESSAGE)
        window = self._planner.week_window()
        existing = await self._planner.latest_pending_detail(window)
        # Nothing that can change the plan moved since the pending proposal was built, so
        # re-planning would only produce the same plan again.
        unchanged = (
            existing is not None
            and await self._planner.current_revision() == existing.proposal.input_revision
        )
        if unchanged:
            return None
        detail = await self._planner.create_proposal(window)
        return Notification(
            kind=NotificationKind.PLAN_READY,
            title="Updated plan proposal is ready",
            body=(
                f"Proposal {detail.proposal.id} was generated after your commitments changed."
                f"\nReview with: pw plan show {detail.proposal.id}"
            ),
            dedup_key=plan_ready_notification_key(job.id),
            related_proposal_id=detail.proposal.id,
            created_at=self._clock.now(),
        )

    async def _deadline_notification(
        self,
        job: ScheduledJob,
        task: Task,
        payload: DeadlineReminderPayload,
    ) -> Notification:
        actual_seconds = await self._work.total_work_seconds(task.id)
        effort = compute_remaining_effort(
            task_id=task.id,
            estimated_minutes=task.estimated_minutes,
            actual_seconds=actual_seconds,
        )
        if effort.issue is not None and effort.issue.code is PlanningIssueCode.MISSING_ESTIMATE:
            remaining_line = "Remaining work estimate is not set."
        elif effort.issue is not None and (
            effort.issue.code is PlanningIssueCode.ESTIMATE_EXHAUSTED
        ):
            remaining_line = (
                "Recorded work has reached the current estimate; task is still open."
            )
        else:
            remaining_line = f"Estimated remaining work: {effort.remaining_minutes} minutes"
        return Notification(
            kind=NotificationKind.DEADLINE_REMINDER,
            title=f"Deadline approaching: {task.title}",
            body=f"Due: {self._display(payload.deadline_due_at)}\n{remaining_line}",
            dedup_key=deadline_reminder_notification_key(job.id),
            related_task_id=task.id,
            created_at=self._clock.now(),
        )

    def _warning_notification(self, job: ScheduledJob, message: str) -> Notification:
        return Notification(
            kind=NotificationKind.SCHEDULER_WARNING,
            title="Scheduler warning",
            body=message,
            dedup_key=scheduler_warning_notification_key(job.id),
            created_at=self._clock.now(),
        )

    def _display(self, moment: datetime) -> str:
        """Render an instant in the configured planning timezone, never in machine local time."""
        config = self._planner.config
        if config is None:
            return moment.isoformat()
        return moment.astimezone(ZoneInfo(config.timezone)).isoformat(timespec="seconds")

    # ------------------------------------------------------------------ failures

    async def _dead_letter(self, claim: ScheduledJobClaim, error: Exception) -> None:
        message = format_event_failure(error)
        try:
            await self._scheduler.dead_letter_claim(
                claim.job.id,
                claim_token=claim.claim_token,
                failed_at=self._clock.now(),
                error=message,
            )
        except StaleScheduledJobClaim:  # pragma: no cover - race with a cancellation
            LOGGER.warning("scheduled job %s lost its claim while failing", claim.job.id)
        else:
            LOGGER.error("scheduled job %s dead-lettered: %s", claim.job.id, message)

    async def _record_failure(
        self, claim: ScheduledJobClaim, error: Exception
    ) -> SchedulerRunResult:
        failed_at = self._clock.now()
        message = format_event_failure(error)
        try:
            if claim.job.attempts >= self._policy.max_attempts:
                await self._scheduler.dead_letter_claim(
                    claim.job.id,
                    claim_token=claim.claim_token,
                    failed_at=failed_at,
                    error=message,
                )
                LOGGER.error(
                    "scheduled job %s dead-lettered after %d attempts: %s",
                    claim.job.id,
                    claim.job.attempts,
                    message,
                )
                return SchedulerRunResult.DEAD_LETTERED
            await self._scheduler.retry_claim(
                claim.job.id,
                claim_token=claim.claim_token,
                failed_at=failed_at,
                error=message,
                next_attempt_at=self._policy.next_attempt_at(
                    attempts=claim.job.attempts, now=failed_at
                ),
            )
        except StaleScheduledJobClaim:
            LOGGER.warning("scheduled job %s lost its claim while failing", claim.job.id)
            return SchedulerRunResult.IDLE
        LOGGER.warning(
            "scheduled job %s failed (attempt %d): %s",
            claim.job.id,
            claim.job.attempts,
            message,
        )
        return SchedulerRunResult.RETRY_SCHEDULED

    async def _wait_for_work(self, stop_event: asyncio.Event) -> None:
        """Wait for the next wake-up, capped by the poll interval."""
        delay = self._poll_interval.total_seconds()
        wakeup = await self._scheduler.next_wakeup_at()
        if wakeup is not None:
            delay = min(delay, max((wakeup - self._clock.now()).total_seconds(), 0.0))
        if delay <= 0:
            return
        await self._waiter.wait(delay, stop_event)


__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_POLL_INTERVAL",
    "PLANNING_NOT_CONFIGURED_MESSAGE",
    "SchedulerRunResult",
    "SchedulerService",
]
