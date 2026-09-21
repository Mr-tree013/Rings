"""The supervised periodic attention reconciliation (ADR-0042 §23-24).

Attention is correct because it is *reconciled*, not because every producer remembered to tell it
something. This service runs the projector every `interval_seconds`, which means a task that went
overdue while nothing else happened still shows up, and a proposal that was applied by the CLI
still disappears. A missed event is a latency problem; it is never a correctness problem.

It is supervised like everything else in the daemon: a failure is logged and retried with backoff,
and it cannot take the mail pipeline, the scheduler or the web server down with it. One property is
deliberately not here — the projector is never asked to decide anything. It reads existing state
and writes derived rows, so "attention" can never become a second authority.
"""

from __future__ import annotations

import asyncio
import logging

from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.application.attention import AttentionProjector
from assistant.application.conversation_event_broker import ConversationEventBroker
from assistant.ports.interval_waiter import IntervalWaiter

LOGGER = logging.getLogger("assistant.attention")

DEFAULT_ATTENTION_INTERVAL_SECONDS = 60.0
"""How often the inbox is reconciled. A minute is far below the scale of anything it reports."""

EVENT_NAME = "attention.updated"
"""The browser hint. The payload carries no count; the browser refetches the durable one."""


class AttentionRefreshService:
    """Keep the derived inbox in step with the durable state behind it."""

    name = "attention"

    def __init__(
        self,
        projector: AttentionProjector,
        *,
        interval_seconds: float = DEFAULT_ATTENTION_INTERVAL_SECONDS,
        broker: ConversationEventBroker | None = None,
        waiter: IntervalWaiter | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("the attention interval must be positive")
        self._projector = projector
        self._interval = interval_seconds
        self._broker = broker
        self._waiter = waiter if waiter is not None else AsyncioIntervalWaiter()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Reconcile once, tell open pages, then keep doing it until asked to stop."""
        while not stop_event.is_set():
            await self.refresh_once()
            await self._waiter.wait(self._interval, stop_event)

    async def refresh_once(self) -> None:
        """One reconciliation. A failure here is logged by the supervisor, not hidden here."""
        summary = await self._projector.refresh()
        if not summary.changed or self._broker is None:
            return
        # Ephemeral, per-process and disposable: a browser that misses it reloads and sees the same
        # truth, because the truth was never in the frame.
        self._broker.broadcast(EVENT_NAME, {"changed": True})


__all__ = [
    "DEFAULT_ATTENTION_INTERVAL_SECONDS",
    "EVENT_NAME",
    "AttentionRefreshService",
]
