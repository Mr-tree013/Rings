"""The browser conversation queue end to end (Phase 11A, ADR-0041 §7-§10, §30-§34).

Real SQLite, the real conversation runtime, the real ASGI app, a scripted provider. What is under
test is the interaction contract: accepted input is durable before it runs, one thread runs one
message at a time, a retried POST is not a second message, Stop is honoured where it is safe, and
Stop refuses to claim anything where it is not.

Each client is entered as a context manager so the app gets one event loop for the whole block —
the same thing Uvicorn does, and what lets a queued worker run between requests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.domain.conversation_request import (
    ConversationProgressStage,
    ConversationRequest,
)
from assistant.domain.errors import CannotCancelSafely
from tests.support.chat import ChatStack, build_chat
from tests.support.conversation import direct_reply, operation, plan

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

_TASK_ARGUMENTS = {
    "title": "一条真实的任务",
    "description": None,
    "priority": "normal",
    "estimated_minutes": 30,
    "due_at": "2026-09-24T15:59:00+00:00",
}
"""A complete `task.create` argument object: the schema is closed, and so is this test's intent."""


@pytest.fixture
async def stack(tmp_path: Path) -> ChatStack:
    return await build_chat(tmp_path)


async def _status(stack: ChatStack, request_id: str) -> str | None:
    row = await stack.request_row(request_id)
    return None if row is None else row.status.value


async def _stage(stack: ChatStack, request_id: str) -> str | None:
    row = await stack.request_row(request_id)
    return None if row is None or row.stage is None else row.stage.value


async def _settled(stack: ChatStack, request_id: str) -> bool:
    return await _status(stack, request_id) in TERMINAL


async def _is(stack: ChatStack, request_id: str, status: str) -> bool:
    return await _status(stack, request_id) == status


async def _reached(stack: ChatStack, request_id: str, stage: ConversationProgressStage) -> bool:
    return await _stage(stack, request_id) == stage.value


async def _new_thread(client, stack: ChatStack, tokens: dict[str, str]) -> str:
    """Create a conversation through the real endpoint and return its id."""
    response = client.post("/api/chat/threads", headers=stack.headers(tokens["csrf"]))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_a_message_is_accepted_queued_and_answered(stack: ChatStack) -> None:
    """The happy path: accept returns immediately, and the answer lands in durable history."""
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(direct_reply("今天只有一件小事。"))

        accepted = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={
                "client_request_id": "11111111-2222-3333-4444-555555555555",
                "text": "我今天有什么事？",
            },
            headers=stack.headers(tokens["csrf"]),
        )

        assert accepted.status_code == 202, accepted.text
        payload = accepted.json()
        assert payload["status"] == "queued"
        assert payload["stage"] is None
        assert payload["text"] == "我今天有什么事？"
        assert payload["can_cancel"] is True
        assert payload["duplicate"] is False

        assert await stack.wait_until_async(
            lambda: _settled(stack, payload["id"])
        ), await stack.request_row(payload["id"])
        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        assert [item["role"] for item in snapshot["messages"]] == ["user", "assistant"]
        assert snapshot["messages"][1]["text"] == "今天只有一件小事。"
        assert snapshot["active"] is None
        assert snapshot["pending"] == []
        # The words are not kept twice: the message is history, the queue row is over.
        row = await stack.request_row(payload["id"])
        assert row.input_text is None
        assert row.turn_id is not None


async def test_a_retried_post_is_the_same_request(stack: ChatStack) -> None:
    """`client_request_id` is the idempotency key: one message, one turn, one model call."""
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(direct_reply("好。"), direct_reply("不该出现的第二次回答。"))
        body = {"client_request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "text": "你好"}

        first = client.post(
            f"/api/chat/threads/{thread}/messages",
            json=body,
            headers=stack.headers(tokens["csrf"]),
        )
        second = client.post(
            f"/api/chat/threads/{thread}/messages",
            json=body,
            headers=stack.headers(tokens["csrf"]),
        )

        assert first.status_code == 202 and second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["duplicate"] is True
        assert await stack.wait_until_async(lambda: _settled(stack, first.json()["id"]))
        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        assert len(snapshot["messages"]) == 2
        assert len(await stack.requests_for(thread)) == 1
        assert len(stack.harness.model.requests) == 1


async def test_a_second_message_waits_for_the_first(tmp_path: Path) -> None:
    """Two submissions are two turns, in order, one at a time — never one merged message."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(direct_reply("第一条的回答。"), direct_reply("第二条的回答。"))

        first = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "first-message-id-0001", "text": "第一条"},
            headers=stack.headers(tokens["csrf"]),
        ).json()
        second = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "second-message-id-0002", "text": "第二条"},
            headers=stack.headers(tokens["csrf"]),
        ).json()

        assert second["status"] == "queued"
        assert second["queue_position"] == 1
        assert await stack.wait_until_async(lambda: _settled(stack, second["id"]))
        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        roles = [item["role"] for item in snapshot["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert snapshot["messages"][0]["text"] == "第一条"
        assert snapshot["messages"][2]["text"] == "第二条"
        assert [item["status"] for item in snapshot["requests"]] == ["completed", "completed"]
        assert first["id"] != second["id"]


async def test_cancelling_a_queued_request_never_becomes_a_turn(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path, delay=0.3)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(direct_reply("第一条的回答。"))

        running = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "keep-me-running-0001", "text": "第一条"},
            headers=stack.headers(tokens["csrf"]),
        ).json()
        assert await stack.wait_until_async(
            lambda: _reached(stack, running["id"], ConversationProgressStage.UNDERSTANDING)
        )
        queued = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "cancel-this-one-0002", "text": "第二条"},
            headers=stack.headers(tokens["csrf"]),
        ).json()
        assert queued["status"] == "queued"

        cancelled = client.post(
            f"/api/chat/requests/{queued['id']}/cancel", headers=stack.headers(tokens["csrf"])
        )

        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["finished_at"] is not None
        assert await stack.wait_until_async(lambda: _settled(stack, running["id"]))
        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        texts = [item["text"] for item in snapshot["messages"]]
        assert "第二条" not in texts
        assert len(stack.harness.model.requests) == 1


async def test_stop_during_understanding_leaves_no_mutation(tmp_path: Path) -> None:
    """Stop before the plan is acted on: the intent is abandoned, and nothing is written.

    The scripted model asks for a task write, so anything other than "nothing happened" would be
    visible in the task table.
    """
    stack = await build_chat(tmp_path, delay=0.4)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(plan(operation("task.create", _TASK_ARGUMENTS)))

        accepted = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "stop-me-before-write-01", "text": "记一下：明天交报告"},
            headers=stack.headers(tokens["csrf"]),
        ).json()

        assert await stack.wait_until_async(
            lambda: _reached(stack, accepted["id"], ConversationProgressStage.UNDERSTANDING)
        )
        stopped = client.post(
            f"/api/chat/requests/{accepted['id']}/cancel", headers=stack.headers(tokens["csrf"])
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "processing"
        assert stopped.json()["can_cancel"] is True

        assert await stack.wait_until_async(
            lambda: _is(stack, accepted["id"], "cancelled")
        ), await stack.request_row(accepted["id"])
        assert await stack.harness.task_titles() == []
        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        assert snapshot["messages"][-1]["role"] == "assistant"
        assert "已停止" in snapshot["messages"][-1]["text"]


async def test_a_finished_request_is_not_cancelled_by_a_late_stop(tmp_path: Path) -> None:
    """Once the write has happened, Stop is a refusal: the task exists exactly once."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _new_thread(client, stack, tokens)
        stack.harness.queue(plan(operation("task.create", _TASK_ARGUMENTS)))

        accepted = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "too-late-to-stop-0001", "text": "记一下：明天交报告"},
            headers=stack.headers(tokens["csrf"]),
        ).json()
        assert await stack.wait_until_async(lambda: _settled(stack, accepted["id"]))

        refused = client.post(
            f"/api/chat/requests/{accepted['id']}/cancel", headers=stack.headers(tokens["csrf"])
        )

        assert refused.status_code == 200
        assert refused.json()["status"] == "completed"
        assert refused.json()["can_cancel"] is False
        assert await stack.harness.task_titles() == ["一条真实的任务"]


async def test_a_request_past_the_boundary_is_refused_by_the_server(tmp_path: Path) -> None:
    """`can_cancel` is derived from durable state, so the browser cannot talk its way past it."""
    stack = await build_chat(tmp_path)
    repository = stack.requests_repository()
    thread = await stack.harness.service.start_thread()
    stored, _ = await repository.accept(
        ConversationRequest(
            thread_id=thread.id,
            client_request_id="already-writing-0001",
            input_text="记一下",
            created_at=stack.clock.now(),
        )
    )
    claimed = await repository.claim_next(at=stack.clock.now(), thread_id=thread.id)
    assert claimed is not None
    await repository.set_stage(stored.id, ConversationProgressStage.UPDATING_LOCAL_STATE)

    with pytest.raises(CannotCancelSafely):
        await stack.coordinator.cancel(stored.id)

    unchanged = await repository.get(stored.id)
    assert unchanged is not None
    assert unchanged.status.value == "processing"
    assert unchanged.cancel_requested_at is None


async def test_an_unknown_request_is_not_found(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)

        response = client.post(
            "/api/chat/requests/11111111-2222-3333-4444-555555555555/cancel",
            headers=stack.headers(tokens["csrf"]),
        )

        assert response.status_code == 404


async def test_threads_run_independently(tmp_path: Path) -> None:
    """One thread's queue does not block another's — the claim is per thread."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        first = await _new_thread(client, stack, tokens)
        second = await _new_thread(client, stack, tokens)
        stack.harness.queue(direct_reply("一号线程的回答。"), direct_reply("二号线程的回答。"))

        one = client.post(
            f"/api/chat/threads/{first}/messages",
            json={"client_request_id": "thread-one-message-01", "text": "一号线程"},
            headers=stack.headers(tokens["csrf"]),
        ).json()
        two = client.post(
            f"/api/chat/threads/{second}/messages",
            json={"client_request_id": "thread-two-message-02", "text": "二号线程"},
            headers=stack.headers(tokens["csrf"]),
        ).json()

        assert await stack.wait_until_async(lambda: _settled(stack, one["id"]))
        assert await stack.wait_until_async(lambda: _settled(stack, two["id"]))
        snapshot_one = client.get(f"/api/chat/threads/{first}/snapshot").json()
        snapshot_two = client.get(f"/api/chat/threads/{second}/snapshot").json()
        assert snapshot_one["thread"]["display_title"] == "一号线程"
        assert snapshot_two["thread"]["display_title"] == "二号线程"
