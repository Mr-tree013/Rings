"""The queue inside the operational surfaces (Phase 11A, ADR-0041 §79-§81).

A queued message is authoritative durable intent until it becomes a turn, so it has to be checked
like one and backed up like one — and a `PROCESSING` row that comes back from a restored archive
must be resolved, never replayed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from assistant.application.conversation_event_broker import ConversationEventBroker
from assistant.application.conversation_request_coordinator import (
    ConversationRequestCoordinator,
)
from assistant.domain.conversation_request import (
    ConversationRequest,
    ConversationRequestStatus,
)
from assistant.domain.integrity import IntegritySeverity
from assistant.store.conversation_requests import SqliteConversationRequestRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.ops import RuntimeFixture


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeFixture:
    return RuntimeFixture(tmp_path / "data" / "growing-assistant")


async def _queue_one(
    runtime: RuntimeFixture, *, client_id: str = "integrity-request-0001"
) -> ConversationRequest:
    """One accepted request on the runtime's own database."""
    conversations = SqliteConversationRepository(runtime.database)
    requests = SqliteConversationRequestRepository(runtime.database)
    now = runtime.clock.now()
    thread = await conversations.add_thread(_thread_at(now))
    stored, _ = await requests.accept(
        ConversationRequest(
            thread_id=thread.id,
            client_request_id=client_id,
            input_text="我今天有什么事？",
            created_at=now,
        )
    )
    return stored


def _thread_at(now):
    from assistant.domain.conversation import ConversationThread

    return ConversationThread(created_at=now, updated_at=now)


async def test_a_healthy_runtime_with_a_queued_request_passes(runtime: RuntimeFixture) -> None:
    await runtime.add_task()
    await _queue_one(runtime)

    report = await runtime.integrity_service().check()

    assert report.passed is True
    assert report.section("conversation").severity is IntegritySeverity.OK


async def test_an_active_request_without_a_stage_is_reported(runtime: RuntimeFixture) -> None:
    """Read-only, and never repaired: the finding names the row and nothing about its content."""
    stored = await _queue_one(runtime)
    with runtime.database.connect() as connection:
        connection.execute(
            "UPDATE conversation_requests SET stage = NULL, status = 'processing', "
            "started_at = ? WHERE id = ?",
            (runtime.clock.now().isoformat(), str(stored.id)),
        )

    report = await runtime.integrity_service().check()

    section = report.section("conversation")
    assert section.severity is IntegritySeverity.CRITICAL
    assert any(str(stored.id) in finding for finding in section.findings)
    assert "我今天有什么事" not in " ".join(section.findings)


async def test_a_request_that_lost_its_thread_is_critical(runtime: RuntimeFixture) -> None:
    stored = await _queue_one(runtime)
    with runtime.database.connect() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM conversation_threads")

    report = await runtime.integrity_service().check()

    assert report.section("conversation").severity is IntegritySeverity.CRITICAL
    assert any(str(stored.id) in item for item in report.section("conversation").findings)


async def test_a_queued_request_survives_a_backup_and_restore(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """Queued intent is part of the archive, and comes back claimable."""
    stored = await _queue_one(runtime, client_id="backup-request-0001")
    archive = tmp_path / "backup.gab"
    runtime.backup_service().create(archive)
    destination = tmp_path / "restored"

    runtime.backup_service().restore(archive, destination)

    restored = SqliteConversationRequestRepository(
        _database_at(destination / "assistant.db", runtime)
    )
    row = await restored.get(stored.id)
    assert row is not None
    assert row.status is ConversationRequestStatus.QUEUED
    assert row.input_text == "我今天有什么事？"
    claimed = await restored.claim_next(at=runtime.clock.now())
    assert claimed is not None and claimed.id == stored.id


async def test_a_processing_request_restores_as_durable_intent_and_is_never_replayed(
    runtime: RuntimeFixture, tmp_path
) -> None:
    """The archive keeps the row, and the first start after a restore resolves it fail-closed."""
    stored = await _queue_one(runtime, client_id="restored-processing-01")
    requests = SqliteConversationRequestRepository(runtime.database)
    claimed = await requests.claim_next(at=runtime.clock.now(), thread_id=stored.thread_id)
    assert claimed is not None
    archive = tmp_path / "backup.gab"
    runtime.backup_service().create(archive)
    destination = tmp_path / "restored"
    runtime.backup_service().restore(archive, destination)
    database = _database_at(destination / "assistant.db", runtime)
    restored = SqliteConversationRequestRepository(database)

    row = await restored.get(stored.id)
    assert row is not None and row.status is ConversationRequestStatus.PROCESSING
    resolved = await restored.recover(at=runtime.clock.now() + timedelta(hours=1))
    assert resolved[0].status is ConversationRequestStatus.INTERRUPTED
    assert await restored.claim_next(at=runtime.clock.now()) is None


async def test_the_coordinator_recovers_a_restored_runtime_without_rerunning(
    runtime: RuntimeFixture,
) -> None:
    """The daemon's own recovery path: interrupted work is resolved, queued work remains queued."""
    interrupted = await _queue_one(runtime, client_id="recover-processing-01")
    waiting = await _queue_one(runtime, client_id="recover-queued-000001")
    requests = SqliteConversationRequestRepository(runtime.database)
    assert (
        await requests.claim_next(at=runtime.clock.now(), thread_id=interrupted.thread_id)
    ) is not None
    coordinator = ConversationRequestCoordinator(
        requests=requests,
        conversation=_UnusedConversation(),  # type: ignore[arg-type]
        threads=SqliteConversationRepository(runtime.database),
        broker=ConversationEventBroker(),
        clock=runtime.clock,
    )

    resolved = await coordinator.recover()

    assert [row.id for row in resolved] == [interrupted.id]
    assert resolved[0].status is ConversationRequestStatus.INTERRUPTED
    still_queued = await requests.get(waiting.id)
    assert still_queued is not None
    assert still_queued.status is ConversationRequestStatus.QUEUED
    # Recovery started a worker for the queued thread; shut it down deterministically.
    assert coordinator._workers
    await coordinator.shutdown()


class _UnusedConversation:
    """A conversation runtime that must never be reached by recovery."""

    async def recover_interrupted(self) -> tuple[str, ...]:
        return ()

    async def send(self, thread_id, text, *, runtime=None):  # pragma: no cover - never called
        raise AssertionError("recovery must not run a conversation turn")


def _database_at(path: Path, runtime: RuntimeFixture):
    from assistant.store.db import Database

    database = Database.at(path)
    clock = FakeClock(start=runtime.clock.now())
    apply_migrations(database, clock=clock)
    return database
