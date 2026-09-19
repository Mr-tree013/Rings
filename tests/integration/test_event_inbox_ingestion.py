"""Integration test: concurrent EventInbox ingestion over one real database file.

This is the Phase 1B end-to-end proof that idempotent ingestion holds when two adapters
race: exactly one event is created, the other call recognises it, both callers see the
same event id, and the table holds one row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from assistant.application.event_inbox import EventInbox, IngestDisposition, IngestEvent
from assistant.domain.inbound_event import EventId
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import CountingEventIdFactory, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


def _inbox(
    path: str, clock: FakeClock, *, new_id: Callable[[], EventId] | None = None
) -> EventInbox:
    """Build an inbox for `path`; without `new_id` it uses the production uuid4 factory."""
    repository = SqliteEventRepository(Database.at(path), clock)
    if new_id is None:
        return EventInbox(repository, clock)
    return EventInbox(repository, clock, new_id=new_id)


def _row_count(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT count(*) FROM inbound_events").fetchone()[0])


async def test_repeated_ingestion_of_one_identity_is_a_single_event(
    database: Database, clock: FakeClock
) -> None:
    inbox = _inbox(database.path, clock, new_id=CountingEventIdFactory())
    command = IngestEvent(
        source="smail",
        external_id="uidvalidity1:uid456",
        event_type="mail.received",
        content="notice",
    )
    first = await inbox.ingest(command)
    second = await inbox.ingest(command)

    assert first.disposition is IngestDisposition.CREATED
    assert second.disposition is IngestDisposition.DUPLICATE
    assert first.event.id == second.event.id
    assert _row_count(database) == 1


async def test_concurrent_ingestion_of_one_identity_creates_exactly_one_event(
    database: Database, clock: FakeClock
) -> None:
    command = IngestEvent(
        source="smail",
        external_id="uidvalidity1:uid456",
        event_type="mail.received",
        content="notice",
    )
    inbox_a = _inbox(database.path, clock)
    inbox_b = _inbox(database.path, clock)

    first, second = await asyncio.gather(inbox_a.ingest(command), inbox_b.ingest(command))

    dispositions = sorted([first.disposition, second.disposition], key=str)
    assert dispositions == [IngestDisposition.CREATED, IngestDisposition.DUPLICATE]
    assert first.event.id == second.event.id
    assert _row_count(database) == 1


async def test_concurrent_ingestion_of_local_notes_creates_two_events(
    database: Database, clock: FakeClock
) -> None:
    command = IngestEvent(source="cli", event_type="local.note", content="remind me")
    inbox_a = _inbox(database.path, clock)
    inbox_b = _inbox(database.path, clock)

    first, second = await asyncio.gather(inbox_a.ingest(command), inbox_b.ingest(command))

    assert first.disposition is IngestDisposition.CREATED
    assert second.disposition is IngestDisposition.CREATED
    assert first.event.id != second.event.id
    assert _row_count(database) == 2
