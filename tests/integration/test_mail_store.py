"""Real-SQLite mail repository: identity, idempotency and the batch transaction (ADR-0020)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.application.event_inbox import EventInbox, IngestEvent
from assistant.domain.errors import AmbiguousId, MailMessageNotFound
from assistant.domain.mail import (
    MailAttachmentMetadata,
    MailBodyStatus,
    MailboxSyncMode,
    MailMessage,
    MailMessageLocation,
    new_mail_message_id,
)
from assistant.ports.mail_repository import FetchedMail
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def repository(database: Database) -> SqliteMailRepository:
    return SqliteMailRepository(database)


def _message(
    *,
    account_id: str = "smail",
    subject: str = "Subject",
    fingerprint: str = FINGERPRINT,
    message_id_header: str | None = "<a@example.edu>",
    sent_at: datetime | None = NOW,
    body_status: MailBodyStatus = MailBodyStatus.AVAILABLE,
    raw_sha256: str | None = "b" * 64,
    message_id: object | None = None,
) -> MailMessage:
    return MailMessage(
        id=message_id or new_mail_message_id(),  # type: ignore[arg-type]
        account_id=account_id,
        message_id_header=message_id_header,
        subject=subject,
        from_address="ada@example.edu",
        to_addresses=("me@example.edu",),
        date_header="Fri, 23 Oct 2026 23:59:00 +0800",
        sent_at=sent_at,
        body_text="body text" if body_status is MailBodyStatus.AVAILABLE else None,
        body_status=body_status,
        raw_sha256=raw_sha256,
        raw_storage_key=(
            f"mail/raw/bb/{raw_sha256}.eml" if raw_sha256 is not None else None
        ),
        content_fingerprint=fingerprint,
        size_bytes=1200,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _fetched(
    *,
    account_id: str = "smail",
    uidvalidity: int = 10,
    uid: int = 100,
    message: MailMessage | None = None,
    attachments: tuple[MailAttachmentMetadata, ...] = (),
) -> FetchedMail:
    stored = message or _message(account_id=account_id)
    return FetchedMail(
        message=stored,
        location=MailMessageLocation(
            message_id=stored.id,
            account_id=account_id,
            mailbox_name="INBOX",
            uidvalidity=uidvalidity,
            uid=uid,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        attachments=attachments,
    )


async def _apply(
    repository: SqliteMailRepository,
    *messages: FetchedMail,
    uidvalidity: int = 10,
    cursor: int | None = None,
    mode: MailboxSyncMode = MailboxSyncMode.NORMAL,
    reconciled: bool = False,
):
    return await repository.apply_fetched_batch(
        account_id="smail",
        mailbox_name="INBOX",
        uidvalidity=uidvalidity,
        last_seen_uid=cursor if cursor is not None else max(
            (item.location.uid for item in messages), default=0
        ),
        messages=messages,
        mode=mode,
        at=NOW,
        reconciled=reconciled,
    )


# ------------------------------------------------------------------ persistence


async def test_a_fetched_message_round_trips(repository: SqliteMailRepository) -> None:
    fetched = _fetched()

    applied = await _apply(repository, fetched)
    stored = await repository.get_message(fetched.message.id)

    assert applied.created_messages == 1
    assert stored == fetched.message
    assert await repository.list_locations(fetched.message.id) == [fetched.location]


async def test_persistence_survives_reopen(database: Database) -> None:
    first = SqliteMailRepository(database)
    fetched = _fetched()
    await _apply(first, fetched)

    reopened = SqliteMailRepository(Database.at(database.path))

    assert (await reopened.get_message(fetched.message.id)) == fetched.message
    state = await reopened.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 100


async def test_the_same_location_is_never_stored_twice(
    repository: SqliteMailRepository,
) -> None:
    fetched = _fetched()

    first = await _apply(repository, fetched)
    second = await _apply(repository, fetched)

    assert (first.created_messages, first.existing_locations) == (1, 0)
    assert (second.created_messages, second.existing_locations) == (0, 1)
    assert len(await repository.list_locations(fetched.message.id)) == 1
    assert await repository.count_messages() == 1


async def test_one_message_can_have_many_locations(
    repository: SqliteMailRepository,
) -> None:
    """A rebuilt mailbox re-uses the stable message and adds a new location."""
    message = _message()
    await _apply(repository, _fetched(uidvalidity=10, uid=100, message=message))
    await _apply(
        repository,
        _fetched(uidvalidity=20, uid=1, message=message),
        uidvalidity=20,
        cursor=1,
        mode=MailboxSyncMode.RECONCILING,
        reconciled=True,
    )

    locations = await repository.list_locations(message.id)
    assert [location.identity for location in locations] == [
        ("smail", "INBOX", 10, 100),
        ("smail", "INBOX", 20, 1),
    ]
    assert await repository.count_messages() == 1


async def test_attachments_are_stored_with_their_message(
    repository: SqliteMailRepository,
) -> None:
    message = _message()
    attachments = (
        MailAttachmentMetadata(
            message_id=message.id,
            ordinal=0,
            filename="report.pdf",
            content_type="application/pdf",
            content_disposition="attachment",
            size_bytes=10,
            sha256="c" * 64,
        ),
        MailAttachmentMetadata(
            message_id=message.id, ordinal=1, size_bytes=20, sha256="d" * 64
        ),
    )

    await _apply(repository, _fetched(message=message, attachments=attachments))

    assert await repository.list_attachments(message.id) == list(attachments)


async def test_the_cursor_advances_with_the_batch(repository: SqliteMailRepository) -> None:
    await _apply(repository, _fetched(uid=100), cursor=100)

    state = await repository.get_sync_state("smail", "INBOX")

    assert state is not None
    assert (state.uidvalidity, state.last_seen_uid) == (10, 100)
    assert state.next_uid_to_fetch() == 101
    assert state.last_sync_at == NOW
    assert state.last_reconciled_at is None


async def test_a_reconciled_batch_records_when_it_reconciled(
    repository: SqliteMailRepository,
) -> None:
    await _apply(
        repository,
        _fetched(uidvalidity=20),
        uidvalidity=20,
        mode=MailboxSyncMode.RECONCILING,
        reconciled=True,
    )

    state = await repository.get_sync_state("smail", "INBOX")

    assert state is not None
    assert state.mode is MailboxSyncMode.RECONCILING
    assert state.last_reconciled_at == NOW


async def test_a_failed_batch_leaves_no_trace(
    repository: SqliteMailRepository, database: Database
) -> None:
    """The cursor never advances past mail the database did not store."""
    first = _fetched(uid=100)
    await _apply(repository, first, cursor=100)
    duplicate_attachment_message = _message()
    broken = FetchedMail(
        message=duplicate_attachment_message,
        location=MailMessageLocation(
            message_id=duplicate_attachment_message.id,
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=10,
            uid=101,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        attachments=(
            MailAttachmentMetadata(
                message_id=duplicate_attachment_message.id,
                ordinal=0,
                size_bytes=1,
                sha256="e" * 64,
            ),
            MailAttachmentMetadata(
                message_id=duplicate_attachment_message.id,
                ordinal=0,  # duplicate ordinal: the UNIQUE constraint fires
                size_bytes=1,
                sha256="f" * 64,
            ),
        ),
    )

    from assistant.store.errors import CommitmentStoreError

    with pytest.raises(CommitmentStoreError):
        await _apply(repository, broken, cursor=101)

    state = await repository.get_sync_state("smail", "INBOX")
    assert state is not None and state.last_seen_uid == 100
    assert await repository.get_message(duplicate_attachment_message.id) is None
    assert await repository.count_messages() == 1


# ------------------------------------------------------------------- queries


async def test_messages_are_listed_newest_first_and_can_be_filtered(
    repository: SqliteMailRepository,
) -> None:
    newer = _message(subject="newer", sent_at=NOW + timedelta(hours=1), message_id=uuid4())
    older = _message(subject="older", sent_at=NOW, message_id=uuid4())
    undated = _message(subject="undated", sent_at=None, message_id=uuid4())
    other_account = _message(
        subject="other account", account_id="personal", message_id=uuid4()
    )
    await _apply(
        repository,
        _fetched(uid=100, message=older),
        _fetched(uid=101, message=newer),
        _fetched(uid=102, message=undated),
    )
    await repository.apply_fetched_batch(
        account_id="personal",
        mailbox_name="INBOX",
        uidvalidity=1,
        last_seen_uid=1,
        messages=(_fetched(account_id="personal", uid=1, message=other_account),),
        mode=MailboxSyncMode.NORMAL,
        at=NOW,
    )

    listed = await repository.list_messages(limit=10)
    scoped = await repository.list_messages(account_id="smail", limit=10)

    subjects = [message.subject for message in listed]
    assert set(subjects) == {"newer", "older", "undated", "other account"}
    assert subjects[0] == "newer"  # newest `sent_at` first
    assert subjects[-1] == "undated"  # a message with no Date sorts last
    # Within one account the remaining order is deterministic as well.
    assert [message.subject for message in scoped] == ["newer", "older", "undated"]
    assert await repository.count_messages(account_id="personal") == 1


async def test_reconciliation_candidates_are_found_by_evidence(
    repository: SqliteMailRepository,
) -> None:
    message = _message(message_id_header="<same@example.edu>")
    await _apply(repository, _fetched(uid=100, message=message))

    by_raw = await repository.find_reconciliation_candidates(
        account_id="smail",
        message_id_header=None,
        content_fingerprint="z" * 64,
        raw_sha256="b" * 64,
    )
    by_header = await repository.find_reconciliation_candidates(
        account_id="smail",
        message_id_header="<same@example.edu>",
        content_fingerprint="z" * 64,
        raw_sha256=None,
    )
    by_fingerprint = await repository.find_reconciliation_candidates(
        account_id="smail",
        message_id_header=None,
        content_fingerprint=FINGERPRINT,
        raw_sha256=None,
    )

    assert by_raw.by_raw_sha256 == message
    assert by_header.by_message_id == (message,)
    assert by_fingerprint.by_fingerprint == (message,)


async def test_duplicate_message_ids_are_allowed_in_storage(
    repository: SqliteMailRepository,
) -> None:
    """Message-ID is evidence, not an identity: two rows may share it."""
    first = _message(subject="first", fingerprint="1" * 64, raw_sha256="2" * 64)
    second = _message(subject="second", fingerprint="3" * 64, raw_sha256="4" * 64)

    await _apply(
        repository, _fetched(uid=100, message=first), _fetched(uid=101, message=second)
    )
    candidates = await repository.find_reconciliation_candidates(
        account_id="smail",
        message_id_header="<a@example.edu>",
        content_fingerprint="1" * 64,
        raw_sha256=None,
    )

    assert await repository.count_messages() == 2
    assert {message.id for message in candidates.by_message_id} == {first.id, second.id}


# --------------------------------------------------------------- event bridge


async def test_unlinked_messages_are_listed_and_can_be_linked(
    repository: SqliteMailRepository, database: Database, clock: FakeClock
) -> None:
    message = _message()
    await _apply(repository, _fetched(message=message))
    event = await _real_event(database, clock, message_id=message.id)

    unlinked = await repository.list_unlinked_messages(limit=10)
    await repository.link_inbound_event(
        mail_message_id=message.id, inbound_event_id=event, linked_at=NOW
    )

    assert [item.id for item in unlinked] == [message.id]
    assert await repository.list_unlinked_messages(limit=10) == []
    assert await repository.count_unlinked_messages() == 0


async def test_linking_twice_is_a_no_op(
    repository: SqliteMailRepository, database: Database, clock: FakeClock
) -> None:
    message = _message()
    await _apply(repository, _fetched(message=message))
    event_id = await _real_event(database, clock, message_id=message.id)

    await repository.link_inbound_event(
        mail_message_id=message.id, inbound_event_id=event_id, linked_at=NOW
    )
    await repository.link_inbound_event(
        mail_message_id=message.id, inbound_event_id=event_id, linked_at=NOW
    )

    assert await repository.count_unlinked_messages() == 0


async def _real_event(database: Database, clock: FakeClock, *, message_id: object) -> UUID:
    """A real `InboundEvent`, because the link's foreign key points at one."""
    result = await EventInbox(
        SqliteEventRepository(database, clock), clock
    ).ingest(
        IngestEvent(
            source="mail:smail",
            event_type="mail.message.received",
            external_id=f"message:{message_id}",
        )
    )
    return result.event.id


# ------------------------------------------------------------ id resolution


async def test_a_message_id_resolves_by_full_id_or_unique_prefix(
    repository: SqliteMailRepository,
) -> None:
    message = _message()
    await _apply(repository, _fetched(message=message))

    assert await repository.resolve_message_id(str(message.id)) == message.id
    assert await repository.resolve_message_id(str(message.id)[:8]) == message.id
    with pytest.raises(MailMessageNotFound):
        await repository.resolve_message_id("ffffffff")
    with pytest.raises(MailMessageNotFound):
        await repository.resolve_message_id("   ")


async def test_an_ambiguous_prefix_is_never_guessed(
    repository: SqliteMailRepository,
) -> None:
    shared_prefix = UUID("11111111-1111-4111-8111-111111111111")
    other = UUID("11111111-1111-4111-8111-222222222222")
    await _apply(
        repository,
        _fetched(uid=100, message=_message(message_id=shared_prefix, raw_sha256="5" * 64)),
        _fetched(uid=101, message=_message(message_id=other, raw_sha256="6" * 64)),
    )

    with pytest.raises(AmbiguousId):
        await repository.resolve_message_id("11111111")
