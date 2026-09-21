"""The ephemeral event channel: bounded, disposable, and free of anything private (ADR-0041 §12).

Two layers are under test. The broker decides who hears what and what happens when a subscriber
stops reading; the SSE framing decides what actually goes over the wire. Neither is authoritative:
every assertion here is about a *notification*, and the durable snapshot has its own tests.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from assistant.application.conversation_event_broker import (
    DEFAULT_QUEUE_SIZE,
    RESYNC_REQUIRED,
    SAFE_EVENT_NAMES,
    ConversationEvent,
    ConversationEventBroker,
)


def test_the_event_vocabulary_is_closed() -> None:
    """A name outside the set cannot be constructed, so it cannot be sent."""
    with pytest.raises(ValueError):
        ConversationEvent(thread_id=uuid4(), name="request.chain_of_thought", data={}, id=1)


async def test_a_subscriber_receives_what_its_thread_publishes() -> None:
    broker = ConversationEventBroker()
    thread = uuid4()
    other = uuid4()
    mine = broker.subscribe(thread)
    theirs = broker.subscribe(other)

    broker.publish(thread, "request.queued", {"request_id": "r1"})
    broker.publish(other, "request.queued", {"request_id": "r2"})

    first = await mine.next_event(timeout=0.5)
    second = await theirs.next_event(timeout=0.5)
    assert first is not None and first.data["request_id"] == "r1"
    assert second is not None and second.data["request_id"] == "r2"
    assert await mine.next_event(timeout=0.01) is None


async def test_ids_are_monotonic_within_the_process() -> None:
    broker = ConversationEventBroker()
    thread = uuid4()
    subscription = broker.subscribe(thread)

    first = broker.publish(thread, "request.started", {})
    second = broker.publish(thread, "request.completed", {})

    assert second.id > first.id
    assert (await subscription.next_event(timeout=0.5)).id == first.id


async def test_a_slow_subscriber_is_told_to_resynchronise() -> None:
    """Bounded queues: a client that stops reading must not grow the server's memory."""
    broker = ConversationEventBroker(queue_size=3)
    thread = uuid4()
    subscription = broker.subscribe(thread)

    for index in range(10):
        broker.publish(thread, "request.stage", {"index": index})

    assert subscription.overflowed is True
    assert subscription.queue.qsize() == 0


async def test_disconnecting_cleans_the_subscription_up() -> None:
    broker = ConversationEventBroker()
    thread = uuid4()
    subscription = broker.subscribe(thread)
    assert broker.subscriber_count == 1

    broker.unsubscribe(subscription)

    assert broker.subscriber_count == 0
    assert broker.publish(thread, "thread.updated", {}).id > 0


async def test_the_default_queue_is_bounded() -> None:
    broker = ConversationEventBroker()
    subscription = broker.subscribe(uuid4())

    assert subscription.queue_size == DEFAULT_QUEUE_SIZE


def test_the_safe_vocabulary_excludes_everything_private() -> None:
    forbidden = ("prompt", "reasoning", "thought", "challenge", "chain")
    assert not [name for name in SAFE_EVENT_NAMES if any(word in name for word in forbidden)]
    assert RESYNC_REQUIRED in SAFE_EVENT_NAMES


async def test_the_sse_frames_carry_only_safe_data(fake_chat, thread_id) -> None:
    """End to end over the real generator: what a browser receives, and when it ends."""
    from assistant.adapters.web.chat import conversation_event_stream

    chat = fake_chat
    stream = conversation_event_stream(chat, thread_id, heartbeat=0.05)
    opened = await anext(stream)
    assert "event: stream.open" in opened
    assert json.loads(opened.split("data: ", 1)[1])["thread_id"] == str(thread_id)

    chat.broker.publish(
        thread_id,
        "request.stage",
        {"request_id": "r1", "stage": "understanding", "can_cancel": True},
    )
    frame = await anext(stream)
    assert "event: request.stage" in frame
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {
        "request_id": "r1",
        "stage": "understanding",
        "can_cancel": True,
    }

    heartbeat = await anext(stream)
    assert heartbeat.startswith(":")

    await stream.aclose()
    assert chat.broker.subscriber_count == 0


async def test_an_overflowed_stream_ends_with_a_resync_frame(fake_chat, thread_id) -> None:
    from assistant.adapters.web.chat import conversation_event_stream

    chat = fake_chat
    stream = conversation_event_stream(chat, thread_id, heartbeat=0.05)
    await anext(stream)
    for index in range(DEFAULT_QUEUE_SIZE + 5):
        chat.broker.publish(thread_id, "request.stage", {"index": index})

    frames = [frame async for frame in stream]

    assert any(f"event: {RESYNC_REQUIRED}" in frame for frame in frames)
    assert chat.broker.subscriber_count == 0


@pytest.fixture
def thread_id():
    return uuid4()


@pytest.fixture
def fake_chat(thread_id):
    """A chat surface stub: the stream only needs the broker."""

    class _Chat:
        def __init__(self) -> None:
            self.broker = ConversationEventBroker()

    return _Chat()

