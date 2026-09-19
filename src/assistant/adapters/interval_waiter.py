"""The asyncio implementation of the IntervalWaiter port (ADR-0013)."""

from __future__ import annotations

import asyncio
import contextlib


class AsyncioIntervalWaiter:
    """Sleeps for a bounded time, waking immediately when the stop event is set."""

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await stop_event.wait()


__all__ = ["AsyncioIntervalWaiter"]

