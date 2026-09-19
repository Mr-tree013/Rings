"""Unit tests for the daemon supervisor (ADR-0013)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from assistant.daemon.supervisor import BackoffPolicy, supervise


@dataclass
class ScriptedService:
    """Service that follows a script: `fail`, `return`, or `wait` for shutdown."""

    name: str = "scripted"
    script: list[str] = field(default_factory=list)
    runs: int = 0

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self.runs += 1
        action = self.script.pop(0) if self.script else "wait"
        if action == "fail":
            raise RuntimeError("boom")
        if action == "return":
            return
        await stop_event.wait()


@dataclass
class RecordingWaiter:
    """Records backoff delays; optionally returns immediately."""

    immediate: bool = False
    delays: list[float] = field(default_factory=list)

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        self.delays.append(seconds)
        if self.immediate:
            await asyncio.sleep(0)


async def _wait_until(predicate: object, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")


async def test_a_failing_service_is_restarted() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["fail", "wait"])
    waiter = RecordingWaiter(immediate=True)

    task = asyncio.create_task(supervise(service, stop_event, waiter=waiter))
    await _wait_until(lambda: service.runs >= 2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert service.runs == 2
    assert waiter.delays == [1.0]


async def test_backoff_doubles_and_caps() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["fail", "fail", "fail", "fail"])
    waiter = RecordingWaiter(immediate=True)

    task = asyncio.create_task(
        supervise(
            service,
            stop_event,
            policy=BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=4.0),
            waiter=waiter,
        )
    )
    await _wait_until(lambda: len(waiter.delays) >= 4)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert waiter.delays == [1.0, 2.0, 4.0, 4.0]


async def test_a_healthy_run_resets_the_backoff_sequence() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["fail", "fail", "fail"])
    waiter = RecordingWaiter(immediate=True)

    task = asyncio.create_task(
        supervise(
            service,
            stop_event,
            policy=BackoffPolicy(
                base_delay_seconds=1.0, max_delay_seconds=60.0, healthy_run_seconds=0.0
            ),
            waiter=waiter,
        )
    )
    await _wait_until(lambda: len(waiter.delays) >= 3)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert waiter.delays == [1.0, 1.0, 1.0]


async def test_an_unexpected_return_is_treated_as_a_failure() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["return", "wait"])
    waiter = RecordingWaiter(immediate=True)

    task = asyncio.create_task(supervise(service, stop_event, waiter=waiter))
    await _wait_until(lambda: service.runs >= 2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert service.runs == 2
    assert waiter.delays == [1.0]


async def test_stop_during_backoff_exits_without_waiting() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["fail"])
    delays: list[float] = []

    class StoppingWaiter:
        async def wait(self, seconds: float, event: asyncio.Event) -> None:
            delays.append(seconds)
            event.set()

    task = asyncio.create_task(
        supervise(service, stop_event, waiter=StoppingWaiter())
    )

    await asyncio.wait_for(task, timeout=1)
    assert service.runs == 1
    assert delays == [1.0]


async def test_cancellation_propagates() -> None:
    stop_event = asyncio.Event()
    service = ScriptedService(script=["wait"])
    waiter = RecordingWaiter(immediate=True)

    task = asyncio.create_task(supervise(service, stop_event, waiter=waiter))
    await _wait_until(lambda: service.runs >= 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_a_service_failure_never_cancels_its_sibling() -> None:
    stop_event = asyncio.Event()
    failing = ScriptedService(name="failing", script=["fail"] * 20)
    healthy = ScriptedService(name="healthy", script=["wait"])
    waiter = RecordingWaiter(immediate=True)

    async def scenario() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(supervise(failing, stop_event, waiter=waiter))
            group.create_task(supervise(healthy, stop_event, waiter=waiter))
            await asyncio.sleep(0.05)
            assert failing.runs >= 2
            assert healthy.runs == 1
            stop_event.set()

    await asyncio.wait_for(scenario(), timeout=2)


def test_backoff_policy_validates_its_configuration() -> None:
    with pytest.raises(ValueError, match="base_delay_seconds"):
        BackoffPolicy(base_delay_seconds=0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        BackoffPolicy(base_delay_seconds=2.0, max_delay_seconds=1.0)
    with pytest.raises(ValueError, match="healthy_run_seconds"):
        BackoffPolicy(healthy_run_seconds=-1.0)

