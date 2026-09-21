"""The durable accepted-input queue, at the store layer (Phase 11A, ADR-0041 §4-§10).

What is under test here is the storage contract the coordinator relies on: idempotent accept, the
atomic claim that gives one thread at most one active request, FIFO order, fail-closed restart
recovery, and the point at which the user's words stop being duplicated.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.conversation import (
    ConversationMessage,
    ConversationMessageRole,
    ConversationThread,
    ConversationTurn,
    ConversationTurnStatus,
)
from assistant.domain.conversation_request import (
    ConversationProgressStage,
    ConversationRequest,
    ConversationRequestStatus,
)
from assistant.domain.errors import (
    ConversationRequestNotFound,
    InvalidConversationRequest,
)
from assistant.store.conversation_requests import SqliteConversationRequestRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
CLIENT_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def requests(database: Database) -> SqliteConversationRequestRepository:
    return SqliteConversationRequestRepository(database)


@pytest.fixture
def conversations(database: Database) -> SqliteConversationRepository:
    return SqliteConversationRepository(database)


async def _thread(
    conversations: SqliteConversationRepository, *, at: datetime = NOW
) -> ConversationThread:
    return await conversations.add_thread(ConversationThread(created_at=at, updated_at=at))


def _request(thread_id: UUID, *, client_id: str = CLIENT_ID, text: str = "我今天有什么事？"):
    return ConversationRequest(
        thread_id=thread_id,
        client_request_id=client_id,
        input_text=text,
        created_at=NOW,
    )


async def _make_turn(
    conversations: SqliteConversationRepository,
    thread_id: UUID,
    *,
    status: ConversationTurnStatus = ConversationTurnStatus.PLANNED,
) -> ConversationTurn:
    """Store one user message and the turn that owns it, the way the runtime does."""
    message = await conversations.add_message(
        ConversationMessage(
            thread_id=thread_id,
            role=ConversationMessageRole.USER,
            text="一条消息",
            created_at=NOW,
        )
    )
    stored = await conversations.add_turn(
        ConversationTurn(
            thread_id=thread_id,
            user_message_id=message.id,
            interpreter_version="test",
            context_fingerprint="a" * 64,
            created_at=NOW,
        )
    )
    if status is ConversationTurnStatus.PLANNED:
        return stored
    finished = replace(
        stored,
        status=status,
        completed_at=(
            None
            if status is ConversationTurnStatus.WAITING_CONFIRMATION
            else NOW + timedelta(minutes=1)
        ),
    )
    return await conversations.update_turn(finished)


async def test_a_message_is_accepted_as_a_queued_request(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)

    stored, created = await requests.accept(_request(thread.id))

    assert created is True
    assert stored.status is ConversationRequestStatus.QUEUED
    assert stored.stage is None
    assert stored.input_text == "我今天有什么事？"
    assert stored.started_at is None
    assert stored.finished_at is None
    assert stored.can_cancel is True
    assert await requests.get(stored.id) == stored


async def test_a_retried_accept_returns_the_same_request(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """The idempotency key, and the reason a browser timeout is harmless."""
    thread = await _thread(conversations)
    first, _ = await requests.accept(_request(thread.id))

    second, created = await requests.accept(_request(thread.id, text="另一句话"))

    assert created is False
    assert second.id == first.id
    # The stored row is the answer: a retry never rewrites the accepted intent.
    assert second.input_text == "我今天有什么事？"
    assert len(await requests.list_for_thread(thread.id)) == 1


async def test_the_same_client_id_in_another_thread_is_another_request(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    first_thread = await _thread(conversations)
    second_thread = await _thread(conversations, at=NOW + timedelta(minutes=1))

    first, _ = await requests.accept(_request(first_thread.id))
    second, created = await requests.accept(_request(second_thread.id))

    assert created is True
    assert second.id != first.id


async def test_the_database_refuses_to_store_a_users_words_that_are_not_bounded(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)

    with pytest.raises(InvalidConversationRequest):
        await requests.accept(_request(thread.id, text="   "))


async def test_claiming_moves_one_request_to_processing_and_records_the_start(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    started_at = NOW + timedelta(seconds=5)

    claimed = await requests.claim_next(at=started_at)

    assert claimed is not None
    assert claimed.id == stored.id
    assert claimed.status is ConversationRequestStatus.PROCESSING
    assert claimed.stage is ConversationProgressStage.UNDERSTANDING
    assert claimed.started_at == started_at
    assert claimed.can_cancel is True
    assert await requests.claim_next(at=started_at) is None


async def test_one_thread_never_has_two_active_requests(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """The invariant that protects a thread's conversational order."""
    thread = await _thread(conversations)
    await requests.accept(_request(thread.id, client_id="first-client-id-0001"))
    await requests.accept(
        ConversationRequest(
            thread_id=thread.id,
            client_request_id="second-client-id-0002",
            input_text="第二条",
            created_at=NOW + timedelta(seconds=1),
        )
    )

    first = await requests.claim_next(at=NOW, thread_id=thread.id)
    second = await requests.claim_next(at=NOW, thread_id=thread.id)

    assert first is not None
    assert second is None
    queued = await requests.list_by_status(ConversationRequestStatus.QUEUED)
    assert [item.client_request_id for item in queued] == ["second-client-id-0002"]


async def test_queued_requests_are_claimed_in_the_order_they_arrived(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)
    for position, client_id in enumerate(("client-id-aaaa-0001", "client-id-bbbb-0002")):
        await requests.accept(
            ConversationRequest(
                thread_id=thread.id,
                client_request_id=client_id,
                input_text=f"第 {position} 条",
                created_at=NOW + timedelta(seconds=position),
            )
        )

    first = await requests.claim_next(at=NOW, thread_id=thread.id)
    await requests.complete(first.id, at=NOW)
    second = await requests.claim_next(at=NOW, thread_id=thread.id)

    assert first is not None and second is not None
    assert (first.input_text, second.input_text) == ("第 0 条", "第 1 条")


async def test_a_thread_is_independent_of_another(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    first_thread = await _thread(conversations)
    second_thread = await _thread(conversations, at=NOW + timedelta(seconds=1))
    await requests.accept(_request(first_thread.id, client_id="client-id-thread-one"))
    await requests.accept(_request(second_thread.id, client_id="client-id-thread-two"))

    first = await requests.claim_next(at=NOW, thread_id=first_thread.id)
    second = await requests.claim_next(at=NOW, thread_id=second_thread.id)

    assert first is not None and second is not None
    assert first.thread_id == first_thread.id
    assert second.thread_id == second_thread.id


async def test_attaching_a_turn_binds_it_in_both_directions(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
    database: Database,
) -> None:
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)
    turn = await _make_turn(conversations, thread.id)

    attached = await requests.attach_turn(stored.id, turn.id)

    assert attached.turn_id == turn.id
    with database.connect() as connection:
        row = connection.execute(
            "SELECT request_id FROM conversation_turns WHERE id = ?", (str(turn.id),)
        ).fetchone()
    assert row is not None
    assert str(row["request_id"]) == str(stored.id)
    with pytest.raises(InvalidConversationRequest):
        await requests.attach_turn(stored.id, turn.id)


async def test_a_completed_request_drops_the_input_text_it_no_longer_needs(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)
    turn = await _make_turn(conversations, thread.id)
    await requests.attach_turn(stored.id, turn.id)

    completed = await requests.complete(
        stored.id, at=NOW + timedelta(minutes=1), stage=ConversationProgressStage.FINALIZING
    )

    assert completed.status is ConversationRequestStatus.COMPLETED
    assert completed.input_text is None
    assert completed.finished_at == NOW + timedelta(minutes=1)
    assert completed.stage is ConversationProgressStage.FINALIZING


async def test_a_cancelled_request_keeps_the_duplicated_text_until_it_is_bound(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """Crash recovery is never compromised to clear a field early."""
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))

    cancelled = await requests.cancel(stored.id, at=NOW)

    assert cancelled.status is ConversationRequestStatus.CANCELLED
    assert cancelled.input_text == "我今天有什么事？"
    assert cancelled.started_at is None
    assert cancelled.finished_at == NOW


async def test_a_terminal_request_keeps_the_first_terminal_answer(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """A retried finish is not a second effect, and a cancelled request stays cancelled."""
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    cancelled = await requests.cancel(stored.id, at=NOW)

    again = await requests.complete(stored.id, at=NOW + timedelta(minutes=5))

    assert cancelled.status is ConversationRequestStatus.CANCELLED
    assert again.status is ConversationRequestStatus.CANCELLED
    assert again.finished_at == NOW


async def test_stop_is_recorded_without_deciding_the_outcome(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)

    asked = await requests.request_cancel(stored.id, at=NOW + timedelta(seconds=2))
    again = await requests.request_cancel(stored.id, at=NOW + timedelta(seconds=9))

    assert asked.status is ConversationRequestStatus.PROCESSING
    assert asked.cancel_requested_at == NOW + timedelta(seconds=2)
    assert again.cancel_requested_at == NOW + timedelta(seconds=2)


async def test_restart_never_replays_a_processing_request(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """No durable turn evidence means no rerun, which is the fail-closed default."""
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)

    resolved = await requests.recover(at=NOW + timedelta(hours=1))

    assert [item.id for item in resolved] == [stored.id]
    assert resolved[0].status is ConversationRequestStatus.INTERRUPTED
    assert resolved[0].error_code == "INTERRUPTED"
    assert resolved[0].finished_at == NOW + timedelta(hours=1)
    assert await requests.claim_next(at=NOW, thread_id=thread.id) is None


async def test_restart_mirrors_a_terminal_turn(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)
    turn = await _make_turn(conversations, thread.id, status=ConversationTurnStatus.COMPLETED)
    await requests.attach_turn(stored.id, turn.id)

    resolved = await requests.recover(at=NOW + timedelta(hours=1))

    assert resolved[0].status is ConversationRequestStatus.COMPLETED
    assert resolved[0].error_code is None
    assert resolved[0].input_text is None


async def test_restart_accepts_a_turn_that_is_waiting_for_a_human(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """A parked turn is finished work: the pending offer is itself durable."""
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))
    await requests.claim_next(at=NOW)
    turn = await _make_turn(
        conversations, thread.id, status=ConversationTurnStatus.WAITING_CONFIRMATION
    )
    await requests.attach_turn(stored.id, turn.id)

    resolved = await requests.recover(at=NOW + timedelta(hours=1))

    assert resolved[0].status is ConversationRequestStatus.COMPLETED
    assert resolved[0].stage is ConversationProgressStage.WAITING_CONFIRMATION


async def test_restart_leaves_queued_work_alone(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    """Queued intent survives a restart and is still claimable afterwards."""
    thread = await _thread(conversations)
    stored, _ = await requests.accept(_request(thread.id))

    resolved = await requests.recover(at=NOW + timedelta(hours=1))
    claimed = await requests.claim_next(at=NOW + timedelta(hours=1))

    assert resolved == ()
    assert claimed is not None
    assert claimed.id == stored.id


async def test_unknown_requests_and_impossible_transitions_are_refused(
    requests: SqliteConversationRequestRepository,
    conversations: SqliteConversationRepository,
) -> None:
    missing = uuid4()
    thread = await _thread(conversations)
    queued, _ = await requests.accept(_request(thread.id))

    with pytest.raises(ConversationRequestNotFound):
        await requests.complete(missing, at=NOW)
    with pytest.raises(ConversationRequestNotFound):
        await requests.request_cancel(missing, at=NOW)
    # A stage belongs to a request a worker is holding, not to intent that is still queued.
    with pytest.raises(InvalidConversationRequest):
        await requests.set_stage(queued.id, ConversationProgressStage.FINALIZING)
