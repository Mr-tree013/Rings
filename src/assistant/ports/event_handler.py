"""EventHandler port: the code that actually processes a claimed event (ADR-0010).

Handlers may not assume they run once. Processing is at-least-once, so a handler must
either be idempotent or write a durable downstream intent that a later step can dedupe.

An empty, do-nothing handler is deliberately not shipped in production: a handler exists
to do something, and a plausible-looking no-op would hide an unwired pipeline.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.inbound_event import InboundEvent


class EventHandler(Protocol):
    """Processes one event."""

    async def handle(self, event: InboundEvent) -> None:
        """Do the work for `event`.

        Returning normally means success. Raise `PermanentEventError` for a failure that
        retrying cannot fix (the event goes straight to dead letter); any other
        `Exception` is treated as retryable. `asyncio.CancelledError` must be allowed to
        propagate — cancellation is not a business failure.
        """
        ...


__all__ = ["EventHandler"]

