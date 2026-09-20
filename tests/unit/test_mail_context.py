"""The bounded mail context: what reaches the model and what cannot (ADR-0021)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from assistant.application.mail_context import (
    MAX_CONTEXT_CHARS_PER_MESSAGE,
    MAX_CONTEXT_CHARS_TOTAL,
    MAX_THREAD_CONTEXT_MESSAGES,
    MailContextBuilder,
)
from assistant.store.db import Database
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_intelligence import DEFAULT_AT, MailStores, build_message
from tests.support.mail_intelligence import build_message as _build


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


async def _threaded(stores: MailStores, clock: FakeClock, bodies: list[str]):
    """Store a chain of messages and return `(thread_id, messages)`."""
    from assistant.application.mail_threading import MailThreadLinker

    linker = MailThreadLinker(stores.mail, stores.intelligence, clock)
    messages = []
    for index, body in enumerate(bodies):
        header = f"<m{index}@example.edu>"
        messages.append(
            await stores.store(
                _build(
                    message_id_header=header,
                    in_reply_to_header=None if index == 0 else f"<m{index - 1}@example.edu>",
                    body_text=body,
                    subject=f"Message {index}",
                    sent_at=DEFAULT_AT + timedelta(hours=index),
                )
            )
        )
    member = await linker.ensure_thread(messages[-1].id)
    return member.thread_id, messages


async def test_the_context_holds_the_current_message_and_recent_history(
    stores: MailStores, clock: FakeClock
) -> None:
    thread_id, messages = await _threaded(stores, clock, ["one", "two", "three"])
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    context = await builder.build(messages[-1], thread_id)

    assert context.current.body_text == "three"
    assert [item.body_text for item in context.previous] == ["one", "two"]
    assert context.to_payload()["current_message"]["subject"] == "Message 2"
    assert context.thread_message_ids == tuple(item.id for item in messages)


async def test_only_the_most_recent_messages_reach_the_model(
    stores: MailStores, clock: FakeClock
) -> None:
    bodies = [f"body {index}" for index in range(MAX_THREAD_CONTEXT_MESSAGES + 4)]
    thread_id, messages = await _threaded(stores, clock, bodies)
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    context = await builder.build(messages[-1], thread_id)

    assert len(context.previous) == MAX_THREAD_CONTEXT_MESSAGES
    assert [item.body_text for item in context.previous] == [
        f"body {index}"
        for index in range(len(bodies) - 1 - MAX_THREAD_CONTEXT_MESSAGES, len(bodies) - 1)
    ]
    assert context.context_truncated is True
    # The whole thread is still recorded, so the fingerprint notices a growing context.
    assert len(context.thread_message_ids) == len(bodies)


async def test_a_long_body_is_cut_at_the_per_message_budget(
    stores: MailStores, clock: FakeClock
) -> None:
    thread_id, messages = await _threaded(stores, clock, ["x" * 5000, "reply"])
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    context = await builder.build(messages[0], thread_id)

    assert len(context.current.body_text or "") == MAX_CONTEXT_CHARS_PER_MESSAGE
    assert context.current.body_truncated is True
    assert context.context_truncated is True


async def test_the_total_budget_stops_history_early(
    stores: MailStores, clock: FakeClock
) -> None:
    bodies = ["y" * MAX_CONTEXT_CHARS_PER_MESSAGE for _ in range(6)]
    thread_id, messages = await _threaded(stores, clock, bodies)
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    context = await builder.build(messages[-1], thread_id)

    total = len(context.current.body_text or "") + sum(
        len(item.body_text or "") for item in context.previous
    )
    assert total <= MAX_CONTEXT_CHARS_TOTAL
    assert context.context_truncated is True


async def test_the_payload_carries_no_recipients_no_paths_and_no_raw_message(
    stores: MailStores, clock: FakeClock
) -> None:
    thread_id, messages = await _threaded(stores, clock, ["hello"])
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    payload = (await builder.build(messages[0], thread_id)).to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert sorted(payload) == [
        "context_truncated",
        "current_message",
        "planning_timezone",
        "thread_message_count",
        "thread_messages",
    ]
    assert sorted(payload["current_message"]) == [
        "body_text",
        "body_truncated",
        "from_address",
        "message_id",
        "sent_at",
        "subject",
    ]
    for forbidden in (
        "to_addresses",
        "cc_addresses",
        "raw_sha256",
        "raw_storage_key",
        "uidvalidity",
        "attachment",
        "@example.edu>",
    ):
        assert forbidden not in encoded, f"the context must not carry {forbidden}"


async def test_the_same_state_produces_the_same_request_bytes(
    stores: MailStores, clock: FakeClock
) -> None:
    thread_id, messages = await _threaded(stores, clock, ["one", "two"])
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    first = (await builder.build(messages[-1], thread_id)).to_json()
    clock.advance(3600)
    second = (await builder.build(messages[-1], thread_id)).to_json()

    assert first == second


async def test_a_message_without_a_stored_body_stays_null(
    stores: MailStores, clock: FakeClock
) -> None:
    from assistant.domain.mail import MailBodyStatus

    oversized = await stores.store(
        build_message(message_id_header="<a@example.edu>", body_status=MailBodyStatus.OVERSIZE)
    )
    reply = await stores.store(
        build_message(
            message_id_header="<b@example.edu>", in_reply_to_header="<a@example.edu>"
        )
    )
    from assistant.application.mail_threading import MailThreadLinker

    member = await MailThreadLinker(stores.mail, stores.intelligence, clock).ensure_thread(
        reply.id
    )
    builder = MailContextBuilder(stores.mail, stores.intelligence)

    context = await builder.build(reply, member.thread_id)

    assert context.previous[0].message_id == oversized.id
    assert context.previous[0].body_text is None
    assert context.previous[0].body_truncated is False


async def test_the_builder_refuses_an_impossible_budget(
    stores: MailStores,
) -> None:
    repository: SqliteMailIntelligenceRepository = stores.intelligence
    with pytest.raises(ValueError):
        MailContextBuilder(stores.mail, repository, total_chars=10)
    with pytest.raises(ValueError):
        MailContextBuilder(stores.mail, repository, max_previous=-1)
