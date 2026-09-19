"""Turning handler exceptions into bounded, storable failure summaries (ADR-0010).

Only a summary is persisted. Tracebacks belong to logs and audit, not to a database column
that every retry would otherwise grow without limit.
"""

from __future__ import annotations

MAX_ERROR_LENGTH = 2000
"""Upper bound for `last_error`, in characters."""


def format_event_failure(error: BaseException) -> str:
    """Return `ExceptionType: message`, whitespace-collapsed and length-capped.

    The result is safe to store in `inbound_events.last_error` and short enough to display
    in a terminal or on a phone.
    """
    message = " ".join(str(error).split())
    summary = f"{type(error).__name__}: {message}" if message else type(error).__name__
    if len(summary) <= MAX_ERROR_LENGTH:
        return summary
    return summary[: MAX_ERROR_LENGTH - 3] + "..."


__all__ = ["MAX_ERROR_LENGTH", "format_event_failure"]

