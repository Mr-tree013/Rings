"""One definition of "an instant this project accepts" (ADR-0018).

Everything that carries time — CLI options, model-produced datetimes, planner windows — must be
ISO 8601 *with* an offset. Local time is never assumed, because the machine's timezone is not
the user's timezone.

Only the parsing rule lives here; each caller owns the wording of its own error, so the CLI can
keep its option-specific message while the interpreter reports a semantic problem.
"""

from __future__ import annotations

from datetime import datetime


def parse_iso_instant(text: str) -> datetime:
    """Parse ISO 8601 text into a timezone-aware instant.

    Raises:
        ValueError: the text is not ISO 8601, or it carries no offset. The message says which.
    """
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 timestamp: {text.strip()!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp carries no timezone offset: {text.strip()!r}")
    return parsed


__all__ = ["parse_iso_instant"]
