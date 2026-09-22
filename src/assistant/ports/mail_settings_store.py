"""Where managed mail account metadata is persisted (ADR-0043 §17-18).

The application layer owns *what* may be stored and *whether* a change is valid. It does not own the
filesystem: writing a file is an adapter's job, and keeping it behind this port is what lets the
settings service be tested without a temp directory and reviewed without a `Path`.

There is no read method. The effective account list arrives through the configuration loader, which
is the single reader of both `config.toml` and the managed overlay — a second reader would be a
second chance for the two to disagree about precedence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from assistant.domain.config import MailAccountConfig


class MailSettingsStore(Protocol):
    """Persistence for the complete managed account list."""

    async def save_accounts(self, accounts: Sequence[MailAccountConfig]) -> None:
        """Replace the managed account list atomically.

        The list is always the *complete* effective set, never a delta, so a caller cannot
        accidentally drop an account by omitting it.

        Raises:
            InvalidAssistantConfig: the write could not be completed.
        """
        ...


__all__ = ["MailSettingsStore"]
