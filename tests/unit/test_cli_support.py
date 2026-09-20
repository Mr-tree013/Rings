"""The CLI's display-timezone contract (v1).

`format_local` renders display timestamps in the *machine* local timezone. That is deliberate today:
the same durable instant prints differently on a Shanghai laptop and on a UTC server. These tests
pin that behavior down, so nobody has to guess whether it is intentional or an accident of one
developer's machine - and so a future decision to render in the configured planning timezone has to
change a test that says out loud what the current contract is.
"""

from __future__ import annotations

from datetime import UTC, datetime

from assistant.cli_support import format_local
from tests.support.timezone import machine_timezone

INSTANT = datetime(2026, 9, 23, 11, 0, tzinfo=UTC)


def test_format_local_renders_in_the_machine_timezone(shanghai_local_timezone: None) -> None:
    """A UTC instant is displayed in the machine's local time, not in UTC."""
    assert format_local(INSTANT) == "2026-09-23T19:00+08:00"


def test_format_local_follows_a_different_machine_timezone() -> None:
    """The same instant, on a machine whose local time is UTC, renders as UTC."""
    with machine_timezone("UTC"):
        assert format_local(INSTANT) == "2026-09-23T11:00+00:00"


def test_format_local_drops_seconds(shanghai_local_timezone: None) -> None:
    """Display uses minute precision; the exact instant is what is stored, not what is shown."""
    assert format_local(INSTANT.replace(second=42, microsecond=1)) == "2026-09-23T19:00+08:00"
