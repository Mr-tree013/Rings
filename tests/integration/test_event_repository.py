"""Integration tests for the async SQLite inbound event repository.

Real SQLite files, no mocks: durability across reopen, database-level deduplication and
atomic transitions are exactly the properties a mock would fabricate. Every call goes
through the async port, so the thread boundary (ADR-0009) is covered too.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.domain.errors import (
    DuplicateInboundEvent,
    EventNotFound,
    InvalidEventTransition,
    UnexpectedEventStatus,
)
from assistant.domain.inbound_event import EventStatus, InboundEvent
from assistant.store.db import Database
from assistant.store.errors import StoreError
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock, make_event


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


@pytest.fixture
def repository(database: Database, clock: FakeClock) -> SqliteEventRepository:
    return SqliteEventRepository(database, clock)


def _row_count(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT count(*) FROM inbound_events").fetchone()[0])


def _stored_timestamp(database: Database, event_id: object, column: str) -> str:
    assert column in {"created_at", "updated_at"}
    with database.connect() as connection:
        row = connection.execute(
            f"SELECT {column} FROM inbound_events WHERE id = ?", (str(event_id),)
        ).fetchone()
    assert row is not None
    return str(row[column])


async def test_event_survives_reopening_the_repository(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="42", event_type="mail.received")
    await repository.add(event)

    reopened = SqliteEventRepository(Database.at(database.path), clock)
    stored = await reopened.get(event.id)

    assert stored is not None
    assert stored.id == event.id
    assert stored.source == "smail"
    assert stored.external_id == "42"
    assert stored.event_type == "mail.received"
    assert stored.content == event.content
    assert stored.received_at == event.received_at
    assert stored.status is EventStatus.RECEIVED


async def test_get_returns_none_for_an_unknown_event(
    repository: SqliteEventRepository,
) -> None:
    assert await repository.get(uuid4()) is None


async def test_duplicate_source_and_external_id_is_rejected(
    database: Database, repository: SqliteEventRepository
) -> None:
    await repository.add(make_event(source="smail", external_id="42"))

    with pytest.raises(DuplicateInboundEvent) as excinfo:
        await repository.add(make_event(source="smail", external_id="42"))

    assert excinfo.value.source == "smail"
    assert excinfo.value.external_id == "42"
    assert _row_count(database) == 1


async def test_events_without_external_id_may_repeat(
    database: Database, repository: SqliteEventRepository
) -> None:
    await repository.add(make_event(source="cli", external_id=None))
    await repository.add(make_event(source="cli", external_id=None))

    assert _row_count(database) == 2


async def test_same_external_id_from_different_sources_coexist(
    database: Database, repository: SqliteEventRepository
) -> None:
    await repository.add(make_event(source="smail", external_id="42"))
    await repository.add(make_event(source="qq", external_id="42"))

    assert _row_count(database) == 2


async def test_get_by_external_identity_returns_the_stored_event(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="smail", external_id="uidv123:uid456")
    await repository.add(event)

    found = await repository.get_by_external_identity("smail", "uidv123:uid456")

    assert found is not None
    assert found.id == event.id


async def test_get_by_external_identity_returns_none_when_missing(
    repository: SqliteEventRepository,
) -> None:
    await repository.add(make_event(source="smail", external_id="42"))

    assert await repository.get_by_external_identity("smail", "43") is None
    assert await repository.get_by_external_identity("qq", "42") is None


@pytest.mark.parametrize("external_id", ["", "   "])
async def test_get_by_external_identity_rejects_a_blank_identity(
    repository: SqliteEventRepository, external_id: str
) -> None:
    with pytest.raises(ValueError, match="external_id"):
        await repository.get_by_external_identity("smail", external_id)


async def test_deduplication_is_enforced_by_the_database_not_by_python(
    database: Database, clock: FakeClock
) -> None:
    await SqliteEventRepository(Database.at(database.path), clock).add(
        make_event(source="smail", external_id="42")
    )

    with (
        database.connect() as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            """
            INSERT INTO inbound_events (
                id, source, external_id, event_type, content, received_at,
                status, attempts, last_error, created_at, updated_at
            ) VALUES (
                ?, 'smail', '42', 'mail.received', NULL, '2026-09-19T12:00:00.000000+00:00',
                'RECEIVED', 0, NULL, '2026-09-19T12:00:00.000000+00:00',
                '2026-09-19T12:00:00.000000+00:00'
            )
            """,
            (str(uuid4()),),
        )

    assert _row_count(database) == 1


async def test_concurrent_adds_from_two_repositories_keep_exactly_one_row(
    database: Database, clock: FakeClock
) -> None:
    first = SqliteEventRepository(Database.at(database.path), clock)
    second = SqliteEventRepository(Database.at(database.path), clock)

    results = await asyncio.gather(
        first.add(make_event(source="smail", external_id="race-1")),
        second.add(make_event(source="smail", external_id="race-1")),
        return_exceptions=True,
    )

    created = [result for result in results if isinstance(result, InboundEvent)]
    duplicates = [result for result in results if isinstance(result, DuplicateInboundEvent)]
    assert len(created) == 1, results
    assert len(duplicates) == 1, results
    assert _row_count(database) == 1


async def test_reusing_an_event_id_is_not_reported_as_a_duplicate_identity(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="cli", external_id=None)
    await repository.add(event)

    with pytest.raises(StoreError, match="could not persist"):
        await repository.add(event)


async def test_transition_is_persisted_and_visible_after_reopening(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="7")
    await repository.add(event)
    clock.advance(60)

    moved = await repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )
    stored = await SqliteEventRepository(Database.at(database.path), clock).get(event.id)

    assert moved.status is EventStatus.PROCESSING
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING
    assert stored.attempts == 1


async def test_updated_at_follows_the_clock(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="8")
    await repository.add(event)
    created_at = _stored_timestamp(database, event.id, "created_at")

    clock.advance(120)
    await repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )

    assert _stored_timestamp(database, event.id, "updated_at") == (
        "2026-09-19T12:02:00.000000+00:00"
    )
    assert _stored_timestamp(database, event.id, "updated_at") != created_at


async def test_every_processing_attempt_increases_the_counter(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="smail", external_id="9")
    await repository.add(event)

    await repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )
    failed = await repository.transition(
        event.id,
        expected=EventStatus.PROCESSING,
        target=EventStatus.FAILED,
        error="smtp timeout",
    )
    retrying = await repository.transition(
        event.id, expected=EventStatus.FAILED, target=EventStatus.PROCESSING
    )

    assert failed.attempts == 1
    assert failed.last_error == "smtp timeout"
    assert retrying.attempts == 2
    assert retrying.last_error == "smtp timeout"


async def test_success_clears_the_stored_error(repository: SqliteEventRepository) -> None:
    event = make_event(source="smail", external_id="10", status=EventStatus.FAILED, attempts=1)
    await repository.add(event)

    await repository.transition(
        event.id, expected=EventStatus.FAILED, target=EventStatus.PROCESSING
    )
    processed = await repository.transition(
        event.id, expected=EventStatus.PROCESSING, target=EventStatus.PROCESSED
    )

    assert processed.last_error is None
    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.last_error is None


async def test_transition_rejects_a_stale_expected_status(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="smail", external_id="11")
    await repository.add(event)
    await repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )

    with pytest.raises(UnexpectedEventStatus) as excinfo:
        await repository.transition(
            event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
        )

    assert excinfo.value.expected is EventStatus.RECEIVED
    assert excinfo.value.actual is EventStatus.PROCESSING


async def test_transition_rejects_an_illegal_move(repository: SqliteEventRepository) -> None:
    event = make_event(source="smail", external_id="12")
    await repository.add(event)

    with pytest.raises(InvalidEventTransition):
        await repository.transition(
            event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSED
        )

    stored = await repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.RECEIVED


async def test_transition_rejects_an_unknown_event(repository: SqliteEventRepository) -> None:
    with pytest.raises(EventNotFound):
        await repository.transition(
            uuid4(), expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
        )


async def test_list_pending_returns_received_and_failed_oldest_first(
    repository: SqliteEventRepository,
) -> None:
    earliest = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    middle = datetime(2026, 9, 19, 11, 0, tzinfo=UTC)
    latest = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    await repository.add(make_event(source="smail", external_id="a", received_at=middle))
    await repository.add(make_event(source="smail", external_id="b", received_at=earliest))
    await repository.add(
        make_event(
            source="smail",
            external_id="c",
            received_at=latest,
            status=EventStatus.FAILED,
            attempts=1,
        )
    )
    await repository.add(
        make_event(
            source="smail",
            external_id="d",
            received_at=latest,
            status=EventStatus.PROCESSED,
            attempts=1,
        )
    )

    pending = await repository.list_pending(limit=10)
    limited = await repository.list_pending(limit=1)

    assert [event.external_id for event in pending] == ["b", "a", "c"]
    assert [event.external_id for event in limited] == ["b"]


async def test_list_pending_rejects_a_non_positive_limit(
    repository: SqliteEventRepository,
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await repository.list_pending(limit=0)

