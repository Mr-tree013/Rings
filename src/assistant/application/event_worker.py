"""The durable event worker: claim, handle, complete, retry, dead-letter (ADR-0010).

One call to `run_once` performs exactly one attempt:

```text
claim_next ──► None ──► IDLE
    │ claim
    ▼
handler.handle(event)
    ├── success ─────────────► complete_claim ──► PROCESSED
    ├── PermanentEventError ─► fail_claim(dead_letter=True) ──► DEAD_LETTERED
    └── Exception ───────────► attempts exhausted ? dead letter : schedule retry
```

What this class deliberately does not do: swallow infrastructure errors (a supervisor's
job), convert cancellation into a failure (a crash-recovery hazard), or loop internally
(that is `run_forever`, driven by the caller's stop event).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.application.event_failures import format_event_failure
from assistant.application.retry import RetryPolicy
from assistant.domain.errors import PermanentEventError
from assistant.domain.inbound_event import InboundEvent
from assistant.ports.clock import Clock
from assistant.ports.event_handler import EventHandler
from assistant.ports.event_repository import EventRepository

DEFAULT_LEASE_DURATION = timedelta(minutes=5)
"""How long a claim stays valid before another worker may recover the event."""

DEFAULT_POLL_INTERVAL = timedelta(seconds=5)
"""How long `run_forever` waits when there is nothing to do."""


class WorkerResult(StrEnum):
    """What one `run_once` call did."""

    IDLE = "idle"
    PROCESSED = "processed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


class EventWorker:
    """Processes durable events under fenced leases.

    Dependencies are injected, never read from globals: repository, handler, clock, retry
    policy, worker identity, claim-token factory, lease duration and poll interval.
    """

    def __init__(
        self,
        repository: EventRepository,
        handler: EventHandler,
        clock: Clock,
        policy: RetryPolicy,
        *,
        worker_id: str,
        claim_token_factory: Callable[[], UUID] = uuid4,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if poll_interval < timedelta(0):
            raise ValueError("poll_interval must not be negative")
        self._repository = repository
        self._handler = handler
        self._clock = clock
        self._policy = policy
        self._worker_id = worker_id
        self._claim_token_factory = claim_token_factory
        self._lease_duration = lease_duration
        self._poll_interval = poll_interval

    @property
    def worker_id(self) -> str:
        """This worker's identity, as recorded on its claims."""
        return self._worker_id

    async def run_once(self) -> WorkerResult:
        """Attempt a single event, if one is eligible.

        `CancelledError` raised by the handler propagates untouched: the event stays
        `PROCESSING` under its current lease, and a later attempt recovers it once the
        lease expires. Cancellation is not a business failure.
        """
        now = self._clock.now()
        claim = await self._repository.claim_next(
            worker_id=self._worker_id,
            claim_token=self._claim_token_factory(),
            now=now,
            lease_expires_at=now + self._lease_duration,
        )
        if claim is None:
            return WorkerResult.IDLE
        try:
            await self._handler.handle(claim.event)
        except asyncio.CancelledError:
            raise
        except PermanentEventError as exc:
            await self._repository.fail_claim(
                claim.event.id,
                claim_token=claim.claim_token,
                failed_at=self._clock.now(),
                error=format_event_failure(exc),
                next_attempt_at=None,
                dead_letter=True,
            )
            return WorkerResult.DEAD_LETTERED
        except Exception as exc:
            return await self._record_failure(claim.event, claim.claim_token, exc)
        await self._repository.complete_claim(
            claim.event.id,
            claim_token=claim.claim_token,
            completed_at=self._clock.now(),
        )
        return WorkerResult.PROCESSED

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Process events until `stop_event` is set.

        An idle cycle waits for the poll interval or the stop event, whichever comes first,
        so the loop never spins. Infrastructure errors from the repository or a handler are
        not swallowed here; restart policy belongs to the daemon supervisor.
        """
        while not stop_event.is_set():
            result = await self.run_once()
            if result is WorkerResult.IDLE:
                await self._wait_for_work(stop_event)

    async def _record_failure(
        self, event: InboundEvent, claim_token: UUID, error: Exception
    ) -> WorkerResult:
        failed_at = self._clock.now()
        message = format_event_failure(error)
        if event.attempts >= self._policy.max_attempts:
            await self._repository.fail_claim(
                event.id,
                claim_token=claim_token,
                failed_at=failed_at,
                error=message,
                next_attempt_at=None,
                dead_letter=True,
            )
            return WorkerResult.DEAD_LETTERED
        await self._repository.fail_claim(
            event.id,
            claim_token=claim_token,
            failed_at=failed_at,
            error=message,
            next_attempt_at=self._policy.next_attempt_at(attempts=event.attempts, now=failed_at),
            dead_letter=False,
        )
        return WorkerResult.RETRY_SCHEDULED

    async def _wait_for_work(self, stop_event: asyncio.Event) -> None:
        """Sleep for the poll interval, waking immediately when asked to stop."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self._poll_interval.total_seconds()):
                await stop_event.wait()


__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_POLL_INTERVAL",
    "EventWorker",
    "WorkerResult",
]

