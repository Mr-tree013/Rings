"""The watcher end to end: baseline, changes, reconciliation and the event bridge (ADR-0029).

Real SQLite, real snapshots, a scripted source. The properties under test are the ones that make a
watcher safe to leave running: the first fetch is a baseline and emits nothing, only a content hash
change is announced, a server that answers `304` forever cannot hide a change because a full fetch
is forced periodically, every crash window between observation and event is repairable, one broken
target does not stop its siblings, and a watcher never writes anything except its own tables.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.application.web_watch import (
    WEB_EVENT_TYPE,
    TargetOutcome,
    WebWatchService,
)
from assistant.domain.config import WatchersConfig, WebTargetConfig
from assistant.domain.errors import (
    WebTargetNotFound,
    WebWatchRequestFailed,
    WebWatchUnsafeAddress,
)
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from assistant.store.web_watch import SqliteWebWatchRepository
from tests.support.fakes import FakeClock
from tests.support.watchers import (
    NOW,
    SECOND_TARGET_ID,
    SECOND_TARGET_URL,
    TARGET_ID,
    TARGET_URL,
    FakeWebSource,
)

PAGE_A = "Notices\nRegistration closes Oct 20.\n"
PAGE_B = "Notices\nRegistration closes Oct 25.\nWorkshop Oct 30.\n"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def snapshots_store(tmp_path: Path) -> WebSnapshotStore:
    return WebSnapshotStore(tmp_path / "runtime")


def _service(
    database: Database,
    clock: FakeClock,
    source: FakeWebSource,
    *,
    targets: tuple[WebTargetConfig, ...] | None = None,
    full_fetch_every: int = 24,
) -> WebWatchService:
    config = WatchersConfig(
        web=targets or (WebTargetConfig(id=TARGET_ID, url=TARGET_URL),),
        full_fetch_every=full_fetch_every,
    )
    return WebWatchService(
        source,
        SqliteWebWatchRepository(database),
        SqliteEventRepository(database, clock),
        clock,
        config,
    )


def _events(database: Database) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT source, event_type, external_id, content FROM inbound_events "
            "ORDER BY received_at, id"
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


# ------------------------------------------------------------------ baseline semantics


async def test_the_first_fetch_is_a_baseline_and_emits_nothing(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§18: enabling a watcher must not announce the whole site as news."""
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)

    results = await _service(database, clock, source).sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.BASELINE]
    observations = await SqliteWebWatchRepository(database).list_observations(limit=None)
    assert len(observations) == 1
    assert observations[0].is_baseline is True
    assert observations[0].previous_observation_id is None
    assert _events(database) == []


async def test_an_unchanged_page_emits_nothing_and_moves_the_counter(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()

    results = await service.sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.UNCHANGED]
    repository = SqliteWebWatchRepository(database)
    assert len(await repository.list_observations(limit=None)) == 1
    state = await repository.get_state(TARGET_ID)
    assert state is not None and state.checks_since_full == 1
    assert _events(database) == []


async def test_a_content_change_creates_one_observation_and_one_event(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    source.set_page(TARGET_URL, PAGE_B)
    clock.advance(300)

    results = await service.sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.CHANGED]
    repository = SqliteWebWatchRepository(database)
    observations = await repository.list_observations(limit=None)
    assert len(observations) == 2
    latest = observations[0]
    assert latest.is_baseline is False
    assert latest.previous_observation_id is not None
    events = _events(database)
    assert len(events) == 1
    assert events[0]["source"] == f"web:{TARGET_ID}"
    assert events[0]["event_type"] == WEB_EVENT_TYPE
    assert events[0]["external_id"] == f"observation:{latest.id}"
    # §19: the event carries identity only — never the page.
    assert json.loads(str(events[0]["content"])) == {
        "observation_id": str(latest.id),
        "target_id": TARGET_ID,
    }
    assert "Registration closes" not in str(events[0]["content"])


async def test_a_second_identical_change_does_not_emit_a_second_event(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    source.set_page(TARGET_URL, PAGE_B)
    await service.sync_once()

    results = await service.sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.UNCHANGED]
    assert len(_events(database)) == 1


async def test_a_url_change_starts_a_new_baseline_instead_of_a_change(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§15: a configured URL that moved must not be reported as a change of the old page."""
    source = FakeWebSource(
        {TARGET_URL: PAGE_A, SECOND_TARGET_URL: PAGE_B}, snapshots=snapshots_store
    )
    service = _service(database, clock, source)
    await service.sync_once()
    clock.advance(300)
    moved = _service(
        database,
        clock,
        source,
        targets=(WebTargetConfig(id=TARGET_ID, url=SECOND_TARGET_URL),),
    )

    results = await moved.sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.BASELINE]
    assert len(_events(database)) == 0
    observations = await SqliteWebWatchRepository(database).list_observations(limit=None)
    assert observations[0].url == SECOND_TARGET_URL


# ------------------------------------------------------- conditional + full fetch


async def test_a_server_that_always_says_not_modified_cannot_hide_a_change(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§41: the validators are an optimization; the forced full fetch is the correctness rule."""
    source = FakeWebSource(
        {TARGET_URL: PAGE_A}, snapshots=snapshots_store, answer_not_modified=True
    )
    service = _service(database, clock, source, full_fetch_every=3)
    await service.sync_once()  # baseline, unconditional
    for _ in range(3):
        clock.advance(300)
        await service.sync_once()  # conditional, answered 304
    assert all(item.conditional for item in source.requests[1:])
    source.set_page(TARGET_URL, PAGE_B)

    # The counter has reached the threshold, so this check cannot use the validators.
    results = await service.sync_once()

    assert source.requests[-1].conditional is False
    assert [item.outcome for item in results] == [TargetOutcome.CHANGED]
    assert len(_events(database)) == 1


async def test_the_counter_restarts_after_a_full_fetch(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source, full_fetch_every=2)

    await service.sync_once()
    await service.sync_once()
    await service.sync_once()
    await service.sync_once()

    state = await SqliteWebWatchRepository(database).get_state(TARGET_ID)
    assert state is not None and state.checks_since_full == 0


# ---------------------------------------------------------------- bridge recovery


async def test_an_observation_without_its_event_is_repaired(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§20: the observation commits first, so a crash between the two steps must be repairable."""
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    source.set_page(TARGET_URL, PAGE_B)
    await service.sync_once()
    with database.connect() as connection:  # simulate the crash window
        connection.execute("DELETE FROM web_observation_event_links")
        connection.execute("DELETE FROM inbound_events")
    assert _events(database) == []

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (1, 0)
    assert len(_events(database)) == 1


async def test_an_event_without_its_link_is_linked_idempotently(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    source.set_page(TARGET_URL, PAGE_B)
    await service.sync_once()
    with database.connect() as connection:  # simulate the other crash window
        connection.execute("DELETE FROM web_observation_event_links")

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (0, 1)
    assert len(_events(database)) == 1  # no second event was created


async def test_an_empty_round_still_repairs_a_bounded_backlog(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """A round with no page changes must still close open crash windows."""
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    source.set_page(TARGET_URL, PAGE_B)
    await service.sync_once()
    with database.connect() as connection:
        connection.execute("DELETE FROM web_observation_event_links")

    results = await service.sync_once()

    assert [item.outcome for item in results] == [TargetOutcome.UNCHANGED]
    assert len(_events(database)) == 1


# -------------------------------------------------------------- target isolation


def _two_targets() -> tuple[WebTargetConfig, ...]:
    return (
        WebTargetConfig(id=TARGET_ID, url=TARGET_URL),
        WebTargetConfig(id=SECOND_TARGET_ID, url=SECOND_TARGET_URL),
    )


async def test_one_broken_target_does_not_stop_its_sibling(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§23: a page that is down is that page's problem."""
    source = FakeWebSource(
        {SECOND_TARGET_URL: PAGE_A},
        snapshots=snapshots_store,
        errors={TARGET_URL: WebWatchRequestFailed(TARGET_URL, "the server answered HTTP 503")},
    )
    service = _service(database, clock, source, targets=_two_targets())

    results = await service.sync_once()

    assert [item.outcome for item in results] == [
        TargetOutcome.FAILED,
        TargetOutcome.BASELINE,
    ]
    assert results[0].ok is False
    assert "503" in (results[0].error or "")
    assert results[1].ok is True
    state = await SqliteWebWatchRepository(database).get_state(SECOND_TARGET_ID)
    assert state is not None and state.has_baseline


async def test_an_unsafe_address_is_a_target_failure_not_a_crash(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource(
        {SECOND_TARGET_URL: PAGE_A},
        snapshots=snapshots_store,
        errors={TARGET_URL: WebWatchUnsafeAddress("example.edu", "127.0.0.1", "private")},
    )
    service = _service(database, clock, source, targets=_two_targets())

    results = await service.sync_once()

    assert results[0].outcome is TargetOutcome.FAILED
    assert "127.0.0.1" in (results[0].error or "")


async def test_syncing_one_target_leaves_the_other_alone(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource(
        {TARGET_URL: PAGE_A, SECOND_TARGET_URL: PAGE_A}, snapshots=snapshots_store
    )
    service = _service(database, clock, source, targets=_two_targets())

    results = await service.sync_once(SECOND_TARGET_ID)

    assert [item.target_id for item in results] == [SECOND_TARGET_ID]
    assert await SqliteWebWatchRepository(database).get_state(TARGET_ID) is None


async def test_an_unknown_target_id_is_refused(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)

    with pytest.raises(WebTargetNotFound):
        await _service(database, clock, source).sync_once("not-configured")


# ---------------------------------------------------------------- no side effects


async def test_watching_touches_only_its_own_tables(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    """§45: a watcher observes. It does not create work, cases, facts, playbooks or actions."""
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()
    before = _counts(database)
    source.set_page(TARGET_URL, PAGE_B)

    await service.sync_once()

    after = _counts(database)
    changed = {table for table in before if before[table] != after[table]}
    assert changed == {
        "web_observations",
        "web_observation_event_links",
        "inbound_events",
    }
    # `web_watch_state` is updated in place, so its row count stays the same while its content
    # address moves — which is exactly what the assertion above cannot see.
    state = await SqliteWebWatchRepository(database).get_state(TARGET_ID)
    assert state is not None
    assert state.content_sha256 == source.stored_sha(PAGE_B)
    for untouched in ("tasks", "cases", "action_requests", "approvals", "facts", "playbooks"):
        if untouched in before:
            assert before[untouched] == after[untouched]


async def test_reading_state_and_observations_is_local(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = _service(database, clock, source)
    await service.sync_once()

    observations = await service.observations(target_id=TARGET_ID)
    state = await service.state(TARGET_ID)
    fetched = await service.observation(str(observations[0].id)[:8])

    assert state is not None and state.has_baseline
    assert fetched.id == observations[0].id
    assert len(source.requests) == 1  # reading never fetches


async def test_the_round_interval_is_honoured_and_shutdown_is_immediate(
    database: Database, clock: FakeClock, snapshots_store: WebSnapshotStore
) -> None:
    import asyncio

    source = FakeWebSource({TARGET_URL: PAGE_A}, snapshots=snapshots_store)
    service = WebWatchService(
        source,
        SqliteWebWatchRepository(database),
        SqliteEventRepository(database, clock),
        clock,
        WatchersConfig(
            web=(WebTargetConfig(id=TARGET_ID, url=TARGET_URL),),
            poll_interval_seconds=10,
        ),
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run_forever(stop_event))

    while not source.requests:
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(running, timeout=5)

    assert len(source.requests) >= 1


def test_the_service_reports_the_daemon_name() -> None:
    assert WebWatchService.name == "web-watch"


def test_timestamps_are_aware() -> None:
    assert NOW.tzinfo is UTC
    assert isinstance(datetime(2026, 9, 25, tzinfo=UTC), datetime)
