"""IntervalWaiter port: wait for a duration, but wake immediately on shutdown (ADR-0013).

Both the periodic reconciliation loop and the supervisor's restart backoff need the same
behaviour: sleep for a bounded time, return early when the process is asked to stop, and
never leak a pending task when cancelled.
"""

from __future__ import annotations

import asyncio
from typing import Protocol


class IntervalWaiter(Protocol):
    """Waits for a duration or until a stop event is set."""

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        """Return after `seconds`, or immediately once `stop_event` is set."""
        ...


__all__ = ["IntervalWaiter"]

