"""Unit tests for EventInbox idempotent ingestion (fake repository, no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.application.event_inbox import (
    EventInbox,
    EventInboxConsistencyError,
    IngestDisposition,
    IngestEvent,
)
from assistant.domain.errors import DuplicateInboundEvent, InvalidInboundEvent
from assistant.domain.inbound_event import EventId, EventStatus, InboundEvent
from tests.support.fakes import (
    CountingEventIdFactory,
    FakeClock,
    FakeEventRepository,
    make_event,
)

FIXED_NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


class _ContradictoryRepository:
    """A repository that reports a duplicate and then cannot read it back.

    Only `add` and `get_by_external_identity` are exercised by these tests.
    """

    def __init__(self) -> None:
        self.external_id_seen: str | None = None

    async def add(self, event: InboundEvent) -> InboundEvent:
        self.external_id_seen = event.external_id
        raise DuplicateInboundEvent(event.source, event.external_id or "")

    async def get(self, event_id: EventId) -> InboundEvent | None:
        return None

    async def get_by_external_identity(
        self, source: str, external_id: str
    ) -> InboundEvent | None:
        return None

    async def list_pending(self, *, limit: int) -> list[InboundEvent]:
        return []

    async def transition(
        self,
        event_id: EventId,
        *,
        expected: EventStatus,
        target: EventStatus,
        error: str | None = None,
    ) -> InboundEvent:
        raise NotImplementedError("unused by these tests")


def _inbox() -> tuple[EventInbox, FakeEventRepository, CountingEventIdFactory]:
    repository = FakeEventRepository()
    ids = CountingEventIdFactory()
    inbox = EventInbox(repository, FakeClock(start=FIXED_NOW), new_id=ids)
    return inbox, repository, ids


async def test_new_external_event_is_created() -> None:
    inbox, repository, _ = _inbox()
    command = IngestEvent(
        source="smail",
        external_id="uidv123:uid456",
        event_type="mail.received",
        content="hello",
    )

    result = await inbox.ingest(command)

    assert result.disposition is IngestDisposition.CREATED
    assert result.event.status is EventStatus.RECEIVED
    assert result.event.attempts == 0
    assert result.event.last_error is None
    assert result.event.received_at == FIXED_NOW
    assert result.event.source == "smail"
    assert result.event.external_id == "uidv123:uid456"
    assert result.event.event_type == "mail.received"
    assert result.event.content == "hello"
    assert repository.events == (result.event,)


async def test_duplicate_external_event_returns_the_first_event() -> None:
    inbox, repository, _ = _inbox()
    command = IngestEvent(source="smail", external_id="uidv123:uid456", event_type="mail.received")

    first = await inbox.ingest(command)
    second = await inbox.ingest(command)

    assert first.disposition is IngestDisposition.CREATED
    assert second.disposition is IngestDisposition.DUPLICATE
    assert first.event.id == second.event.id
    assert len(repository.events) == 1


async def test_same_external_id_from_another_source_is_created() -> None:
    inbox, repository, _ = _inbox()

    first = await inbox.ingest(
        IngestEvent(source="smail", external_id="42", event_type="mail.received")
    )
    second = await inbox.ingest(
        IngestEvent(source="qq", external_id="42", event_type="chat.message")
    )

    assert first.disposition is IngestDisposition.CREATED
    assert second.disposition is IngestDisposition.CREATED
    assert first.event.id != second.event.id
    assert len(repository.events) == 2


async def test_events_without_external_id_are_never_deduplicated() -> None:
    inbox, repository, _ = _inbox()
    command = IngestEvent(source="cli", event_type="local.note", content="提醒我交作业")

    first = await inbox.ingest(command)
    second = await inbox.ingest(command)

    assert first.disposition is IngestDisposition.CREATED
    assert second.disposition is IngestDisposition.CREATED
    assert first.event.id != second.event.id
    assert len(repository.events) == 2


async def test_received_at_comes_from_the_injected_clock() -> None:
    clock = FakeClock(start=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    inbox = EventInbox(FakeEventRepository(), clock)

    result = await inbox.ingest(
        IngestEvent(source="cli", event_type="local.note", content="note")
    )

    assert result.event.received_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def test_ids_come_from_the_injected_factory() -> None:
    repository = FakeEventRepository()
    inbox = EventInbox(
        repository, FakeClock(start=FIXED_NOW), new_id=CountingEventIdFactory()
    )

    first = await inbox.ingest(IngestEvent(source="cli", event_type="local.note"))
    second = await inbox.ingest(IngestEvent(source="cli", event_type="local.note"))

    assert (first.event.id.int, second.event.id.int) == (1, 2)


@pytest.mark.parametrize(
    "command",
    [
        IngestEvent(source="", event_type="local.note"),
        IngestEvent(source="cli", event_type="   "),
        IngestEvent(source="cli", event_type="local.note", external_id="  "),
    ],
)
async def test_invalid_commands_are_rejected_by_the_domain(command: IngestEvent) -> None:
    inbox, repository, _ = _inbox()

    with pytest.raises(InvalidInboundEvent):
        await inbox.ingest(command)

    assert repository.events == ()


async def test_duplicate_without_a_readable_identity_is_a_consistency_error() -> None:
    repository = _ContradictoryRepository()
    inbox = EventInbox(repository, FakeClock(start=FIXED_NOW))

    with pytest.raises(EventInboxConsistencyError, match="could be read back"):
        await inbox.ingest(
            IngestEvent(source="smail", external_id="42", event_type="mail.received")
        )


async def test_duplicate_without_external_identity_is_a_consistency_error() -> None:
    repository = _ContradictoryRepository()
    inbox = EventInbox(repository, FakeClock(start=FIXED_NOW))

    with pytest.raises(EventInboxConsistencyError, match="no external identity"):
        await inbox.ingest(IngestEvent(source="cli", event_type="local.note"))


async def test_ingest_returns_the_stored_representation() -> None:
    repository = FakeEventRepository()
    inbox = EventInbox(repository, FakeClock(start=FIXED_NOW))
    stored = make_event(source="smail", external_id="7")
    await repository.add(stored)

    result = await inbox.ingest(
        IngestEvent(source="smail", external_id="7", event_type="mail.received")
    )

    assert result.disposition is IngestDisposition.DUPLICATE
    assert result.event is stored
