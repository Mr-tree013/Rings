"""Draft persistence against real SQLite: provenance, ordering and compare-and-set edits."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from assistant.domain.errors import (
    AmbiguousId,
    MailDraftNotFound,
    StaleMailDraftUpdate,
)
from assistant.domain.knowledge import SourceSpan
from assistant.domain.mail_draft import MailDraftOrigin, MailDraftSource
from assistant.store.db import Database
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_drafts import NOW, build_draft, make_evidence
from tests.support.mail_intelligence import MailStores, build_message


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def drafts(database: Database) -> SqliteMailDraftRepository:
    return SqliteMailDraftRepository(database)


@pytest.fixture
def stores(database: Database, clock: FakeClock) -> MailStores:
    return MailStores(database, clock)


async def _stored_message(stores: MailStores) -> UUID:
    message = await stores.store(build_message(message_id_header="<a@example.edu>"))
    return message.id


def _source(draft_id: UUID, evidence_position: int = 1, ordinal: int = 0) -> MailDraftSource:
    evidence = make_evidence(position=evidence_position)
    return MailDraftSource(
        draft_id=draft_id,
        root_id=evidence.root_id,
        entry_id=evidence.entry_id,
        chunk_id=evidence.chunk_id,
        logical_uri=evidence.logical_uri,
        source_span=evidence.source_span,
        ordinal=ordinal,
    )


async def test_a_draft_and_its_sources_are_stored_together(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    message_id = await _stored_message(stores)
    draft = build_draft(reply_to_message_id=message_id, needs_user_input=("Which course?",))

    stored = await drafts.add_draft(draft, (_source(draft.id), _source(draft.id, 2, 1)))

    assert stored == draft
    assert await drafts.get_draft(draft.id) == draft
    assert await drafts.count_drafts() == 1
    sources = await drafts.list_sources(draft.id)
    assert [source.ordinal for source in sources] == [0, 1]
    assert sources[0].source_span == SourceSpan.lines(18, 31)
    assert await drafts.count_sources(draft.id) == 2


async def test_a_draft_without_knowledge_stores_no_sources(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    message_id = await _stored_message(stores)
    draft = build_draft(reply_to_message_id=message_id)

    await drafts.add_draft(draft)

    assert await drafts.list_sources(draft.id) == []
    assert await drafts.list_drafts(limit=None) == [draft]


async def test_drafts_are_listed_newest_update_first(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    message_id = await _stored_message(stores)
    older = build_draft(reply_to_message_id=message_id, at=NOW, subject="Re: older")
    newer = build_draft(
        reply_to_message_id=message_id, at=NOW + timedelta(hours=1), subject="Re: newer"
    )
    await drafts.add_draft(older)
    await drafts.add_draft(newer)

    listed = await drafts.list_drafts(limit=1)

    assert [draft.subject for draft in listed] == ["Re: newer"]
    assert len(await drafts.list_drafts(limit=None)) == 2


async def test_an_edit_is_a_compare_and_set(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    message_id = await _stored_message(stores)
    draft = build_draft(reply_to_message_id=message_id)
    await drafts.add_draft(draft)
    edited = build_draft(
        reply_to_message_id=message_id,
        body_text="Edited by hand.",
        origin=MailDraftOrigin.USER_EDITED,
        version=2,
        at=NOW + timedelta(minutes=5),
    )

    updated = await drafts.update_draft(_with_id(edited, draft.id), expected_version=1)

    assert updated.version == 2
    assert updated.origin is MailDraftOrigin.USER_EDITED
    assert updated.body_text == "Edited by hand."
    assert updated.created_at == draft.created_at
    with pytest.raises(StaleMailDraftUpdate):
        await drafts.update_draft(_with_id(edited, draft.id), expected_version=1)


async def test_editing_an_unknown_draft_says_so(
    drafts: SqliteMailDraftRepository,
) -> None:
    from uuid import uuid4

    with pytest.raises(MailDraftNotFound):
        await drafts.update_draft(build_draft(), expected_version=1)
    with pytest.raises(MailDraftNotFound):
        await drafts.resolve_draft_id(str(uuid4()))


async def test_an_edit_cannot_change_the_recipients(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    """The recipient is a header decision, not an editable field."""
    message_id = await _stored_message(stores)
    draft = build_draft(reply_to_message_id=message_id)
    await drafts.add_draft(draft)
    hijacked = _with_id(
        build_draft(
            reply_to_message_id=message_id,
            to_addresses=("attacker@example.com",),
            version=2,
            at=NOW + timedelta(minutes=1),
        ),
        draft.id,
    )

    await drafts.update_draft(hijacked, expected_version=1)

    stored = await drafts.get_draft(draft.id)
    assert stored is not None
    assert stored.to_addresses == ("ada@example.edu",)


async def test_draft_ids_resolve_by_full_value_and_unique_prefix(
    drafts: SqliteMailDraftRepository, stores: MailStores
) -> None:
    message_id = await _stored_message(stores)
    first = _with_id(build_draft(reply_to_message_id=message_id), UUID(int=1))
    second = _with_id(build_draft(reply_to_message_id=message_id), UUID(int=2))
    await drafts.add_draft(first)
    await drafts.add_draft(second)

    assert await drafts.resolve_draft_id(str(first.id)) == first.id
    with pytest.raises(AmbiguousId):
        await drafts.resolve_draft_id("0000")


def _with_id(draft: object, draft_id: UUID):
    from dataclasses import replace

    return replace(draft, id=draft_id)  # type: ignore[arg-type]
