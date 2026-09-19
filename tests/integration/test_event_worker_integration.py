"""Integration tests for EventWorker over real SQLite.

The interesting failures here are concurrency and crash ones: two workers racing for one
event, a worker cancelled mid-handler leaving a live lease, and a stale worker coming back
after its event was reclaimed. None of that can be proven against a fake database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.retry import RetryPolicy
from assistant.domain.errors import StaleEventClaim
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock, ScriptedHandler, make_event

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LEASE = timedelta(seconds=30)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


def _worker(
    database: Database,
    clock: FakeClock,
    handler: ScriptedHandler,
    *,
    worker_id: str,
    policy: RetryPolicy | None = None,
) -> EventWorker:
    return EventWorker(
        SqliteEventRepository(Database.at(database.path), clock),
        handler,
        clock,
        policy if policy is not None else RetryPolicy(),
        worker_id=worker_id,
        lease_duration=LEASE,
        poll_interval=timedelta(milliseconds=10),
    )


def _status(database: Database) -> list[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT status FROM inbound_events ORDER BY received_at, id"
        ).fetchall()
    return [str(row["status"]) for row in rows]


def _attempts(database: Database) -> list[int]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT attempts FROM inbound_events ORDER BY received_at, id"
        ).fetchall()
    return [int(row["attempts"]) for row in rows]


async def test_two_workers_racing_process_the_event_exactly_once(
    database: Database, clock: FakeClock
) -> None:
    event = make_event(source="smail", external_id="race")
    await SqliteEventRepository(Database.at(database.path), clock).add(event)
    handler_a = ScriptedHandler(["ok"])
    handler_b = ScriptedHandler(["ok"])
    worker_a = _worker(database, clock, handler_a, worker_id="worker-a")
    worker_b = _worker(database, clock, handler_b, worker_id="worker-b")

    results = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

    assert sorted(results, key=str) == [WorkerResult.IDLE, WorkerResult.PROCESSED]
    handled = handler_a.handled + handler_b.handled
    assert [item.id for item in handled] == [event.id]
    assert _status(database) == ["PROCESSED"]
    assert _attempts(database) == [1]


async def test_cancelled_worker_leaves_a_lease_that_another_worker_recovers(
    database: Database, clock: FakeClock
) -> None:
    event = make_event(source="smail", external_id="crash")
    await SqliteEventRepository(Database.at(database.path), clock).add(event)
    blocking = ScriptedHandler(["block"])
    worker_a = _worker(database, clock, blocking, worker_id="worker-a")

    task = asyncio.create_task(worker_a.run_once())
    await asyncio.wait_for(blocking.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _status(database) == ["PROCESSING"]

    recovery = _worker(database, clock, ScriptedHandler(["ok"]), worker_id="worker-b")
    assert await recovery.run_once() is WorkerResult.IDLE  # the lease is still alive

    clock.advance(int(LEASE.total_seconds()))
    assert await recovery.run_once() is WorkerResult.PROCESSED
    assert _status(database) == ["PROCESSED"]
    assert _attempts(database) == [2]


async def test_a_stale_worker_cannot_overwrite_the_recovering_attempt(
    database: Database, clock: FakeClock
) -> None:
    repository = SqliteEventRepository(Database.at(database.path), clock)
    event = make_event(source="smail", external_id="stale")
    await repository.add(event)
    stale = await repository.claim_next(
        worker_id="worker-a",
        claim_token=UUID(int=1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )
    assert stale is not None
    clock.advance(int(LEASE.total_seconds()))
    assert await _worker(
        database, clock, ScriptedHandler(["ok"]), worker_id="worker-b"
    ).run_once() is WorkerResult.PROCESSED

    # The old worker wakes up with a token nobody honours any more.
    with pytest.raises(StaleEventClaim):
        await repository.complete_claim(
            event.id, claim_token=stale.claim_token, completed_at=clock.now()
        )

    assert _status(database) == ["PROCESSED"]
    assert _attempts(database) == [2]


async def test_failure_then_success_end_to_end(database: Database, clock: FakeClock) -> None:
    event = make_event(source="smail", external_id="retry")
    await SqliteEventRepository(Database.at(database.path), clock).add(event)
    handler = ScriptedHandler(["fail", "ok"])
    policy = RetryPolicy(max_attempts=3, base_delay=timedelta(seconds=10))
    worker = _worker(database, clock, handler, worker_id="worker-a", policy=policy)

    assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
    assert _status(database) == ["FAILED"]

    clock.advance(9)
    assert await worker.run_once() is WorkerResult.IDLE

    clock.advance(1)
    assert await worker.run_once() is WorkerResult.PROCESSED
    assert _status(database) == ["PROCESSED"]
    assert _attempts(database) == [2]


async def test_dead_lettered_events_are_never_picked_up_again(
    database: Database, clock: FakeClock
) -> None:
    event = make_event(source="smail", external_id="poison")
    await SqliteEventRepository(Database.at(database.path), clock).add(event)
    policy = RetryPolicy(max_attempts=1, base_delay=timedelta(seconds=10))
    worker = _worker(
        database, clock, ScriptedHandler(["fail", "ok"]), worker_id="worker-a", policy=policy
    )

    assert await worker.run_once() is WorkerResult.DEAD_LETTERED

    clock.advance(int(LEASE.total_seconds()) * 10)
    assert await worker.run_once() is WorkerResult.IDLE
    assert _status(database) == ["DEAD_LETTERED"]
    assert _attempts(database) == [1]


async def test_run_forever_drains_the_inbox_then_stops(
    database: Database, clock: FakeClock
) -> None:
    repository = SqliteEventRepository(Database.at(database.path), clock)
    for index in range(3):
        await repository.add(make_event(source="smail", external_id=f"bulk-{index}"))
    stop_event = asyncio.Event()
    handler = ScriptedHandler(["ok", "ok", "ok"])
    worker = _worker(database, clock, handler, worker_id="worker-a")

    task = asyncio.create_task(worker.run_forever(stop_event))
    deadline = asyncio.get_running_loop().time() + 2
    while len(handler.handled) < 3:
        assert asyncio.get_running_loop().time() < deadline, "worker did not drain the inbox"
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert _status(database) == ["PROCESSED", "PROCESSED", "PROCESSED"]
    assert _attempts(database) == [1, 1, 1]
