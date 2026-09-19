"""Unit tests for the periodic loop of IndexSyncService (ADR-0013)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.application.index_sync import IndexSyncResult, IndexSyncService
from assistant.domain.config import AssistantConfig, IndexingConfig
from assistant.domain.errors import InvalidAssistantConfig
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


@dataclass
class RecordingWaiter:
    """Records requested intervals and can stop the loop after a given number of waits."""

    stop_after_waits: int | None = None
    calls: list[float] = field(default_factory=list)

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        self.calls.append(seconds)
        if self.stop_after_waits is not None and len(self.calls) >= self.stop_after_waits:
            stop_event.set()


class Unused:
    """Stand-in dependency that must never be called by the loop itself."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the loop must not touch {name}")


def _service(
    config: AssistantConfig, waiter: RecordingWaiter
) -> tuple[IndexSyncService, list[int]]:
    service = IndexSyncService(
        config,
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        FakeClock(start=NOW),
        waiter,  # type: ignore[arg-type]
        enable_logging=False,
    )
    runs: list[int] = []
    original = service.sync_once

    async def counting(*args: object, **kwargs: object) -> IndexSyncResult:
        runs.append(1)
        return await original(*args, **kwargs)

    service.sync_once = counting  # type: ignore[method-assign]
    return service, runs


async def test_run_on_startup_syncs_immediately_and_then_periodically() -> None:
    waiter = RecordingWaiter(stop_after_waits=2)
    config = AssistantConfig(
        indexing=IndexingConfig(interval_seconds=300, run_on_startup=True)
    )
    service, runs = _service(config, waiter)

    await service.run_forever(asyncio.Event())

    assert len(runs) == 2
    assert waiter.calls == [300, 300]


async def test_run_on_startup_false_waits_first() -> None:
    waiter = RecordingWaiter(stop_after_waits=1)
    config = AssistantConfig(
        indexing=IndexingConfig(interval_seconds=60, run_on_startup=False)
    )
    service, runs = _service(config, waiter)

    await service.run_forever(asyncio.Event())

    assert runs == []
    assert waiter.calls == [60]


async def test_stop_event_ends_a_long_wait_promptly() -> None:
    config = AssistantConfig(
        indexing=IndexingConfig(interval_seconds=3600, run_on_startup=False)
    )
    service = IndexSyncService(
        config,
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        FakeClock(start=NOW),
        AsyncioIntervalWaiter(),
        enable_logging=False,
    )
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run_forever(stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()

    await asyncio.wait_for(task, timeout=1)
    assert not task.cancelled()


async def test_cancellation_propagates_without_leaking_waiters() -> None:
    config = AssistantConfig(
        indexing=IndexingConfig(interval_seconds=3600, run_on_startup=False)
    )
    service = IndexSyncService(
        config,
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        Unused(),  # type: ignore[arg-type]
        FakeClock(start=NOW),
        AsyncioIntervalWaiter(),
        enable_logging=False,
    )
    task = asyncio.create_task(service.run_forever(asyncio.Event()))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leftover = [
        item
        for item in asyncio.all_tasks()
        if item is not asyncio.current_task() and not item.done()
    ]
    assert leftover == []


def test_interval_wait_is_bounded_by_the_configured_value() -> None:
    with pytest.raises(InvalidAssistantConfig):
        IndexingConfig(interval_seconds=1)
