"""The supervised owner of the durable conversation queue (ADR-0041 §10, §30).

The queue itself is durable state, not a process: this service exists so that *something* runs the
restart recovery and picks up work that was accepted before the last shutdown. Its whole job is:

```text
start ──► recover (fail closed) ──► resume only what is QUEUED ──► idle until stop
```

A crash therefore has exactly one outcome: work that had not started yet continues, and work that
had started is never replayed. Nothing here decides what a message means; that stays in the
conversation runtime.
"""

from __future__ import annotations

import asyncio
import logging

from assistant.application.conversation_request_coordinator import (
    ConversationRequestCoordinator,
)

LOGGER = logging.getLogger("assistant.conversation.queue")


class ConversationQueueService:
    """Recover the queue once, then wait for the process to stop."""

    name = "conversation-queue"

    def __init__(self, coordinator: ConversationRequestCoordinator) -> None:
        self._coordinator = coordinator

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Resolve interrupted work, resume queued work, then idle."""
        resolved = await self._coordinator.recover()
        LOGGER.info("conversation queue recovered (%d request(s) resolved)", len(resolved))
        await stop_event.wait()
        # A worker interrupted here leaves its request `PROCESSING`, which the next start resolves
        # instead of replaying. Shutdown is therefore never a silent retry.
        await self._coordinator.shutdown()


__all__ = ["ConversationQueueService"]
