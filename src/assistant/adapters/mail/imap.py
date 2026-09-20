"""IMAP over TLS, read-only, behind the `MailSource` port (ADR-0020).

Everything protocol-shaped lives here: `imaplib`, `ssl`, sockets, the login sequence, the UID
commands and the response parsing. The rest of the project only sees a `MailFetchRequest` in and
a `MailFetchBatch` out.

Four behaviours are deliberate:

- **TLS only, verified.** `IMAP4_SSL` with `ssl.create_default_context()`; there is no plaintext
  mode, no STARTTLS and no "skip verification" switch;
- **read-only.** The mailbox is selected with `readonly=True` and bodies are fetched with
  `BODY.PEEK[]`, so a sync never marks a message as read;
- **one conversation per fetch, in one worker thread.** `connect → login → select → UID SEARCH →
  UID FETCH → logout` runs inside a single `asyncio.to_thread`, not one thread hop per UID;
- **UIDVALIDITY is read, never assumed.** If the server does not report a usable value, or the
  value differs from the caller's expectation, the fetch says so instead of continuing a cursor
  that no longer means anything.

Oversize messages are fetched header-only (`BODY.PEEK[HEADER]`): the caller still records a
durable message, a location and a cursor advance, with `header_only=True` and no body.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import re
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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

DEFAULT_IMAP_PORT = 993

_UIDVALIDITY_PATTERN = re.compile(rb"UIDVALIDITY\s+(\d+)")
_SIZE_PATTERN = re.compile(rb"RFC822\.SIZE\s+(\d+)")


class ImapSession:
    """The blocking IMAP conversation, isolated so tests can drive a strict fake.

    It is a thin wrapper, not an abstraction layer: it exists so the adapter's call *order* and
    command strings are testable without a live server.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout_seconds: int,
        factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._factory = factory
        self._connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> ImapSession:
        factory = self._factory or imaplib.IMAP4_SSL
        try:
            self._connection = factory(
                self._host,
                self._port,
                timeout=self._timeout,
                ssl_context=ssl.create_default_context(),
            )
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise MailConnectionError(
                f"could not connect to the mail server ({type(exc).__name__})"
            ) from exc
        self._login()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._logout()

    def _login(self) -> None:
        connection = self._require_connection()
        try:
            status, _ = connection.login(self._username, self._password)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailAuthenticationError(
                "the mail server rejected the credential"
            ) from exc
        if status != "OK":
            raise MailAuthenticationError("the mail server rejected the credential")

    def _logout(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        # A failed logout must not mask the real outcome of the conversation.
        with contextlib.suppress(Exception):
            connection.logout()

    def select_readonly(self, mailbox_name: str) -> int:
        """Select the mailbox read-only and return the UIDVALIDITY the server reported."""
        connection = self._require_connection()
        try:
            status, _ = connection.select(mailbox_name, readonly=True)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectionError(
                f"could not select the mailbox ({type(exc).__name__})"
            ) from exc
        if status != "OK":
            raise MailProtocolError(f"the server refused to select {mailbox_name!r}")
        try:
            code, payload = connection.response("UIDVALIDITY")
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectionError(
                f"could not read UIDVALIDITY ({type(exc).__name__})"
            ) from exc
        if code != "UIDVALIDITY" or not payload:
            raise MailProtocolError("the server did not report a UIDVALIDITY")
        raw = payload[0] if isinstance(payload[0], bytes) else str(payload[0]).encode()
        match = _UIDVALIDITY_PATTERN.search(raw)
        if match is not None:
            value = int(match.group(1))
        elif raw.strip().isdigit():
            # Some servers hand back the bare number rather than the ``UIDVALIDITY n`` line.
            value = int(raw.strip())
        else:
            raise MailProtocolError("the server's UIDVALIDITY is not a positive integer")
        if value < 1:
            raise MailProtocolError("the server's UIDVALIDITY is not a positive integer")
        return value

    def search_uids(self, criteria: str) -> list[int]:
        """Return the UIDs matching one search criterion, ascending."""
        connection = self._require_connection()
        try:
            # imaplib's documented idiom for "no CHARSET prefix" is `None`; its stubs type the
            # argument as `str`.
            status, payload = connection.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectionError(
                f"could not search the mailbox ({type(exc).__name__})"
            ) from exc
        if status != "OK":
            raise MailProtocolError("the server refused the UID SEARCH")
        return _parse_uid_list(payload)

    def fetch_sizes(self, uids: Sequence[int]) -> dict[int, int]:
        """Return `RFC822.SIZE` for each requested UID."""
        if not uids:
            return {}
        connection = self._require_connection()
        sequence = ",".join(str(uid) for uid in uids)
        try:
            status, payload = connection.uid("FETCH", sequence, "(RFC822.SIZE)")
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectionError(
                f"could not read message sizes ({type(exc).__name__})"
            ) from exc
        if status != "OK":
            raise MailProtocolError("the server refused to report message sizes")
        sizes: dict[int, int] = {}
        for item in _flatten(payload):
            if not isinstance(item, bytes):
                continue
            uid = _uid_from_metadata(item)
            match = _SIZE_PATTERN.search(item)
            if uid is not None and match is not None:
                sizes[uid] = int(match.group(1))
        return sizes

    def fetch_body(self, uid: int) -> bytes:
        """Fetch one full message with `BODY.PEEK[]`, which never sets the `\\Seen` flag."""
        return self._fetch_section(uid, "BODY.PEEK[]")

    def fetch_header(self, uid: int) -> bytes:
        """Fetch only a message's headers, again without touching flags."""
        return self._fetch_section(uid, "BODY.PEEK[HEADER]")

    def _fetch_section(self, uid: int, section: str) -> bytes:
        connection = self._require_connection()
        try:
            status, payload = connection.uid("FETCH", str(uid), f"({section})")
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectionError(
                f"could not fetch a message ({type(exc).__name__})"
            ) from exc
        if status != "OK":
            raise MailProtocolError("the server refused to fetch the message")
        for item in payload or ():
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        raise MailProtocolError("the server's fetch response carried no message data")

    def _require_connection(self) -> imaplib.IMAP4_SSL:
        if self._connection is None:  # pragma: no cover - guarded by the context manager
            raise MailConnectionError("the IMAP session is not connected")
        return self._connection


@dataclass(frozen=True, slots=True)
class _SelectedWindow:
    """The UIDs one fetch decided to read, and how they were chosen."""

    uids: tuple[int, ...]
    oversize: frozenset[int]


class ImapMailSource:
    """A `MailSource` over one IMAP account."""

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        port: int = DEFAULT_IMAP_PORT,
        timeout_seconds: int = 30,
        factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._timeout = timeout_seconds
        self._factory = factory

    def __repr__(self) -> str:  # pragma: no cover - defensive: never leak the secret
        return f"ImapMailSource(host={self._host!r}, username={self._username!r})"

    async def fetch(self, request: MailFetchRequest) -> MailFetchBatch:
        """Run one whole IMAP conversation in a worker thread."""
        return await asyncio.to_thread(self._fetch_sync, request)

    def _fetch_sync(self, request: MailFetchRequest) -> MailFetchBatch:
        with self._session() as session:
            uidvalidity = session.select_readonly(request.mailbox_name)
            changed = (
                request.expected_uidvalidity is not None
                and request.expected_uidvalidity != uidvalidity
            )
            uids = self._select_uids(session, request)
            highest = uids[-1] if uids else 0
            state = MailboxRemoteState(
                mailbox_name=request.mailbox_name,
                uidvalidity=uidvalidity,
                highest_uid=highest,
            )
            if changed:
                # Every stored UID belongs to the old UIDVALIDITY, so none of them may be
                # continued. The caller decides what to reconcile.
                return MailFetchBatch(state=state, messages=(), uidvalidity_changed=True)
            return self._fetch_messages(session, state, uids, request)

    def _session(self) -> ImapSession:
        return ImapSession(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            timeout_seconds=self._timeout,
            factory=self._factory,
        )

    def _select_uids(
        self, session: ImapSession, request: MailFetchRequest
    ) -> tuple[int, ...]:
        """Choose which UIDs to read, always ascending and always bounded."""
        if request.mode is MailFetchMode.INCREMENTAL:
            after = request.after_uid or 0
            uids = session.search_uids(f"UID {after + 1}:*")
            uids = [uid for uid in uids if uid > after]
            return tuple(sorted(uids)[: request.window])
        all_uids = sorted(session.search_uids("ALL"))
        # INITIAL and RECONCILIATION both take the newest bounded window of history.
        return tuple(all_uids[-request.window :])

    def _fetch_messages(
        self,
        session: ImapSession,
        state: MailboxRemoteState,
        uids: tuple[int, ...],
        request: MailFetchRequest,
    ) -> MailFetchBatch:
        sizes = session.fetch_sizes(uids)
        messages: list[RemoteMailMessage] = []
        oversize = 0
        for uid in uids:
            size = sizes.get(uid)
            if size is None:
                raise MailProtocolError(f"the server did not report the size of UID {uid}")
            if size > request.max_message_bytes:
                oversize += 1
                messages.append(
                    RemoteMailMessage(
                        uid=uid,
                        size_bytes=size,
                        raw=session.fetch_header(uid),
                        header_only=True,
                    )
                )
                continue
            messages.append(
                RemoteMailMessage(
                    uid=uid,
                    size_bytes=size,
                    raw=session.fetch_body(uid),
                    header_only=False,
                )
            )
        return MailFetchBatch(
            state=state, messages=tuple(messages), oversize=oversize
        )


def _flatten(payload: object) -> list[object]:
    """IMAP responses nest tuples and lists; flatten one level of each."""
    items: list[object] = []
    if not isinstance(payload, (list, tuple)):
        return [payload]
    for item in payload:
        if isinstance(item, (list, tuple)):
            items.extend(item)
        else:
            items.append(item)
    return items


def _uid_from_metadata(metadata: bytes) -> int | None:
    match = re.search(rb"UID\s+(\d+)", metadata)
    return None if match is None else int(match.group(1))


def _parse_uid_list(payload: object) -> list[int]:
    uids: list[int] = []
    for item in _flatten(payload) if isinstance(payload, (list, tuple)) else [payload]:
        if isinstance(item, bytes):
            uids.extend(int(token) for token in item.split() if token.isdigit())
    return sorted(uids)


__all__ = ["DEFAULT_IMAP_PORT", "ImapMailSource", "ImapSession"]
