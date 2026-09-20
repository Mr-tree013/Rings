"""Manual input end to end: stored, bridged, repairable and analyzed (ADR-0029).

Real SQLite and the real event inbox. The order is the guarantee — the text is durable before the
event exists — so these tests check both crash windows and the no-mutation rule that keeps pasted
text from becoming work.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.application.event_inbox import IngestDisposition
from assistant.application.manual_input_service import (
    MANUAL_EVENT_TYPE,
    ManualInputService,
)
from assistant.domain.errors import (
    InvalidManualInput,
    ManualInputNotFound,
)
from assistant.domain.manual_input import ManualInputSource
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.manual_inputs import SqliteManualInputRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
TEXT = "Forwarded: the lab report is due Friday 23:59."


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def service(database: Database, clock: FakeClock) -> ManualInputService:
    return ManualInputService(
        SqliteManualInputRepository(database), SqliteEventRepository(database, clock), clock
    )


def _events(database: Database) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT source, event_type, external_id, content FROM inbound_events"
        ).fetchall()
    return [dict(row) for row in rows]


def _counts(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: int(
                connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()[0]
            )
            for table in sorted(tables)
        }


async def test_input_is_stored_and_queued_with_identity_only(
    service: ManualInputService, database: Database
) -> None:
    stored = await service.create_input(TEXT, source=ManualInputSource.QQ_FORWARD)

    assert stored.disposition is IngestDisposition.CREATED
    manual_input = await service.get_input(stored.manual_input.id)
    assert manual_input.text == TEXT
    assert manual_input.source is ManualInputSource.QQ_FORWARD
    events = _events(database)
    assert len(events) == 1
    assert events[0]["source"] == "manual:qq-forward"
    assert events[0]["event_type"] == MANUAL_EVENT_TYPE
    assert events[0]["external_id"] == f"manual-input:{manual_input.id}"
    # §22: the event names the input; the text itself stays in the input row.
    assert json.loads(str(events[0]["content"])) == {
        "manual_input_id": str(manual_input.id),
        "source": "qq-forward",
    }
    assert TEXT not in str(events[0]["content"])


async def test_the_default_source_is_manual(
    service: ManualInputService,
) -> None:
    stored = await service.create_input(TEXT)

    assert stored.manual_input.source is ManualInputSource.MANUAL


async def test_blank_input_is_refused_before_anything_is_stored(
    service: ManualInputService, database: Database
) -> None:
    with pytest.raises(InvalidManualInput):
        await service.create_input("   ")

    assert _events(database) == []
    assert await service.list_inputs(limit=None) == []


async def test_an_unknown_source_is_refused(
    service: ManualInputService, database: Database
) -> None:
    with pytest.raises(InvalidManualInput):
        await service.create_input(TEXT, source="shell")

    assert _events(database) == []


async def test_inputs_are_listed_newest_first_and_resolved_by_prefix(
    service: ManualInputService, clock: FakeClock
) -> None:
    first = await service.create_input("first note")
    clock.advance(60)
    second = await service.create_input("second note")

    listed = await service.list_inputs(limit=None)

    assert [item.id for item in listed] == [second.manual_input.id, first.manual_input.id]
    resolved = await service.get_input(str(first.manual_input.id)[:8])
    assert resolved.id == first.manual_input.id
    with pytest.raises(ManualInputNotFound):
        await service.get_input("ffffffff")


# ---------------------------------------------------------------- crash windows


async def test_an_input_without_its_event_is_repaired(
    service: ManualInputService, database: Database
) -> None:
    stored = await service.create_input(TEXT)
    with database.connect() as connection:  # the crash window: row committed, event missing
        connection.execute("DELETE FROM manual_input_event_links")
        connection.execute("DELETE FROM inbound_events")

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (1, 0)
    assert len(_events(database)) == 1
    assert stored.manual_input.id is not None


async def test_an_event_without_its_link_is_linked_idempotently(
    service: ManualInputService, database: Database
) -> None:
    await service.create_input(TEXT)
    with database.connect() as connection:  # the other crash window
        connection.execute("DELETE FROM manual_input_event_links")

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (0, 1)
    assert len(_events(database)) == 1


async def test_repair_is_bounded_and_leaves_nothing_half_done(
    service: ManualInputService, database: Database
) -> None:
    for index in range(3):
        await service.create_input(f"note {index}")
    with database.connect() as connection:
        connection.execute("DELETE FROM manual_input_event_links")

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (0, 3)
    repository = SqliteManualInputRepository(database)
    unlinked = await repository.list_unlinked_inputs(limit=10)
    assert unlinked == []


# ---------------------------------------------------------------- no side effects


async def test_storing_input_touches_only_its_own_tables(
    service: ManualInputService, database: Database
) -> None:
    before = _counts(database)

    await service.create_input(TEXT, source=ManualInputSource.QQ_FORWARD)

    after = _counts(database)
    changed = {table for table in before if before[table] != after[table]}
    assert changed == {"manual_inputs", "manual_input_event_links", "inbound_events"}
    for untouched in (
        "tasks",
        "cases",
        "action_requests",
        "approvals",
        "execution_runs",
        "corrections",
        "fact_candidates",
        "confirmed_facts",
        "playbook_candidates",
        "playbooks",
    ):
        if untouched in before:
            assert before[untouched] == after[untouched]
