"""Integration tests for atomic claiming, leases and fencing, against real SQLite.

sqlite3 is never mocked here. These are the behaviours a mock would fabricate:
one-winner claiming inside a write transaction, lease expiry recovery, and stale workers
being unable to overwrite the attempt that replaced them (ADR-0010).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from assistant.domain.errors import EventNotFound, StaleEventClaim
from assistant.domain.event_claim import EventClaim
from assistant.domain.inbound_event import EventStatus
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock, make_event

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


@pytest.fixture
def repository(database: Database, clock: FakeClock) -> SqliteEventRepository:
    return SqliteEventRepository(database, clock)


def _token(value: int) -> UUID:
    return UUID(int=value)


def _row(database: Database, event_id: UUID) -> sqlite3.Row:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM inbound_events WHERE id = ?", (str(event_id),)
        ).fetchone()
    assert row is not None
    return row


async def test_claims_the_oldest_received_event(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    older = make_event(source="smail", external_id="older", received_at=START - timedelta(hours=2))
    newer = make_event(source="smail", external_id="newer", received_at=START - timedelta(hours=1))
    await repository.add(newer)
    await repository.add(older)

    claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    assert claim is not None
    assert claim.event.id == older.id
    assert claim.event.attempts == 1
    assert claim.event.status is EventStatus.PROCESSING


async def test_claim_records_the_lease_on_the_row(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="1")
    await repository.add(event)

    claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    row = _row(database, event.id)
    assert claim is not None
    assert row["status"] == "PROCESSING"
    assert row["attempts"] == 1
    assert row["claim_token"] == str(claim.claim_token)
    assert row["claimed_by"] == "worker-a"
    assert row["claimed_at"] == "2026-09-19T12:00:00.000000+00:00"
    assert row["lease_expires_at"] == "2026-09-19T12:05:00.000000+00:00"
    assert row["next_attempt_at"] is None


async def test_claims_a_retry_once_it_is_due(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(
        source="smail",
        external_id="2",
        status=EventStatus.FAILED,
        attempts=1,
        next_attempt_at=START,
    )
    await repository.add(event)

    claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    assert claim is not None
    assert claim.event.id == event.id
    assert claim.event.attempts == 2
    assert claim.event.next_attempt_at is None


async def test_ignores_a_retry_that_is_not_due_yet(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    await repository.add(
        make_event(
            source="smail",
            external_id="3",
            status=EventStatus.FAILED,
            attempts=1,
            next_attempt_at=START + timedelta(seconds=1),
        )
    )

    claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    assert claim is None


async def test_does_not_steal_a_live_lease(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="4")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    stolen = await repository.claim_next(
        worker_id="worker-b",
        claim_token=_token(2),
        now=clock.now() + timedelta(minutes=1),
        lease_expires_at=clock.now() + timedelta(minutes=6),
    )

    assert stolen is None
    assert _row(database, event.id)["claimed_by"] == "worker-a"


async def test_reclaims_an_expired_lease_with_a_new_token(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="5")
    await repository.add(event)
    first = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + timedelta(minutes=1),
    )
    assert first is not None

    clock.advance(60)
    second = await repository.claim_next(
        worker_id="worker-b",
        claim_token=_token(2),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    assert second is not None
    assert second.claim_token != first.claim_token
    assert second.claimed_by == "worker-b"
    assert second.event.attempts == 2

    with pytest.raises(StaleEventClaim):
        await repository.complete_claim(
            event.id, claim_token=first.claim_token, completed_at=clock.now()
        )

    completed = await repository.complete_claim(
        event.id, claim_token=second.claim_token, completed_at=clock.now()
    )
    assert completed.status is EventStatus.PROCESSED
    assert completed.attempts == 2


async def test_never_claims_processed_or_dead_lettered_events(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    processed = make_event(source="smail", external_id="6")
    dead = make_event(source="smail", external_id="7")
    await repository.add(processed)
    await repository.add(dead)
    first_claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )
    second_claim = await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(2),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )
    assert first_claim is not None
    assert second_claim is not None
    assert {first_claim.event.id, second_claim.event.id} == {processed.id, dead.id}
    await repository.complete_claim(
        first_claim.event.id, claim_token=first_claim.claim_token, completed_at=clock.now()
    )
    await repository.fail_claim(
        second_claim.event.id,
        claim_token=second_claim.claim_token,
        failed_at=clock.now(),
        error="poison",
        next_attempt_at=None,
        dead_letter=True,
    )
    clock.advance(int(LEASE.total_seconds() * 2))

    claim = await repository.claim_next(
        worker_id="worker-b",
        claim_token=_token(3),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    assert claim is None


async def test_only_one_of_two_competing_claims_wins(
    database: Database, clock: FakeClock
) -> None:
    event = make_event(source="smail", external_id="8")
    first = SqliteEventRepository(Database.at(database.path), clock)
    second = SqliteEventRepository(Database.at(database.path), clock)
    await first.add(event)

    results = await asyncio.gather(
        first.claim_next(
            worker_id="worker-a",
            claim_token=_token(1),
            now=clock.now(),
            lease_expires_at=clock.now() + LEASE,
        ),
        second.claim_next(
            worker_id="worker-b",
            claim_token=_token(2),
            now=clock.now(),
            lease_expires_at=clock.now() + LEASE,
        ),
    )

    claimed = [result for result in results if isinstance(result, EventClaim)]
    assert len(claimed) == 1, results
    assert results.count(None) == 1
    assert _row(database, event.id)["attempts"] == 1


async def test_completion_clears_the_lease_and_failure_metadata(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="9")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    completed = await repository.complete_claim(
        event.id, claim_token=_token(1), completed_at=clock.now()
    )

    row = _row(database, event.id)
    assert completed.status is EventStatus.PROCESSED
    assert row["attempts"] == 1
    assert row["last_error"] is None
    assert row["next_attempt_at"] is None
    assert row["dead_lettered_at"] is None
    assert row["claim_token"] is None
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None
    assert row["lease_expires_at"] is None


async def test_retry_failure_records_the_schedule_and_clears_the_lease(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="10")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )
    retry_at = clock.now() + timedelta(seconds=30)

    failed = await repository.fail_claim(
        event.id,
        claim_token=_token(1),
        failed_at=clock.now(),
        error="RuntimeError: boom",
        next_attempt_at=retry_at,
        dead_letter=False,
    )

    row = _row(database, event.id)
    assert failed.status is EventStatus.FAILED
    assert failed.last_error == "RuntimeError: boom"
    assert row["next_attempt_at"] == "2026-09-19T12:00:30.000000+00:00"
    assert row["dead_lettered_at"] is None
    assert row["claim_token"] is None
    assert row["lease_expires_at"] is None
    assert row["attempts"] == 1


async def test_dead_letter_records_the_terminal_timestamp(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="11")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    dead = await repository.fail_claim(
        event.id,
        claim_token=_token(1),
        failed_at=clock.now(),
        error="PermanentEventError: nope",
        next_attempt_at=None,
        dead_letter=True,
    )

    row = _row(database, event.id)
    assert dead.status is EventStatus.DEAD_LETTERED
    assert row["dead_lettered_at"] == "2026-09-19T12:00:00.000000+00:00"
    assert row["next_attempt_at"] is None
    assert row["claim_token"] is None


async def test_a_stale_worker_cannot_complete_or_fail(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="12")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + timedelta(seconds=1),
    )
    clock.advance(1)
    await repository.claim_next(
        worker_id="worker-b",
        claim_token=_token(2),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    with pytest.raises(StaleEventClaim):
        await repository.complete_claim(
            event.id, claim_token=_token(1), completed_at=clock.now()
        )
    with pytest.raises(StaleEventClaim):
        await repository.fail_claim(
            event.id,
            claim_token=_token(1),
            failed_at=clock.now(),
            error="too late",
            next_attempt_at=clock.now() + timedelta(seconds=30),
            dead_letter=False,
        )

    row = _row(database, event.id)
    assert row["claimed_by"] == "worker-b"
    assert row["status"] == "PROCESSING"


async def test_completing_an_unknown_event_is_rejected(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    with pytest.raises(EventNotFound):
        await repository.complete_claim(
            UUID(int=999), claim_token=_token(1), completed_at=clock.now()
        )


async def test_claim_rejects_bad_arguments(repository: SqliteEventRepository) -> None:
    with pytest.raises(ValueError, match="worker_id"):
        await repository.claim_next(
            worker_id="  ",
            claim_token=_token(1),
            now=START,
            lease_expires_at=START + LEASE,
        )
    with pytest.raises(ValueError, match="nil UUID"):
        await repository.claim_next(
            worker_id="worker-a",
            claim_token=UUID(int=0),
            now=START,
            lease_expires_at=START + LEASE,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.claim_next(
            worker_id="worker-a",
            claim_token=_token(1),
            now=datetime(2026, 9, 19, 12, 0),
            lease_expires_at=START + LEASE,
        )
    with pytest.raises(ValueError, match="after now"):
        await repository.claim_next(
            worker_id="worker-a",
            claim_token=_token(1),
            now=START,
            lease_expires_at=START,
        )


async def test_fail_claim_validates_its_arguments(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="13")
    await repository.add(event)
    await repository.claim_next(
        worker_id="worker-a",
        claim_token=_token(1),
        now=clock.now(),
        lease_expires_at=clock.now() + LEASE,
    )

    with pytest.raises(ValueError, match="requires next_attempt_at"):
        await repository.fail_claim(
            event.id,
            claim_token=_token(1),
            failed_at=clock.now(),
            error="boom",
            next_attempt_at=None,
            dead_letter=False,
        )
    with pytest.raises(ValueError, match="must be None"):
        await repository.fail_claim(
            event.id,
            claim_token=_token(1),
            failed_at=clock.now(),
            error="boom",
            next_attempt_at=clock.now() + timedelta(seconds=1),
            dead_letter=True,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.complete_claim(
            event.id, claim_token=_token(1), completed_at=datetime(2026, 9, 19, 12, 0)
        )
