"""Run a block of test code as if the machine's timezone were something else.

The v1 CLI renders display timestamps in the *machine* local timezone
(`assistant.cli_support.format_local`), so a test that asserts a rendered `+08:00` string depends on
the host timezone unless it says so itself. This helper makes that dependency explicit and local to
the test that has it, instead of a suite-wide override that would hide future timezone bugs
everywhere else.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator


@contextlib.contextmanager
def machine_timezone(name: str) -> Iterator[None]:
    """Set the process timezone for the block, then restore whatever was there before.

    POSIX only: changing the timezone of a running process needs `time.tzset()`, and the reference
    test runtime is Linux / WSL.
    """
    if not hasattr(time, "tzset"):  # pragma: no cover - POSIX-only, like the rest of the suite
        raise RuntimeError("this test controls the process timezone, which needs time.tzset()")

    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
