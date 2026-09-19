"""Unit tests for EventWorker control flow (fake repository, fake handler).

These tests pin down the worker's decisions: idle, complete, retry, dead letter,
cancellation and error propagation. Claiming, leases and fencing under real concurrency
are proven separately against SQLite in tests/integration/test_event_worker_integration.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.retry import RetryPolicy
from assistant.domain.inbound_event import EventStatus, InboundEvent
from assistant.store.errors import StoreError
from tests.support.fakes import FakeClock, FakeEventRepository, ScriptedHandler, make_event

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)


class BrokenRepository:
    """Repository whose claim path fails, to prove the worker does not swallow it."""

    async def claim_next(self, **_kwargs: object) -> None:
        raise StoreError("database is on fire")

    async def complete_claim(self, *_args: object, **_kwargs: object) -> InboundEvent:
        raise AssertionError("unreachable")

    async def fail_claim(self, *_args: object, **_kwargs: object) -> InboundEvent:
        raise AssertionError("unreachable")


class TokenFactory:
    """Deterministic claim tokens: 1, 2, 3, ..."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


def _worker(
    repository: FakeEventRepository,
    handler: ScriptedHandler,
    clock: FakeClock,
    *,
    policy: RetryPolicy | None = None,
    worker_id: str = "worker-a",
    poll_interval: timedelta = timedelta(milliseconds=10),
) -> EventWorker:
    return EventWorker(
        repository,
        handler,
        clock,
        policy if policy is not None else RetryPolicy(),
        worker_id=worker_id,
        claim_token_factory=TokenFactory(),
        lease_duration=LEASE,
        poll_interval=poll_interval,
    )


async def test_run_once_is_idle_without_work() -> None:
    repository = FakeEventRepository()
    handler = ScriptedHandler()

    result = await _worker(repository, handler, FakeClock(start=START)).run_once()

    assert result is WorkerResult.IDLE
    assert handler.handled == []
    assert repository.list_pending_calls == 0


async def test_successful_processing_completes_the_event() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="1")
    await repository.add(event)
    handler = ScriptedHandler(["ok"])

    result = await _worker(repository, handler, FakeClock(start=START)).run_once()

    stored = await repository.get(event.id)
    assert result is WorkerResult.PROCESSED
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.attempts == 1
    assert stored.last_error is None
    assert [handled.id for handled in handler.handled] == [event.id]
    assert handler.handled[0].status is EventStatus.PROCESSING


async def test_retryable_failure_schedules_a_retry() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="2")
    await repository.add(event)
    policy = RetryPolicy(max_attempts=5, base_delay=timedelta(seconds=30))

    result = await _worker(
        repository, ScriptedHandler(["fail"]), FakeClock(start=START), policy=policy
    ).run_once()

    stored = await repository.get(event.id)
    assert result is WorkerResult.RETRY_SCHEDULED
    assert stored is not None
    assert stored.status is EventStatus.FAILED
    assert stored.last_error == "RuntimeError: boom"
    assert stored.next_attempt_at == START + timedelta(seconds=30)
    assert stored.attempts == 1


async def test_a_retry_is_not_claimable_before_it_is_due() -> None:
    repository = FakeEventRepository()
    await repository.add(make_event(source="smail", external_id="3"))
    clock = FakeClock(start=START)
    policy = RetryPolicy(base_delay=timedelta(seconds=30))
    worker = _worker(repository, ScriptedHandler(["fail"]), clock, policy=policy)

    await worker.run_once()

    assert await worker.run_once() is WorkerResult.IDLE
    clock.advance(29)
    assert await worker.run_once() is WorkerResult.IDLE


async def test_a_retry_runs_once_it_is_due() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="4")
    await repository.add(event)
    clock = FakeClock(start=START)
    handler = ScriptedHandler(["fail", "ok"])
    worker = _worker(
        repository, handler, clock, policy=RetryPolicy(base_delay=timedelta(seconds=30))
    )

    assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
    clock.advance(30)
    assert await worker.run_once() is WorkerResult.PROCESSED

    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.attempts == 2


async def test_repeated_failures_back_off_exponentially_then_cap() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="5")
    await repository.add(event)
    clock = FakeClock(start=START)
    policy = RetryPolicy(
        max_attempts=10,
        base_delay=timedelta(seconds=10),
        max_delay=timedelta(seconds=25),
    )
    worker = _worker(repository, ScriptedHandler(["fail", "fail", "fail"]), clock, policy=policy)

    delays: list[timedelta] = []
    for _ in range(3):
        assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
        stored = await repository.get(event.id)
        assert stored is not None
        assert stored.next_attempt_at is not None
        delays.append(stored.next_attempt_at - clock.now())
        clock.advance(int(delays[-1].total_seconds()))

    assert delays == [
        timedelta(seconds=10),
        timedelta(seconds=20),
        timedelta(seconds=25),
    ]


async def test_exhausted_attempts_dead_letter_the_event() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="6")
    await repository.add(event)
    clock = FakeClock(start=START)
    policy = RetryPolicy(max_attempts=2, base_delay=timedelta(seconds=1))
    worker = _worker(repository, ScriptedHandler(["fail", "fail", "ok"]), clock, policy=policy)

    assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
    clock.advance(1)
    assert await worker.run_once() is WorkerResult.DEAD_LETTERED

    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.DEAD_LETTERED
    assert stored.dead_lettered_at == clock.now()
    assert stored.next_attempt_at is None
    assert stored.attempts == 2
    assert await worker.run_once() is WorkerResult.IDLE


async def test_permanent_failure_dead_letters_immediately() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="7")
    await repository.add(event)
    clock = FakeClock(start=START)
    policy = RetryPolicy(max_attempts=5, base_delay=timedelta(seconds=30))
    worker = _worker(repository, ScriptedHandler(["permanent"]), clock, policy=policy)

    result = await worker.run_once()

    stored = await repository.get(event.id)
    assert result is WorkerResult.DEAD_LETTERED
    assert stored is not None
    assert stored.status is EventStatus.DEAD_LETTERED
    assert stored.last_error == "PermanentEventError: cannot ever work"
    assert stored.dead_lettered_at == START
    assert stored.attempts == 1


async def test_cancellation_propagates_and_leaves_the_event_processing() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="8")
    await repository.add(event)
    handler = ScriptedHandler(["block"])
    worker = _worker(repository, handler, FakeClock(start=START))

    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING
    assert stored.attempts == 1
    assert stored.last_error is None
    assert stored.next_attempt_at is None


async def test_a_cancelled_event_is_recovered_once_its_lease_expires() -> None:
    repository = FakeEventRepository()
    event = make_event(source="smail", external_id="9")
    await repository.add(event)
    clock = FakeClock(start=START)
    blocked_handler = ScriptedHandler(["block"])
    blocked = _worker(repository, blocked_handler, clock, worker_id="worker-a")

    task = asyncio.create_task(blocked.run_once())
    await asyncio.wait_for(blocked_handler.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    recovery = _worker(repository, ScriptedHandler(["ok"]), clock, worker_id="worker-b")
    assert await recovery.run_once() is WorkerResult.IDLE  # the lease is still live

    clock.advance(int(LEASE.total_seconds()))
    assert await recovery.run_once() is WorkerResult.PROCESSED

    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.PROCESSED
    assert stored.attempts == 2


async def test_worker_never_claims_through_list_pending() -> None:
    repository = FakeEventRepository()
    await repository.add(make_event(source="cli"))
    worker = _worker(repository, ScriptedHandler(["ok", "ok"]), FakeClock(start=START))

    await worker.run_once()
    await worker.run_once()

    assert repository.claim_calls == 2
    assert repository.list_pending_calls == 0


async def test_run_forever_waits_between_poll_cycles_and_stops_promptly() -> None:
    repository = FakeEventRepository()
    await repository.add(make_event(source="cli"))
    worker = _worker(
        repository,
        ScriptedHandler(["ok"]),
        FakeClock(start=START),
        poll_interval=timedelta(milliseconds=50),
    )
    stop_event = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    # One productive cycle plus a couple of idle polls: the loop waits, it does not spin.
    assert repository.claim_calls <= 5


async def test_run_forever_propagates_repository_failures() -> None:
    worker = EventWorker(
        BrokenRepository(),
        ScriptedHandler(),
        FakeClock(start=START),
        RetryPolicy(),
        worker_id="worker-a",
    )

    with pytest.raises(StoreError, match="on fire"):
        await worker.run_forever(asyncio.Event())


def test_rejects_a_blank_worker_id() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        EventWorker(
            FakeEventRepository(),
            ScriptedHandler(),
            FakeClock(start=START),
            RetryPolicy(),
            worker_id="   ",
        )


def test_rejects_a_non_positive_lease() -> None:
    with pytest.raises(ValueError, match="lease_duration"):
        EventWorker(
            FakeEventRepository(),
            ScriptedHandler(),
            FakeClock(start=START),
            RetryPolicy(),
            worker_id="worker-a",
            lease_duration=timedelta(0),
        )


def test_rejects_a_negative_poll_interval() -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        EventWorker(
            FakeEventRepository(),
            ScriptedHandler(),
            FakeClock(start=START),
            RetryPolicy(),
            worker_id="worker-a",
            poll_interval=timedelta(seconds=-1),
        )
