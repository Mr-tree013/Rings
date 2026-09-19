"""Supervision for daemon services (ADR-0013).

A supervisor keeps one service alive: ordinary failures are logged, waited out with
deterministic exponential backoff, and retried. Cancellation and shutdown are never treated
as failures, and a service's failure stays inside its own supervisor, so it cannot take its
siblings down.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.application.event_failures import format_event_failure
from assistant.ports.interval_waiter import IntervalWaiter

LOGGER = logging.getLogger("assistant.supervisor")

BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
HEALTHY_RUN_SECONDS = 60.0
"""A service that ran at least this long before failing gets a fresh backoff sequence."""

_MAX_EXPONENT = 30


class AsyncService(Protocol):
    """A long-running service the daemon can supervise."""

    name: str

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run until `stop_event` is set, then return."""
        ...


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Deterministic restart backoff: base, 2x base, 4x base, ... capped."""

    base_delay_seconds: float = BASE_BACKOFF_SECONDS
    max_delay_seconds: float = MAX_BACKOFF_SECONDS
    healthy_run_seconds: float = HEALTHY_RUN_SECONDS

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be shorter than base_delay_seconds")
        if self.healthy_run_seconds < 0:
            raise ValueError("healthy_run_seconds must not be negative")

    def delay_for(self, attempt: int) -> float:
        """Delay before restart number `attempt` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        exponent = min(attempt - 1, _MAX_EXPONENT)
        delay = float(self.base_delay_seconds * (2**exponent))
        return min(delay, self.max_delay_seconds)


async def supervise(
    service: AsyncService,
    stop_event: asyncio.Event,
    *,
    policy: BackoffPolicy | None = None,
    waiter: IntervalWaiter | None = None,
) -> None:
    """Keep `service` running until `stop_event` is set.

    Ordinary exceptions are logged (sanitised) and retried after a backoff delay; an
    unexpected normal return is treated the same way. `CancelledError` propagates untouched,
    and a shutdown during backoff returns immediately.
    """
    chosen_policy = policy if policy is not None else BackoffPolicy()
    chosen_waiter = waiter if waiter is not None else AsyncioIntervalWaiter()
    loop = asyncio.get_running_loop()
    attempt = 0
    while not stop_event.is_set():
        started_at = loop.time()
        failed = False
        try:
            await service.run_forever(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            reason = f"{type(exc).__name__}: {format_event_failure(exc)}"
        if stop_event.is_set():
            return
        attempt += 1
        if loop.time() - started_at >= chosen_policy.healthy_run_seconds:
            attempt = 1
        delay = chosen_policy.delay_for(attempt)
        if failed:
            LOGGER.warning(
                "service %s failed (%s); restarting in %.0fs",
                service.name,
                reason,
                delay,
            )
        else:
            LOGGER.warning(
                "service %s returned unexpectedly; restarting in %.0fs", service.name, delay
            )
        await chosen_waiter.wait(delay, stop_event)


__all__ = [
    "BASE_BACKOFF_SECONDS",
    "HEALTHY_RUN_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "AsyncService",
    "BackoffPolicy",
    "supervise",
]
