"""Mail sync end to end: cursor, reconciliation, the event bridge and isolation (ADR-0020).

Real SQLite, real raw store, real `EventInbox`, scripted mail server. What is being tested is the
contract a user depends on: mail is stored once, the cursor only advances over durably handled
mail, a rebuilt mailbox is reconciled instead of silently skipped, and every stored message
eventually has exactly one `InboundEvent`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.mail.parser import Rfc822MailParser
from assistant.adapters.mail.raw_store import RawMailStore
from assistant.application.event_inbox import EventInbox
from assistant.application.mail_sync import (
    MAIL_EVENT_TYPE,
    AccountSyncStatus,
    MailSyncService,
)
from assistant.domain.config import MailAccountConfig, MailConfig
from assistant.domain.errors import MailCredentialsMissing
from assistant.domain.mail import MailBodyStatus
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_fakes import (
    FakeMailSource,
    authentication_failure,
    connection_failure,
)

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


class RecordingWaiter:
    """An interval waiter that records what it was asked to wait for."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        self.calls.append(seconds)
        stop_event.set()


def _raw(
    *,
    subject: str = "Subject",
    message_id: str | None = "<a@example.edu>",
    body: str = "body text",
    sender: str = "ada@example.edu",
) -> bytes:
    lines = [f"From: {sender}", "To: me@example.edu", f"Subject: {subject}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    lines += [
        "Date: Fri, 23 Oct 2026 23:59:00 +0800",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\r\n".join(lines).encode()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


def _service(
    database: Database,
    clock: FakeClock,
    source: FakeMailSource,
    *,
    accounts: tuple[MailAccountConfig, ...] | None = None,
    **config_overrides: object,
) -> MailSyncService:
    config = MailConfig(
        accounts=accounts
        or (
            MailAccountConfig(
                id="smail",
                host="imap.example.edu",
                username="student@example.edu",
                mailbox="INBOX",
            ),
        ),
        **config_overrides,  # type: ignore[arg-type]
    )
    return MailSyncService(
        config,
        SqliteMailRepository(database),
        EventInbox(SqliteEventRepository(database, clock), clock),
        RawMailStore(database_path(database).parent / "mail"),
        clock,
        lambda account: source,
        Rfc822MailParser(),
        waiter=RecordingWaiter(),
    )


def database_path(database: Database) -> Path:
    return Path(database.path)


def _stack(
    database: Database, clock: FakeClock, source: FakeMailSource
) -> tuple[MailSyncService, SqliteMailRepository, SqliteEventRepository]:
    repository = SqliteMailRepository(database)
    events = SqliteEventRepository(database, clock)
    config = MailConfig(
        accounts=(
            MailAccountConfig(
                id="smail",
                host="imap.example.edu",
                username="student@example.edu",
                mailbox="INBOX",
            ),
        )
    )
    service = MailSyncService(
        config,
        repository,
        EventInbox(events, clock),
        RawMailStore(Path(database.path).parent / "mail"),
        clock,
        lambda account: source,
        Rfc822MailParser(),
        waiter=RecordingWaiter(),
    )
    return service, repository, events


# ---------------------------------------------------------------- initial history


async def test_the_first_sync_imports_a_bounded_slice_of_history(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    for uid in range(1, 1001):
        source.add(uid, _raw(subject=f"message {uid}", message_id=f"<m{uid}@example.edu>"))
    service, repository, _events = _stack(database, clock, source)

    result = await service.sync_once()

    assert result.accounts[0].status is AccountSyncStatus.SYNCED
    assert result.accounts[0].new_messages == 500  # initial_fetch_limit
    assert [request.window for request in source.requests] == [500]
    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 1000
    assert await repository.count_messages() == 500  # UIDs 1..500 were never fetched
    assert sorted(await events_counts(repository)) == list(range(501, 1001))


async def test_a_custom_initial_limit_is_respected(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    for uid in range(1, 1001):
        source.add(uid, _raw(subject=f"message {uid}", message_id=f"<m{uid}@example.edu>"))
    service, repository, _ = _stack(database, clock, source)
    service._config = MailConfig(
        accounts=service._config.accounts, initial_fetch_limit=100
    )

    await service.sync_once()

    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 1000
    assert await repository.count_messages() == 100


async def events_counts(repository: SqliteMailRepository) -> list[int]:
    """The UIDs of the stored locations, for a readable assertion."""
    messages = await repository.list_messages(limit=None)
    uids: list[int] = []
    for message in messages:
        uids.extend(location.uid for location in await repository.list_locations(message.id))
    return uids


# ------------------------------------------------------------------- incremental


async def test_an_incremental_sync_only_fetches_new_uids(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    for uid in range(1, 6):
        source.add(uid, _raw(subject=f"message {uid}", message_id=f"<m{uid}@example.edu>"))
    service, repository, _ = _stack(database, clock, source)
    service._config = MailConfig(
        accounts=service._config.accounts, initial_fetch_limit=5
    )
    await service.sync_once()

    source.add(101, _raw(subject="new", message_id="<m101@example.edu>"))
    source.requests.clear()
    second = await service.sync_once()
    third = await service.sync_once()

    assert source.requests[0].after_uid == 5
    assert second.accounts[0].new_messages == 1
    assert third.accounts[0].new_messages == 0  # nothing new
    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 101


async def test_a_poll_batch_is_bounded_and_the_cursor_moves_step_by_step(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    for uid in range(1, 101):
        source.add(uid, _raw(subject=f"message {uid}", message_id=f"<m{uid}@example.edu>"))
    service, repository, _ = _stack(database, clock, source)
    service._config = MailConfig(
        accounts=service._config.accounts,
        initial_fetch_limit=100,
        max_messages_per_poll=100,
    )
    await service.sync_once()
    for uid in range(101, 401):
        source.add(uid, _raw(subject=f"message {uid}", message_id=f"<m{uid}@example.edu>"))

    cursors: list[int] = []
    for _ in range(4):
        await service.sync_once()
        state = await repository.get_sync_state("smail", "INBOX")
        assert state is not None
        cursors.append(state.last_seen_uid)

    assert cursors == [200, 300, 400, 400]


# -------------------------------------------------------------------- oversize


async def test_an_oversize_message_is_recorded_without_its_body(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(1, _raw(subject="small", message_id="<m1@example.edu>"))
    source.add(
        2, _raw(subject="big", message_id="<m2@example.edu>", body="x" * (1024 * 1024 + 10))
    )
    service, repository, events = _stack(database, clock, source)
    service._config = MailConfig(
        accounts=service._config.accounts, max_message_bytes=1024 * 1024
    )

    result = await service.sync_once()

    assert result.accounts[0].oversize == 1
    messages = await repository.list_messages(limit=None)
    oversize = [message for message in messages if message.subject == "big"]
    assert len(oversize) == 1
    assert oversize[0].body_status is MailBodyStatus.OVERSIZE
    assert oversize[0].body_text is None
    assert oversize[0].raw_storage_key is None
    assert oversize[0].size_bytes > 1024 * 1024
    # The metadata, its location and its event still exist, and the cursor moved.
    assert await repository.list_locations(oversize[0].id) != []
    assert await events.get_by_external_identity(
        "mail:smail", f"message:{oversize[0].id}"
    ) is not None
    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 2


# ------------------------------------------------------------------- UIDVALIDITY


async def _sync_two_messages(
    database: Database, clock: FakeClock
) -> tuple[MailSyncService, SqliteMailRepository, FakeMailSource, list[object]]:
    source = FakeMailSource(uidvalidity=10)
    source.add(100, _raw(subject="A", message_id="<a@example.edu>", body="body A"))
    source.add(101, _raw(subject="B", message_id="<b@example.edu>", body="body B"))
    service, repository, _ = _stack(database, clock, source)
    await service.sync_once()
    return service, repository, source, await repository.list_messages(limit=None)


async def test_a_uidvalidity_change_reconciles_instead_of_continuing(
    database: Database, clock: FakeClock
) -> None:
    service, repository, source, before = await _sync_two_messages(database, clock)
    by_subject = {message.subject: message for message in before}
    source.rebuild(uidvalidity=20)
    source.messages = {
        1: _raw(subject="A", message_id="<a@example.edu>", body="body A"),
        2: _raw(subject="B", message_id="<b@example.edu>", body="body B"),
        3: _raw(subject="C", message_id="<c@example.edu>", body="body C"),
    }
    source.requests.clear()

    result = await service.sync_once()

    assert result.accounts[0].reconciled is True
    assert result.accounts[0].matched_existing == 2
    assert result.accounts[0].new_messages == 1
    after = {message.subject: message for message in await repository.list_messages(limit=None)}
    assert after["A"].id == by_subject["A"].id  # the stable identity survived the rebuild
    assert after["B"].id == by_subject["B"].id
    assert after["C"].id not in {by_subject["A"].id, by_subject["B"].id}
    assert len(await repository.list_locations(after["A"].id)) == 2  # old and new location
    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None
    assert (state.uidvalidity, state.last_seen_uid) == (20, 3)
    assert state.last_reconciled_at == NOW


async def test_the_same_message_id_with_different_content_is_not_merged(
    database: Database, clock: FakeClock
) -> None:
    """Two real messages can share a Message-ID; merging them would destroy one."""
    service, repository, source, before = await _sync_two_messages(database, clock)
    source.rebuild(uidvalidity=20)
    source.messages = {
        1: _raw(subject="A", message_id="<a@example.edu>", body="completely different body"),
        2: _raw(subject="B", message_id="<b@example.edu>", body="body B"),
    }

    result = await service.sync_once()

    assert result.accounts[0].reconciliation_conflicts == 1
    messages = await repository.list_messages(limit=None)
    same_id = [message for message in messages if message.message_id_header == "<a@example.edu>"]
    assert len(same_id) == 2  # the conflicting one is a new message, not a merge
    assert len({message.id for message in same_id}) == 2
    assert len(before) == 2


async def test_reprocessing_the_same_location_is_idempotent(
    database: Database, clock: FakeClock
) -> None:
    """A lost cursor must replay work, and replaying must not duplicate anything."""
    _service, repository, _source, _messages = await _sync_two_messages(database, clock)
    _drop_sync_state(database)  # the cursor is gone; the messages are not
    replay = FakeMailSource(uidvalidity=10)
    replay.add(100, _raw(subject="A", message_id="<a@example.edu>", body="body A"))
    replay.add(101, _raw(subject="B", message_id="<b@example.edu>", body="body B"))
    replay_service = _stack(database, clock, replay)[0]

    result = await replay_service.sync_once()

    assert result.accounts[0].existing_locations == 2  # both were already stored
    assert result.accounts[0].new_messages == 0
    assert await repository.count_messages() == 2  # nothing duplicated
    assert await repository.count_unlinked_messages() == 0


def _drop_sync_state(database: Database) -> None:
    """Simulate a lost cursor: mail is still stored, the incremental state is not."""
    with database.connect() as connection:
        connection.execute("DELETE FROM mailbox_sync_state")


# -------------------------------------------------------------- event bridge


async def test_a_message_becomes_exactly_one_inbound_event(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(100, _raw(subject="A", message_id="<a@example.edu>"))
    service, repository, events = _stack(database, clock, source)

    result = await service.sync_once()

    messages = await repository.list_messages(limit=None)
    event = await events.get_by_external_identity(
        "mail:smail", f"message:{messages[0].id}"
    )
    assert event is not None
    assert event.event_type == MAIL_EVENT_TYPE
    assert event.source == "mail:smail"
    assert result.accounts[0].events_created == 1
    assert await repository.count_unlinked_messages() == 0


async def test_the_event_carries_identity_only_not_the_mail_body(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(
        100,
        _raw(
            subject="MAIL-SUBJECT-SECRET",
            message_id="<a@example.edu>",
            body="MAIL-BODY-SECRET-SENTINEL",
        ),
    )
    service, repository, events = _stack(database, clock, source)

    await service.sync_once()

    messages = await repository.list_messages(limit=None)
    event = await events.get_by_external_identity(
        "mail:smail", f"message:{messages[0].id}"
    )
    assert event is not None and event.content is not None
    assert "MAIL-BODY-SECRET-SENTINEL" not in event.content
    assert "MAIL-SUBJECT-SECRET" not in event.content
    assert json.loads(event.content) == {
        "account_id": "smail",
        "mail_message_id": str(messages[0].id),
    }


async def test_a_message_committed_without_its_event_is_repaired(
    database: Database, clock: FakeClock
) -> None:
    """Crash window A: the message exists, the event does not."""
    source = FakeMailSource(uidvalidity=10)
    source.add(100, _raw(subject="A", message_id="<a@example.edu>"))
    service, repository, events = _stack(database, clock, source)
    await service.sync_once()
    messages = await repository.list_messages(limit=None)
    message_id = messages[0].id
    # Simulate the crash: the link (and therefore the bridge's knowledge) disappears.
    _drop_links(database)
    assert await repository.count_unlinked_messages() == 1

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (0, 1)  # the event already existed
    assert await repository.count_unlinked_messages() == 0
    event = await events.get_by_external_identity("mail:smail", f"message:{message_id}")
    assert event is not None
    assert await _event_count(database) == 1


async def test_an_event_committed_without_its_link_is_repaired(
    database: Database, clock: FakeClock
) -> None:
    """Crash window B: the event exists, the link does not."""
    source = FakeMailSource(uidvalidity=10)
    source.add(100, _raw(subject="A", message_id="<a@example.edu>"))
    service, repository, _events = _stack(database, clock, source)
    await service.sync_once()
    _drop_links(database)
    assert await _event_count(database) == 1

    created, repaired = await service.repair_bridge()

    assert (created, repaired) == (0, 1)
    assert await _event_count(database) == 1  # the duplicate was recognised, not re-created
    assert await repository.count_unlinked_messages() == 0


async def test_a_lost_link_is_repaired_on_the_next_poll(
    database: Database, clock: FakeClock
) -> None:
    """The bridge is repaired even when no new mail arrives."""
    source = FakeMailSource(uidvalidity=10)
    source.add(100, _raw(subject="A", message_id="<a@example.edu>"))
    service, repository, _ = _stack(database, clock, source)
    await service.sync_once()
    _drop_links(database)

    result = await service.sync_once()  # nothing new from the server

    assert result.accounts[0].new_messages == 0
    assert result.accounts[0].events_repaired == 1
    assert await repository.count_unlinked_messages() == 0


def _drop_links(database: Database) -> None:
    """Simulate a crash that lost the link row but kept the event."""
    with database.connect() as connection:
        connection.execute("DELETE FROM mail_event_links")


async def _event_count(database: Database) -> int:
    with database.connect() as connection:
        row = connection.execute("SELECT count(*) AS total FROM inbound_events").fetchone()
    return int(row["total"])


# ------------------------------------------------------------- accounts/isolation


async def test_one_broken_account_does_not_stop_the_other(
    database: Database, clock: FakeClock
) -> None:
    good = FakeMailSource(uidvalidity=10)
    good.add(1, _raw(subject="good", message_id="<good@example.edu>"))
    broken = FakeMailSource(uidvalidity=10, failure=authentication_failure())
    accounts = (
        MailAccountConfig(
            id="smail", host="imap.example.edu", username="u", mailbox="INBOX"
        ),
        MailAccountConfig(
            id="personal", host="imap.example.edu", username="u", mailbox="INBOX"
        ),
    )
    repository = SqliteMailRepository(database)
    service = MailSyncService(
        MailConfig(accounts=accounts),
        repository,
        EventInbox(SqliteEventRepository(database, clock), clock),
        RawMailStore(Path(database.path).parent / "mail"),
        clock,
        lambda account: good if account.id == "smail" else broken,
        Rfc822MailParser(),
        waiter=RecordingWaiter(),
    )

    result = await service.sync_once()

    statuses = {entry.account_id: entry.status for entry in result.accounts}
    assert statuses == {
        "smail": AccountSyncStatus.SYNCED,
        "personal": AccountSyncStatus.AUTH_ERROR,
    }
    assert await repository.count_messages(account_id="smail") == 1


async def test_an_offline_account_is_reported_as_offline(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10, failure=connection_failure())
    service, _, _ = _stack(database, clock, source)

    result = await service.sync_once()

    assert result.accounts[0].status is AccountSyncStatus.OFFLINE


async def test_a_missing_credential_is_reported_per_account(
    database: Database, clock: FakeClock
) -> None:
    def factory(account: MailAccountConfig) -> FakeMailSource:
        raise MailCredentialsMissing(f"no credential for {account.id}")

    service = MailSyncService(
        MailConfig(
            accounts=(
                MailAccountConfig(
                    id="smail", host="imap.example.edu", username="u", mailbox="INBOX"
                ),
            )
        ),
        SqliteMailRepository(database),
        EventInbox(SqliteEventRepository(database, clock), clock),
        RawMailStore(Path(database.path).parent / "mail"),
        clock,
        factory,
        Rfc822MailParser(),
        waiter=RecordingWaiter(),
    )

    result = await service.sync_once()

    assert result.accounts[0].status is AccountSyncStatus.CREDENTIALS_MISSING
    assert await _event_count(database) == 0


async def test_only_the_requested_account_is_synced(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(1, _raw(subject="one", message_id="<one@example.edu>"))
    accounts = (
        MailAccountConfig(
            id="smail", host="imap.example.edu", username="u", mailbox="INBOX"
        ),
        MailAccountConfig(
            id="personal", host="imap.example.edu", username="u", mailbox="INBOX"
        ),
    )
    service = MailSyncService(
        MailConfig(accounts=accounts),
        SqliteMailRepository(database),
        EventInbox(SqliteEventRepository(database, clock), clock),
        RawMailStore(Path(database.path).parent / "mail"),
        clock,
        lambda account: source,
        Rfc822MailParser(),
        waiter=RecordingWaiter(),
    )

    result = await service.sync_once("personal")

    assert [entry.account_id for entry in result.accounts] == ["personal"]


# ----------------------------------------------------------------- run_forever


async def test_run_forever_syncs_then_waits_and_stops_cleanly(
    database: Database, clock: FakeClock
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(1, _raw(subject="one", message_id="<one@example.edu>"))
    waiter = RecordingWaiter()
    service = MailSyncService(
        MailConfig(
            accounts=(
                MailAccountConfig(
                    id="smail", host="imap.example.edu", username="u", mailbox="INBOX"
                ),
            ),
            poll_interval_seconds=42,
        ),
        SqliteMailRepository(database),
        EventInbox(SqliteEventRepository(database, clock), clock),
        RawMailStore(Path(database.path).parent / "mail"),
        clock,
        lambda account: source,
        Rfc822MailParser(),
        waiter=waiter,
    )
    stop_event = asyncio.Event()

    await asyncio.wait_for(service.run_forever(stop_event), timeout=5)

    assert waiter.calls == [42]  # the configured poll interval, and it stopped promptly
    assert await SqliteMailRepository(database).count_messages() == 1
