"""IMAP adapter contract: the commands it sends, and the errors it maps (ADR-0020).

`imaplib` is not a mockable HTTP client, so the adapter is driven through a strict fake
`IMAP4_SSL` that records the exact call sequence. What is checked here is the promise the
adapter makes to the rest of the project: TLS-only construction, read-only select, `BODY.PEEK`
fetches that never set `\\Seen`, header-only reads for oversize messages, real UIDVALIDITY
handling, and provider-neutral errors.
"""

from __future__ import annotations

import imaplib
import ssl
from typing import Any

import pytest

from assistant.adapters.mail.imap import ImapMailSource
from assistant.domain.errors import (
    MailAuthenticationError,
    MailConnectionError,
    MailProtocolError,
)
from assistant.ports.mail_source import MailFetchMode, MailFetchRequest

RAW_SMALL = b"From: a@b\r\nSubject: small\r\n\r\nbody!"
RAW_LARGE = b"From: a@b\r\nSubject: large\r\n\r\n" + b"x" * 5000


class StrictImapServer:
    """A fake `IMAP4_SSL` that records commands and answers realistically."""

    def __init__(
        self,
        *,
        uids: tuple[int, ...] = (100, 101, 102),
        sizes: dict[int, int] | None = None,
        uidvalidity: int | str | None = 42,
        login_ok: bool = True,
        select_ok: bool = True,
        fetch_ok: bool = True,
        raises: Exception | None = None,
    ) -> None:
        self.uids = uids
        self.sizes = sizes or {100: len(RAW_SMALL), 101: len(RAW_LARGE), 102: 130}
        self.uidvalidity = uidvalidity
        self.login_ok = login_ok
        self.select_ok = select_ok
        self.fetch_ok = fetch_ok
        self.raises = raises
        self.calls: list[tuple[Any, ...]] = []
        self.selects: list[tuple[str, bool]] = []
        self.fetches: list[tuple[str, str]] = []
        self.searches: list[str] = []
        self.construction: dict[str, Any] = {}

    # -- construction ---------------------------------------------------------

    def factory(self, host: str, port: int, **kwargs: Any) -> StrictImapServer:
        self.construction = {"host": host, "port": port, **kwargs}
        return self

    # -- the IMAP surface the adapter uses ------------------------------------

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", username, password))
        if isinstance(self.raises, imaplib.IMAP4.error):
            raise self.raises
        return ("OK", [b"logged in"]) if self.login_ok else ("NO", [b"bad credentials"])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        self.selects.append((mailbox, readonly))
        if self.raises is not None and not isinstance(self.raises, imaplib.IMAP4.error):
            raise self.raises
        return ("OK", [b"1"]) if self.select_ok else ("NO", [b"no such mailbox"])

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.calls.append(("response", code))
        if self.uidvalidity is None:
            return ("UIDVALIDITY", [])
        if isinstance(self.uidvalidity, str):
            return ("UIDVALIDITY", [self.uidvalidity.encode()])
        return ("UIDVALIDITY", [str(self.uidvalidity).encode()])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, args))
        if command == "SEARCH":
            self.searches.append(str(args[-1]))
            return "OK", [" ".join(str(uid) for uid in self.uids).encode()]
        if command == "FETCH":
            target, section = str(args[0]), str(args[1])
            self.fetches.append((target, section))
            if not self.fetch_ok:
                return "NO", [b"refused"]
            if "RFC822.SIZE" in section:
                return "OK", [
                    (f"1 (UID {uid} RFC822.SIZE {self.sizes[uid]})".encode(), b"")
                    for uid in (int(part) for part in target.split(","))
                ]
            uid = int(target)
            raw = RAW_LARGE if uid == 101 else RAW_SMALL
            if "HEADER" in section:
                return "OK", [(b"1 (UID " + str(uid).encode() + b" BODY[HEADER] {10}", raw[:10])]
            return "OK", [(b"1 (UID " + str(uid).encode() + b" BODY[] {5}", raw)]
        return "OK", []

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return "BYE", [b"bye"]


def _source(server: StrictImapServer, **overrides: Any) -> ImapMailSource:
    return ImapMailSource(
        host=overrides.get("host", "imap.example.edu"),
        username=overrides.get("username", "student@example.edu"),
        password=overrides.get("password", "test-secret"),
        port=overrides.get("port", 993),
        timeout_seconds=overrides.get("timeout_seconds", 30),
        factory=server.factory,
    )


def _request(**overrides: Any) -> MailFetchRequest:
    values: dict[str, Any] = {
        "mailbox_name": "INBOX",
        "mode": MailFetchMode.INITIAL,
        "window": 10,
        "max_message_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return MailFetchRequest(**values)


# ----------------------------------------------------------------- call sequence


async def test_a_fetch_is_one_read_only_conversation() -> None:
    server = StrictImapServer()

    await _source(server).fetch(_request())

    assert [call[0] for call in server.calls] == [
        "login",
        "select",
        "response",
        "uid",
        "uid",
        "uid",
        "uid",
        "uid",
        "logout",
    ]
    assert server.construction["host"] == "imap.example.edu"
    assert server.construction["port"] == 993
    assert server.construction["timeout"] == 30
    assert isinstance(server.construction["ssl_context"], ssl.SSLContext)
    assert server.construction["ssl_context"].verify_mode is ssl.CERT_REQUIRED


async def test_the_mailbox_is_selected_read_only() -> None:
    server = StrictImapServer()

    await _source(server).fetch(_request())

    assert server.selects == [("INBOX", True)]


async def test_bodies_are_fetched_with_peek_and_never_mark_messages_read() -> None:
    server = StrictImapServer()

    await _source(server).fetch(_request())

    sections = [section for _, section in server.fetches]
    assert sections == ["(RFC822.SIZE)", "(BODY.PEEK[])", "(BODY.PEEK[])", "(BODY.PEEK[])"]
    assert all("BODY.PEEK[]" in section for section in sections[1:])
    for forbidden in ("BODY[]", "RFC822", "BODY[TEXT]"):
        assert all(section != f"({forbidden})" for section in sections)


async def test_an_oversize_message_is_fetched_header_only() -> None:
    server = StrictImapServer()

    batch = await _source(server).fetch(_request(max_message_bytes=1024))

    assert batch.oversize == 1
    by_uid = {message.uid: message for message in batch.messages}
    assert by_uid[101].header_only is True
    assert by_uid[100].header_only is False
    assert ("101", "(BODY.PEEK[HEADER])") in server.fetches
    assert ("101", "(BODY.PEEK[])") not in server.fetches


async def test_an_incremental_fetch_asks_only_for_uids_after_the_cursor() -> None:
    server = StrictImapServer(uids=(101, 102, 103))
    server.sizes = dict.fromkeys((101, 102, 103), len(RAW_SMALL))

    await _source(server).fetch(
        _request(mode=MailFetchMode.INCREMENTAL, after_uid=100, expected_uidvalidity=42)
    )

    assert server.searches == ["UID 101:*"]


async def test_an_initial_fetch_takes_the_newest_bounded_window() -> None:
    server = StrictImapServer(uids=tuple(range(1, 101)))
    server.sizes = dict.fromkeys(range(1, 101), len(RAW_SMALL))

    batch = await _source(server).fetch(_request(window=5))

    assert server.searches == ["ALL"]
    assert [message.uid for message in batch.messages] == [96, 97, 98, 99, 100]


async def test_a_uidvalidity_change_stops_the_cursor_dead() -> None:
    server = StrictImapServer(uidvalidity=99)

    batch = await _source(server).fetch(
        _request(mode=MailFetchMode.INCREMENTAL, after_uid=100, expected_uidvalidity=42)
    )

    assert batch.uidvalidity_changed is True
    assert batch.state.uidvalidity == 99
    assert batch.messages == ()  # nothing from the old UIDVALIDITY is trusted
    assert all("RFC822.SIZE" not in section for _, section in server.fetches)


async def test_a_reconciliation_fetch_reads_a_bounded_window() -> None:
    server = StrictImapServer(uids=(1, 2, 3, 4, 5))
    server.sizes = dict.fromkeys((1, 2, 3, 4, 5), 50)

    batch = await _source(server).fetch(
        _request(mode=MailFetchMode.RECONCILIATION, window=2)
    )

    assert [message.uid for message in batch.messages] == [4, 5]
    assert batch.state.highest_uid == 5


# ------------------------------------------------------------------- UIDVALIDITY


async def test_a_missing_uidvalidity_is_a_protocol_error() -> None:
    server = StrictImapServer(uidvalidity=None)

    with pytest.raises(MailProtocolError):
        await _source(server).fetch(_request())


@pytest.mark.parametrize("value", ("0", "not-a-number", "UIDVALIDITY nope"))
async def test_an_unusable_uidvalidity_is_never_assumed(value: str) -> None:
    server = StrictImapServer(uidvalidity=value)

    with pytest.raises(MailProtocolError):
        await _source(server).fetch(_request())


async def test_a_bare_uidvalidity_number_is_accepted() -> None:
    server = StrictImapServer(uidvalidity="12345")
    server.uidvalidity = "12345"

    batch = await _source(server).fetch(_request())

    assert batch.state.uidvalidity == 12345


# ------------------------------------------------------------------ error mapping


async def test_a_rejected_login_maps_to_an_authentication_error() -> None:
    server = StrictImapServer(login_ok=False)

    with pytest.raises(MailAuthenticationError):
        await _source(server).fetch(_request())


async def test_an_imap_error_during_login_maps_to_an_authentication_error() -> None:
    server = StrictImapServer(raises=imaplib.IMAP4.error("LOGIN failed"))

    with pytest.raises(MailAuthenticationError):
        await _source(server).fetch(_request())


async def test_a_transport_failure_maps_to_a_connection_error() -> None:
    class Broken:
        def __call__(self, *args: Any, **kwargs: Any) -> object:
            raise ConnectionRefusedError("no route to host")

    source = ImapMailSource(
        host="imap.example.edu", username="u", password="p", factory=Broken()  # type: ignore[arg-type]
    )

    with pytest.raises(MailConnectionError):
        await source.fetch(_request())


async def test_a_failed_select_is_a_protocol_error() -> None:
    server = StrictImapServer(select_ok=False)

    with pytest.raises(MailProtocolError):
        await _source(server).fetch(_request())


async def test_a_refused_fetch_is_a_protocol_error() -> None:
    server = StrictImapServer(fetch_ok=False)

    with pytest.raises(MailProtocolError):
        await _source(server).fetch(_request())


async def test_the_adapter_never_retries_a_failed_login() -> None:
    server = StrictImapServer(login_ok=False)

    with pytest.raises(MailAuthenticationError):
        await _source(server).fetch(_request())

    assert [call[0] for call in server.calls].count("login") == 1


def test_the_adapter_repr_never_leaks_the_credential() -> None:
    source = _source(StrictImapServer(), password="super-secret")

    assert "super-secret" not in repr(source)
    assert "imap.example.edu" in repr(source)
