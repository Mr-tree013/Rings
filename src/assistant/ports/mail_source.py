"""MailSource port: read-only access to one remote mailbox (ADR-0020).

The port is deliberately narrow: one `fetch` call performs a whole IMAP conversation
(`connect → login → select readonly → UID search → UID fetch → logout`) and returns a batch of
raw messages plus the mailbox state that conversation observed. It is not a message bus, an
arbitrary query interface or a place to put protocol details: `imaplib`, `ssl` and `socket`
belong to the adapter behind it.

Two semantics the caller depends on:

- **read-only.** A fetch must never mark mail as read, and an oversize message is fetched
  header-only rather than skipped;
- **UIDVALIDITY is reported, never assumed.** The batch carries the UIDVALIDITY the server
  actually reported, and tells the caller when it differs from the expected one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class MailFetchMode(StrEnum):
    """Which slice of the mailbox a fetch should read.

    `INITIAL` takes the newest bounded window of history, `INCREMENTAL` takes everything after
    the caller's cursor, and `RECONCILIATION` re-reads a bounded window because the mailbox's
    UIDVALIDITY changed and every previously stored UID is meaningless.
    """

    INITIAL = "initial"
    INCREMENTAL = "incremental"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True, slots=True)
class MailboxRemoteState:
    """What the server said about the mailbox during this fetch."""

    mailbox_name: str
    uidvalidity: int
    highest_uid: int

    def __post_init__(self) -> None:
        if not self.mailbox_name.strip():
            raise ValueError("mailbox_name must not be blank")
        if self.uidvalidity < 1:
            raise ValueError("uidvalidity must be a positive integer")
        if self.highest_uid < 0:
            raise ValueError("highest_uid must not be negative")


@dataclass(frozen=True, slots=True)
class RemoteMailMessage:
    """One fetched message: its UID, its size, and either bytes or a header-only marker."""

    uid: int
    size_bytes: int
    raw: bytes | None
    header_only: bool = False

    def __post_init__(self) -> None:
        if self.uid < 1:
            raise ValueError("uid must be a positive integer")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if self.header_only and self.raw is None:
            raise ValueError("a header-only fetch still carries the header bytes")
        if self.raw is None and not self.header_only:
            raise ValueError("a fetched message needs raw bytes or a header-only marker")


@dataclass(frozen=True, slots=True)
class MailFetchRequest:
    """Everything one fetch needs. No credentials: those belong to the adapter's construction."""

    mailbox_name: str
    mode: MailFetchMode
    window: int
    max_message_bytes: int
    after_uid: int | None = None
    expected_uidvalidity: int | None = None

    def __post_init__(self) -> None:
        if not self.mailbox_name.strip():
            raise ValueError("mailbox_name must not be blank")
        if self.window < 1:
            raise ValueError("window must be a positive integer")
        if self.max_message_bytes < 1:
            raise ValueError("max_message_bytes must be a positive integer")
        if self.after_uid is not None and self.after_uid < 0:
            raise ValueError("after_uid must not be negative")


@dataclass(frozen=True, slots=True)
class MailFetchBatch:
    """What one fetch observed: the mailbox state and the messages it read."""

    state: MailboxRemoteState
    messages: tuple[RemoteMailMessage, ...] = ()
    uidvalidity_changed: bool = False
    oversize: int = 0

    @property
    def highest_fetched_uid(self) -> int:
        """The highest UID actually read in this batch, or 0 when it read nothing."""
        return max((message.uid for message in self.messages), default=0)


class MailSource(Protocol):
    """One configured mail account's remote mailbox."""

    async def fetch(self, request: MailFetchRequest) -> MailFetchBatch:
        """Read a bounded batch from the mailbox.

        Raises:
            MailCredentialsMissing: no credential is available for this account.
            MailAuthenticationError: the server rejected the credential.
            MailConnectionError: the server could not be reached or the connection failed.
            MailProtocolError: the server answered with something unusable.
        """
        ...


__all__ = [
    "MailFetchBatch",
    "MailFetchMode",
    "MailFetchRequest",
    "MailSource",
    "MailboxRemoteState",
    "RemoteMailMessage",
]
