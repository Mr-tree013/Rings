"""Integration tests for the SQLite inbound event repository.

Real SQLite files, no mocks: durability across reopen, database-level deduplication and
atomic transitions are exactly the properties that a mock would fabricate.
"""

from __future__ import annotations

import sqlite3
import threading
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
from assistant.domain.inbound_event import EventStatus
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
    db = Database.open(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db
    db.close()


@pytest.fixture
def repository(database: Database, clock: FakeClock) -> SqliteEventRepository:
    return SqliteEventRepository(database, clock)


def _row_count(database: Database) -> int:
    return int(database.connection.execute("SELECT count(*) FROM inbound_events").fetchone()[0])


def test_event_survives_reopening_the_repository(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="42", event_type="mail.received")
    repository.add(event)
    database.close()

    reopened = Database.open(database.path)
    try:
        stored = SqliteEventRepository(reopened, clock).get(event.id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.id == event.id
    assert stored.source == "smail"
    assert stored.external_id == "42"
    assert stored.event_type == "mail.received"
    assert stored.content == event.content
    assert stored.received_at == event.received_at
    assert stored.status is EventStatus.RECEIVED


def test_get_returns_none_for_an_unknown_event(repository: SqliteEventRepository) -> None:
    assert repository.get(uuid4()) is None


def test_duplicate_source_and_external_id_is_rejected(
    database: Database, repository: SqliteEventRepository
) -> None:
    repository.add(make_event(source="smail", external_id="42"))

    with pytest.raises(DuplicateInboundEvent) as excinfo:
        repository.add(make_event(source="smail", external_id="42"))

    assert excinfo.value.source == "smail"
    assert excinfo.value.external_id == "42"
    assert _row_count(database) == 1


def test_events_without_external_id_may_repeat(
    database: Database, repository: SqliteEventRepository
) -> None:
    repository.add(make_event(source="cli", external_id=None))
    repository.add(make_event(source="cli", external_id=None))

    assert _row_count(database) == 2


def test_same_external_id_from_different_sources_coexist(
    database: Database, repository: SqliteEventRepository
) -> None:
    repository.add(make_event(source="smail", external_id="42"))
    repository.add(make_event(source="qq", external_id="42"))

    assert _row_count(database) == 2


def test_deduplication_is_enforced_by_the_database_not_by_python(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    SqliteEventRepository(database, clock).add(make_event(source="smail", external_id="42"))

    other = Database.open(database.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            other.connection.execute(
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
    finally:
        other.close()
    assert _row_count(database) == 1


def test_two_connections_racing_for_one_identity_create_exactly_one_row(
    database: Database, clock: FakeClock
) -> None:
    path = database.path
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def attempt() -> None:
        connection = Database.open(path)
        try:
            repository = SqliteEventRepository(connection, clock)
            barrier.wait(timeout=10)
            try:
                repository.add(make_event(source="smail", external_id="race-1"))
                outcome = "created"
            except DuplicateInboundEvent:
                outcome = "duplicate"
        finally:
            connection.close()
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(outcomes) == ["created", "duplicate"]
    assert _row_count(database) == 1


def test_reusing_an_event_id_is_not_reported_as_a_duplicate_identity(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="cli", external_id=None)
    repository.add(event)

    with pytest.raises(StoreError, match="could not persist"):
        repository.add(event)


def test_transition_is_persisted_and_visible_after_reopening(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="7")
    repository.add(event)
    clock.advance(60)

    moved = repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )
    database.close()

    reopened = Database.open(database.path)
    try:
        stored = SqliteEventRepository(reopened, clock).get(event.id)
    finally:
        reopened.close()

    assert moved.status is EventStatus.PROCESSING
    assert stored is not None
    assert stored.status is EventStatus.PROCESSING
    assert stored.attempts == 1


def test_updated_at_follows_the_clock(
    database: Database, clock: FakeClock, repository: SqliteEventRepository
) -> None:
    event = make_event(source="smail", external_id="8")
    repository.add(event)
    created_at = _stored_timestamp(database, event.id, "created_at")

    clock.advance(120)
    repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )

    assert _stored_timestamp(database, event.id, "updated_at") != created_at
    assert _stored_timestamp(database, event.id, "updated_at") == (
        "2026-09-19T12:02:00.000000+00:00"
    )


def test_every_processing_attempt_increases_the_counter(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="smail", external_id="9")
    repository.add(event)

    repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )
    failed = repository.transition(
        event.id,
        expected=EventStatus.PROCESSING,
        target=EventStatus.FAILED,
        error="smtp timeout",
    )
    retrying = repository.transition(
        event.id, expected=EventStatus.FAILED, target=EventStatus.PROCESSING
    )

    assert failed.attempts == 1
    assert failed.last_error == "smtp timeout"
    assert retrying.attempts == 2
    assert retrying.last_error == "smtp timeout"


def test_success_clears_the_stored_error(repository: SqliteEventRepository) -> None:
    event = make_event(
        source="smail", external_id="10", status=EventStatus.FAILED, attempts=1
    )
    repository.add(event)

    processed = repository.transition(
        event.id, expected=EventStatus.FAILED, target=EventStatus.PROCESSING
    )
    processed = repository.transition(
        event.id, expected=EventStatus.PROCESSING, target=EventStatus.PROCESSED
    )

    assert processed.last_error is None
    stored = repository.get(event.id)
    assert stored is not None
    assert stored.last_error is None


def test_transition_rejects_a_stale_expected_status(
    repository: SqliteEventRepository,
) -> None:
    event = make_event(source="smail", external_id="11")
    repository.add(event)
    repository.transition(
        event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
    )

    with pytest.raises(UnexpectedEventStatus) as excinfo:
        repository.transition(
            event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
        )

    assert excinfo.value.expected is EventStatus.RECEIVED
    assert excinfo.value.actual is EventStatus.PROCESSING


def test_transition_rejects_an_illegal_move(repository: SqliteEventRepository) -> None:
    event = make_event(source="smail", external_id="12")
    repository.add(event)

    with pytest.raises(InvalidEventTransition):
        repository.transition(
            event.id, expected=EventStatus.RECEIVED, target=EventStatus.PROCESSED
        )

    stored = repository.get(event.id)
    assert stored is not None
    assert stored.status is EventStatus.RECEIVED


def test_transition_rejects_an_unknown_event(repository: SqliteEventRepository) -> None:
    with pytest.raises(EventNotFound):
        repository.transition(
            uuid4(), expected=EventStatus.RECEIVED, target=EventStatus.PROCESSING
        )


def test_list_pending_returns_received_and_failed_oldest_first(
    repository: SqliteEventRepository,
) -> None:
    earliest = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    middle = datetime(2026, 9, 19, 11, 0, tzinfo=UTC)
    latest = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    repository.add(make_event(source="smail", external_id="a", received_at=middle))
    repository.add(make_event(source="smail", external_id="b", received_at=earliest))
    repository.add(
        make_event(
            source="smail",
            external_id="c",
            received_at=latest,
            status=EventStatus.FAILED,
            attempts=1,
        )
    )
    repository.add(
        make_event(
            source="smail",
            external_id="d",
            received_at=latest,
            status=EventStatus.PROCESSED,
            attempts=1,
        )
    )

    pending = repository.list_pending(limit=10)

    assert [event.external_id for event in pending] == ["b", "a", "c"]
    assert repository.list_pending(limit=1)[0].external_id == "b"


def test_list_pending_rejects_a_non_positive_limit(
    repository: SqliteEventRepository,
) -> None:
    with pytest.raises(ValueError, match="limit"):
        repository.list_pending(limit=0)


def _stored_timestamp(database: Database, event_id: object, column: str) -> str:
    assert column in {"created_at", "updated_at"}
    row = database.connection.execute(
        f"SELECT {column} FROM inbound_events WHERE id = ?", (str(event_id),)
    ).fetchone()
    assert row is not None
    return str(row[column])

