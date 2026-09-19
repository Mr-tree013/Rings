"""`SystemClock`: the production `Clock` implementation."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Reads the system clock in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["SystemClock"]

