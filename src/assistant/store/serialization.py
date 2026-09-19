"""Datetime serialisation shared by every store module.

Rule (spec §"时间存储"): the domain only ever sees timezone-aware `datetime` objects;
SQLite only ever stores UTC ISO 8601 text. Conversion lives here so that no repository
grows its own variant.
"""

from __future__ import annotations

from datetime import UTC, datetime

from assistant.store.errors import StoreError


def to_utc_iso(value: datetime) -> str:
    """Render an aware `datetime` as UTC ISO 8601 with microsecond precision.

    The fixed-width, always-UTC format is what makes `ORDER BY received_at` correct as
    plain text ordering.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise StoreError("datetime must be timezone-aware before it can be persisted")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_utc_iso(value: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC `datetime`."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError(f"stored datetime is not ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreError(f"stored datetime is not timezone-aware: {value!r}")
    return parsed.astimezone(UTC)


__all__ = ["from_utc_iso", "to_utc_iso"]

