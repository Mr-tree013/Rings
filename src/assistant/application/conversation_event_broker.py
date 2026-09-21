"""An in-memory, per-thread notification channel for the browser (ADR-0041 §12-§13, §42).

```text
coordinator ──► broker.publish(thread, name, safe payload)
                     │
                     ├─► subscriber A (bounded queue)  ──► SSE
                     └─► subscriber B (bounded queue)  ──► SSE
```

Three properties keep this honest:

* **it is not state.** Nothing here is persisted, nothing survives a restart, and no browser may
  derive correctness from it. A reload fetches a durable snapshot; the stream only says "something
  changed" more quickly. That is why a restart resetting the stream is not a problem to solve
  (ADR-0041 §13-§16).
* **queues are bounded.** A client that stops reading cannot grow the server's memory: the queue
  overflows, the subscription is marked for resynchronisation, and the connection is closed so the
  browser reconnects and reloads its snapshot.
* **payloads are closed.** `SAFE_EVENT_NAMES` is the whole vocabulary, and the payloads built by
  the application contain ids, statuses, stages and user-facing text — never a prompt, a provider
  response, a reasoning step or an approval challenge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

RESYNC_REQUIRED = "resync.required"
"""Sent, then close the connection, when a subscriber's queue overflowed: reload, do not patch."""

SAFE_EVENT_NAMES = frozenset(
    {
        "request.queued",
        "request.started",
        "request.stage",
        "request.cancel_requested",
        "request.cancelled",
        "request.completed",
        "request.failed",
        "request.interrupted",
        "assistant.message",
        "confirmation.required",
        "confirmation.updated",
        "thread.updated",
        "attention.updated",
        RESYNC_REQUIRED,
    }
)
"""The complete event vocabulary. A name outside this set is a programming error, not a feature."""

DEFAULT_QUEUE_SIZE = 64
"""How much a slow subscriber may fall behind before it is asked to resynchronise."""


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """One ephemeral notification, already shaped for the browser."""

    thread_id: UUID
    name: str
    data: dict[str, object]
    id: int

    def __post_init__(self) -> None:
        if self.name not in SAFE_EVENT_NAMES:
            raise ValueError(f"{self.name!r} is not a safe browser event name")


@dataclass(eq=False)
class ConversationEventSubscription:
    """One browser's place in the stream, with a queue that cannot grow without bound."""

    thread_id: UUID
    queue_size: int = DEFAULT_QUEUE_SIZE
    queue: asyncio.Queue[ConversationEvent] = field(init=False)
    # Set when this subscriber fell too far behind: the reader must emit a resync and stop, so a
    # browser that never reloads its snapshot cannot make the server buffer on its behalf.
    overflowed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)

    def offer(self, event: ConversationEvent) -> None:
        """Enqueue one event, or mark the subscription for resynchronisation."""
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A client that is not reading is a client that must reload, never a reason for the
            # server to hold more and more in memory.
            self.overflowed = True
            _drain(self.queue)

    async def next_event(self, *, timeout: float) -> ConversationEvent | None:
        """The next event, or `None` when `timeout` elapsed (the caller sends a heartbeat)."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except TimeoutError:
            return None


class ConversationEventBroker:
    """Fan-out of ephemeral progress notifications, per conversation thread."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[UUID, list[ConversationEventSubscription]] = {}
        self._next_id = 0

    @property
    def subscriber_count(self) -> int:
        """How many live subscriptions this process holds."""
        return sum(len(items) for items in self._subscribers.values())

    def subscribe(self, thread_id: UUID) -> ConversationEventSubscription:
        """Add one subscriber for one thread."""
        subscription = ConversationEventSubscription(thread_id, queue_size=self._queue_size)
        self._subscribers.setdefault(thread_id, []).append(subscription)
        return subscription

    def unsubscribe(self, subscription: ConversationEventSubscription) -> None:
        """Remove one subscriber. Called from the endpoint's `finally`, so it always happens."""
        items = self._subscribers.get(subscription.thread_id)
        if not items:
            return
        remaining = [item for item in items if item is not subscription]
        if remaining:
            self._subscribers[subscription.thread_id] = remaining
        else:
            self._subscribers.pop(subscription.thread_id, None)

    def publish(
        self, thread_id: UUID, name: str, data: dict[str, object] | None = None
    ) -> ConversationEvent:
        """Offer one event to every subscriber of one thread, and to nobody else."""
        self._next_id += 1
        event = ConversationEvent(
            thread_id=thread_id, name=name, data=dict(data or {}), id=self._next_id
        )
        for subscription in list(self._subscribers.get(thread_id, ())):
            subscription.offer(event)
        return event

    def broadcast(self, name: str, data: dict[str, object] | None = None) -> int:
        """Offer one event to every live subscriber, whatever thread it is watching.

        The inbox is not per-thread, so an attention update belongs to every open page. Like every
        other frame here this is a *hint*: the browser refetches the count from durable state, and a
        subscriber that missed the frame still sees the truth on its next read (ADR-0042 §20).

        Returns how many subscribers the frame reached, which is all a caller can act on.
        """
        self._next_id += 1
        payload = dict(data or {})
        delivered = 0
        for subscriptions in list(self._subscribers.values()):
            for subscription in list(subscriptions):
                subscription.offer(
                    ConversationEvent(
                        thread_id=subscription.thread_id,
                        name=name,
                        data=dict(payload),
                        id=self._next_id,
                    )
                )
                delivered += 1
        return delivered

    def close(self, subscription: ConversationEventSubscription) -> None:
        """Ask one subscriber's stream to end after the events it already has."""
        subscription.overflowed = True


def _drain(queue: asyncio.Queue[ConversationEvent]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "RESYNC_REQUIRED",
    "SAFE_EVENT_NAMES",
    "ConversationEvent",
    "ConversationEventBroker",
    "ConversationEventSubscription",
]
