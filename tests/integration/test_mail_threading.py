"""Deterministic threading against real SQLite (ADR-0021).

What is under test is the *decision*: which stored message a reference header actually names,
what happens when it names several, and what happens when it names none. The linker never calls
a model, so none of this needs a provider.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.application.mail_threading import MailThreadLinker, reference_candidates
from assistant.domain.errors import MailMessageNotFound
from assistant.domain.mail_analysis import MailLinkStatus
from assistant.store.db import Database
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_intelligence import DEFAULT_AT, MailStores, build_message


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=DEFAULT_AT)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def stores(database: Database, clock: FakeClock) -> MailStores:
    return MailStores(database, clock)


def _linker(
    stores: MailStores, clock: FakeClock, *, max_depth: int | None = None
) -> MailThreadLinker:
    if max_depth is None:
        return MailThreadLinker(stores.mail, stores.intelligence, clock)
    return MailThreadLinker(stores.mail, stores.intelligence, clock, max_depth=max_depth)


async def test_a_message_with_no_references_is_a_root(
    stores: MailStores, clock: FakeClock
) -> None:
    message = await stores.store(build_message(message_id_header="<a@example.edu>"))

    member = await _linker(stores, clock).ensure_thread(message.id)

    assert member.link_status is MailLinkStatus.ROOT
    assert member.parent_message_id is None
    thread = await stores.intelligence.get_thread(member.thread_id)
    assert thread is not None and thread.account_id == "smail"


async def test_in_reply_to_links_a_reply_to_its_parent(
    stores: MailStores, clock: FakeClock
) -> None:
    parent = await stores.store(build_message(message_id_header="<a@example.edu>"))
    reply = await stores.store(
        build_message(message_id_header="<b@example.edu>", in_reply_to_header="<a@example.edu>")
    )

    parent_member = await _linker(stores, clock).ensure_thread(parent.id)
    reply_member = await _linker(stores, clock).ensure_thread(reply.id)

    assert reply_member.link_status is MailLinkStatus.LINKED
    assert reply_member.parent_message_id == parent.id
    assert reply_member.thread_id == parent_member.thread_id
    assert reply_member.link_evidence == "<a@example.edu>"


async def test_references_are_read_from_the_nearest_ancestor_backwards(
    stores: MailStores, clock: FakeClock
) -> None:
    """`References` is oldest-first; the nearest stored ancestor is the parent."""
    oldest = await stores.store(build_message(message_id_header="<old@example.edu>"))
    await stores.store(build_message(message_id_header="<middle@example.edu>"))
    reply = await stores.store(
        build_message(
            message_id_header="<new@example.edu>",
            references=("<old@example.edu>", "<missing@example.edu>"),
        )
    )

    member = await _linker(stores, clock).ensure_thread(reply.id)

    # The newest reference was never stored, so resolution falls back to the older one.
    assert member.link_status is MailLinkStatus.LINKED
    assert member.parent_message_id == oldest.id


async def test_a_multi_level_chain_lands_in_one_thread(
    stores: MailStores, clock: FakeClock
) -> None:
    first = await stores.store(
        build_message(message_id_header="<1@example.edu>", sent_at=DEFAULT_AT)
    )
    second = await stores.store(
        build_message(
            message_id_header="<2@example.edu>",
            in_reply_to_header="<1@example.edu>",
            sent_at=DEFAULT_AT + timedelta(hours=1),
        )
    )
    third = await stores.store(
        build_message(
            message_id_header="<3@example.edu>",
            in_reply_to_header="<2@example.edu>",
            sent_at=DEFAULT_AT + timedelta(hours=2),
        )
    )

    linker = _linker(stores, clock)
    members = [await linker.ensure_thread(item.id) for item in (third, second, first)]

    assert len({member.thread_id for member in members}) == 1
    thread_id = members[0].thread_id
    messages = await stores.intelligence.list_thread_messages(thread_id)
    assert [message.id for message in messages] == [first.id, second.id, third.id]


async def test_a_duplicate_message_id_makes_the_link_ambiguous(
    stores: MailStores, clock: FakeClock
) -> None:
    """Two stored messages claim the same header: never choose one."""
    first = await stores.store(build_message(message_id_header="<dup@example.edu>"))
    second = await stores.store(build_message(message_id_header="<dup@example.edu>"))
    reply = await stores.store(
        build_message(message_id_header="<r@example.edu>", in_reply_to_header="<dup@example.edu>")
    )

    member = await _linker(stores, clock).ensure_thread(reply.id)

    assert member.link_status is MailLinkStatus.AMBIGUOUS
    assert member.parent_message_id is None
    assert member.link_evidence == "<dup@example.edu>"
    other = await _linker(stores, clock).ensure_thread(first.id)
    assert member.thread_id != other.thread_id
    # Neither duplicate is chosen: the reply sits alone in a thread of its own.
    contents = await stores.intelligence.list_thread_messages(member.thread_id)
    assert [item.id for item in contents] == [reply.id]
    assert second.id != first.id


async def test_a_missing_parent_is_unresolved_not_a_guess(
    stores: MailStores, clock: FakeClock
) -> None:
    reply = await stores.store(
        build_message(
            message_id_header="<r@example.edu>", in_reply_to_header="<never@example.edu>"
        )
    )

    member = await _linker(stores, clock).ensure_thread(reply.id)

    assert member.link_status is MailLinkStatus.UNRESOLVED
    assert member.parent_message_id is None
    assert member.link_evidence == "<never@example.edu>"


async def test_ensuring_the_same_message_twice_returns_the_same_decision(
    stores: MailStores, clock: FakeClock
) -> None:
    parent = await stores.store(build_message(message_id_header="<a@example.edu>"))
    reply = await stores.store(
        build_message(message_id_header="<b@example.edu>", in_reply_to_header="<a@example.edu>")
    )

    linker = _linker(stores, clock)
    first = await linker.ensure_thread(reply.id)
    second = await linker.ensure_thread(reply.id)

    assert first == second
    assert first.thread_id == (await linker.ensure_thread(parent.id)).thread_id
    threads = await stores.intelligence.list_thread_summaries(limit=None)
    assert len(threads) == 1


async def test_a_reference_cycle_terminates_and_keeps_one_thread(
    stores: MailStores, clock: FakeClock
) -> None:
    """Two messages that reference each other must not make the linker loop."""
    first = await stores.store(
        build_message(message_id_header="<a@example.edu>", in_reply_to_header="<b@example.edu>")
    )
    second = await stores.store(
        build_message(message_id_header="<b@example.edu>", in_reply_to_header="<a@example.edu>")
    )

    linker = _linker(stores, clock)
    first_member = await linker.ensure_thread(first.id)
    second_member = await linker.ensure_thread(second.id)

    assert first_member.thread_id == second_member.thread_id
    assert "cycle" in (first_member.link_evidence or "") or "cycle" in (
        second_member.link_evidence or ""
    )
    members = await stores.intelligence.list_thread_members(first_member.thread_id)
    assert len(members) == 2


async def test_a_depth_limit_stops_a_pathological_chain(
    stores: MailStores, clock: FakeClock
) -> None:
    """The walk is bounded: past the limit the message starts its own thread, with a reason."""
    ids = [f"<m{index}@example.edu>" for index in range(6)]
    messages = []
    for index, header in enumerate(ids):
        messages.append(
            await stores.store(
                build_message(
                    message_id_header=header,
                    in_reply_to_header=None if index == 0 else ids[index - 1],
                    sent_at=DEFAULT_AT + timedelta(hours=index),
                )
            )
        )

    linker = _linker(stores, clock, max_depth=2)
    member = await linker.ensure_thread(messages[-1].id)
    root = await stores.intelligence.get_member(messages[3].id)

    assert member.link_status is MailLinkStatus.LINKED
    assert root is not None and root.link_status is MailLinkStatus.ROOT
    assert "depth limit" in (root.link_evidence or "")
    contents = await stores.intelligence.list_thread_messages(member.thread_id)
    assert [item.id for item in contents] == [item.id for item in messages[3:]]


async def test_a_message_never_joins_another_accounts_thread(
    stores: MailStores, clock: FakeClock
) -> None:
    await stores.store(
        build_message(account_id="smail", message_id_header="<a@example.edu>")
    )
    other = await stores.store(
        build_message(
            account_id="personal",
            message_id_header="<b@example.edu>",
            in_reply_to_header="<a@example.edu>",
        )
    )

    member = await _linker(stores, clock).ensure_thread(other.id)

    assert member.link_status is MailLinkStatus.UNRESOLVED
    assert member.parent_message_id is None


async def test_the_linker_refuses_an_unknown_message(
    stores: MailStores, clock: FakeClock
) -> None:
    with pytest.raises(MailMessageNotFound):
        await _linker(stores, clock).ensure_thread(uuid4())


def test_reference_candidates_order_reply_to_before_references() -> None:
    message = build_message(
        in_reply_to_header="<parent@example.edu>",
        references=("<oldest@example.edu>", "<parent@example.edu>", "<newest@example.edu>"),
    )

    assert reference_candidates(message) == (
        "<parent@example.edu>",
        "<newest@example.edu>",
        "<oldest@example.edu>",
    )


async def test_thread_summaries_are_bounded_and_newest_first(
    stores: MailStores, clock: FakeClock
) -> None:
    older = await stores.store(
        build_message(message_id_header="<a@example.edu>", subject="Older", at=DEFAULT_AT)
    )
    newer = await stores.store(
        build_message(
            message_id_header="<b@example.edu>",
            subject="Newer",
            sent_at=DEFAULT_AT + timedelta(hours=1),
        )
    )
    repository: SqliteMailIntelligenceRepository = stores.intelligence
    linker = _linker(stores, clock)
    older_member = await linker.ensure_thread(older.id)
    newer_member = await linker.ensure_thread(newer.id)

    summaries = await repository.list_thread_summaries(limit=1)

    assert len(summaries) == 1
    assert summaries[0].subject_preview == "Newer"
    assert summaries[0].message_count == 1
    assert await repository.resolve_thread_id(str(newer_member.thread_id)[:8]) == (
        newer_member.thread_id
    )
    assert older_member.thread_id != newer_member.thread_id


async def test_a_thread_preview_follows_the_same_clock_as_its_latest_message(
    stores: MailStores, clock: FakeClock
) -> None:
    """A message with no usable Date falls back to first-seen, in the preview too."""
    dated = await stores.store(
        build_message(
            message_id_header="<a@example.edu>", subject="Dated", sent_at=DEFAULT_AT
        )
    )
    undated = await stores.store(
        build_message(
            message_id_header="<b@example.edu>",
            in_reply_to_header="<a@example.edu>",
            subject="Undated",
            sent_at=None,
            at=DEFAULT_AT + timedelta(hours=3),
        )
    )
    linker = _linker(stores, clock)
    await linker.ensure_thread(dated.id)
    member = await linker.ensure_thread(undated.id)

    summaries = await stores.intelligence.list_thread_summaries(limit=None)

    assert len(summaries) == 1
    assert summaries[0].message_count == 2
    # First-seen (three hours later) is the thread's clock, so the undated message is the latest.
    assert summaries[0].latest_at == DEFAULT_AT + timedelta(hours=3)
    assert summaries[0].subject_preview == "Undated"
    assert member.thread_id == summaries[0].thread.id
