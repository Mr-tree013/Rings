"""Deterministic retry policy for event processing (ADR-0010).

Exponential backoff with a ceiling, and deliberately **no jitter** in this first version:
this is a single-user assistant, and reproducible scheduling is what makes the behaviour
testable. Jitter would only be worth adding if there were many competing workers.

    delay = min(base_delay * 2 ** (attempts - 1), max_delay)

where `attempts` is the number of attempts already started, so the first failure waits
exactly `base_delay`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY = timedelta(seconds=30)
DEFAULT_MAX_DELAY = timedelta(minutes=30)

_MAX_EXPONENT = 30
"""Guard so a large `attempts` value cannot build an absurd timedelta before the cap applies."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many attempts an event gets, and how long to wait between them."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: timedelta = DEFAULT_BASE_DELAY
    max_delay: timedelta = DEFAULT_MAX_DELAY

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay <= timedelta(0):
            raise ValueError("base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must not be shorter than base_delay")

    def delay_for(self, attempts: int) -> timedelta:
        """Return how long to wait after `attempts` attempts have started."""
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        exponent = min(attempts - 1, _MAX_EXPONENT)
        delay: timedelta = self.base_delay * (2**exponent)
        return min(delay, self.max_delay)

    def next_attempt_at(self, *, attempts: int, now: datetime) -> datetime:
        """Return the earliest time the next attempt may start."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now + self.delay_for(attempts)


__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY",
    "RetryPolicy",
]
