"""Clock port: the only sanctioned way to read wall-clock time.

Domain-affecting timestamps are supplied by the caller, never read inline with
`datetime.now()`, so that tests can be deterministic and so that "when did this happen"
has a single source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Supplies timezone-aware timestamps."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware `datetime`."""
        ...


__all__ = ["Clock"]

