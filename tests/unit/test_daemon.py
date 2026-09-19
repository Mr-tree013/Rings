"""Tests for the daemon lifecycle.

The daemon must start, reach its wait state, and stop without leaking exceptions — both
when asked to stop through its stop event and when cancelled from the outside.
"""

from __future__ import annotations

import asyncio

import pytest

from assistant.daemon import async_main, serve


async def test_serve_returns_when_stop_event_is_set() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(serve(stop_event))

    await asyncio.sleep(0)  # let the daemon body reach its wait state
    assert not task.done()

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert not task.cancelled()
    assert task.exception() is None


async def test_serve_propagates_cancellation() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(serve(stop_event))

    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_async_main_can_be_started_and_cancelled() -> None:
    task = asyncio.create_task(async_main())

    await asyncio.sleep(0.05)
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The only outcome is the cancellation we requested: Task.exception() itself raises
    # CancelledError for a cancelled task, so `cancelled()` is the meaningful assertion.
    assert task.cancelled()
