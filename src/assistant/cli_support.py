"""Shared CLI helpers: consoles, error reporting and strict timestamp parsing (ADR-0014).

Timestamps from the user are **never guessed**: an input must be ISO 8601 and must carry an
explicit offset. `2026-09-21T10:00:00+08:00` and `2026-09-21T02:00:00Z` are accepted;
`2026-09-21 10:00`, `tomorrow` and `next Friday` are rejected. Machine-local time is never
assumed for parsing; natural-language dates belong to a future interpreter, not the CLI.
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
error_console = Console(stderr=True)

ISO_EXAMPLE = "2026-09-21T10:00:00+08:00"


def fail(message: str, code: int = 1) -> NoReturn:
    """Report a user-facing failure and exit without a traceback.

    The message is escaped: error text often contains things like `[planning]`, which would
    otherwise be swallowed as Rich markup.
    """
    error_console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code=code)


def parse_aware_datetime(value: str, *, field_name: str = "datetime") -> datetime:
    """Parse an ISO 8601 timestamp that must include a timezone offset.

    Raises:
        ValueError: the text is not ISO 8601, or it carries no offset.
    """
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be ISO 8601 with an explicit offset "
            f"(e.g. {ISO_EXAMPLE}): {text!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{field_name} must include a timezone offset "
            f"(e.g. {ISO_EXAMPLE}); local time is never assumed"
        )
    return parsed


def format_local(moment: datetime) -> str:
    """Render a timestamp in the machine's local timezone for display."""
    return moment.astimezone().isoformat(timespec="minutes")


def short_id(value: object) -> str:
    """The 8-character id prefix used in list output."""
    return str(value)[:8]


__all__ = [
    "ISO_EXAMPLE",
    "console",
    "error_console",
    "fail",
    "format_local",
    "parse_aware_datetime",
    "short_id",
]
