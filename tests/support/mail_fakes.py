"""A scripted `MailSource` for mail tests.

It models a mailbox the way a server does — a UIDVALIDITY and a set of messages — and it records
every fetch request, so tests can assert *which* UIDs the service asked for, not just what it
stored afterwards. No protocol detail is simulated here: the IMAP adapter has its own contract
tests against a strict `IMAP4_SSL` fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from assistant.domain.errors import (
    MailAuthenticationError,
    MailConnectionError,
    MailProtocolError,
)
from assistant.ports.mail_source import (
    MailboxRemoteState,
    MailFetchBatch,
    MailFetchMode,
    MailFetchRequest,
    RemoteMailMessage,
)


@dataclass(frozen=True, slots=True)
class FakeRemoteMessage:
    """One message in the fake mailbox."""

    uid: int
    raw: bytes


@dataclass
class FakeMailSource:
    """A mailbox with a scripted UIDVALIDITY, messages and failures."""

    uidvalidity: int = 1
    messages: dict[int, bytes] = field(default_factory=dict)
    requests: list[MailFetchRequest] = field(default_factory=list)
    failure: Exception | None = None

    def add(self, uid: int, raw: bytes) -> FakeMailSource:
        """Add (or replace) one message."""
        self.messages[uid] = raw
        return self

    def rebuild(self, *, uidvalidity: int) -> FakeMailSource:
        """Simulate a server rebuilding the mailbox under a new UIDVALIDITY."""
        self.uidvalidity = uidvalidity
        return self

    async def fetch(self, request: MailFetchRequest) -> MailFetchBatch:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        state = MailboxRemoteState(
            mailbox_name=request.mailbox_name,
            uidvalidity=self.uidvalidity,
            highest_uid=max(self.messages, default=0),
        )
        changed = (
            request.expected_uidvalidity is not None
            and request.expected_uidvalidity != self.uidvalidity
        )
        if changed:
            return MailFetchBatch(state=state, messages=(), uidvalidity_changed=True)
        uids = self._select(request)
        fetched: list[RemoteMailMessage] = []
        oversize = 0
        for uid in uids:
            raw = self.messages[uid]
            if len(raw) > request.max_message_bytes:
                oversize += 1
                fetched.append(
                    RemoteMailMessage(
                        uid=uid,
                        size_bytes=len(raw),
                        raw=_headers_only(raw),
                        header_only=True,
                    )
                )
                continue
            fetched.append(
                RemoteMailMessage(
                    uid=uid, size_bytes=len(raw), raw=raw, header_only=False
                )
            )
        return MailFetchBatch(state=state, messages=tuple(fetched), oversize=oversize)

    def _select(self, request: MailFetchRequest) -> list[int]:
        available = sorted(self.messages)
        if request.mode is MailFetchMode.INCREMENTAL:
            after = request.after_uid or 0
            return [uid for uid in available if uid > after][: request.window]
        return available[-request.window :]


def _headers_only(raw: bytes) -> bytes:
    """The header block of a message, as `BODY.PEEK[HEADER]` would return it."""
    separator = raw.find(b"\n\n")
    if separator < 0:
        return raw
    return raw[: separator + 2]


def authentication_failure() -> MailAuthenticationError:
    """A scripted `login NO` outcome."""
    return MailAuthenticationError("the mail server rejected the credential")


def connection_failure() -> MailConnectionError:
    """A scripted DNS/transport failure."""
    return MailConnectionError("could not connect to the mail server (ConnectError)")


def protocol_failure() -> MailProtocolError:
    """A scripted unusable server response."""
    return MailProtocolError("the server's UIDVALIDITY is not a positive integer")


__all__ = [
    "FakeMailSource",
    "FakeRemoteMessage",
    "authentication_failure",
    "connection_failure",
    "protocol_failure",
]
