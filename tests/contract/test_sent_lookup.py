"""Sent-mailbox lookup contract: read-only, and exact about the Message-ID (ADR-0024).

The lookup is driven through a strict fake `IMAP4_SSL`, because the behaviour that matters is not
"did it find something" but "did it *check*": a server-side `SEARCH` may return near misses, so
every candidate's header is compared byte-for-byte, and two exact matches are ambiguous rather
than a choice.
"""

from __future__ import annotations

import imaplib
import ssl
from typing import Any

import pytest

from assistant.adapters.mail.sent_lookup import ImapSentMailLookup
from assistant.domain.errors import (
    MailAuthenticationError,
    MailConnectionError,
    MailProtocolError,
)
from assistant.ports.sent_mail_lookup import SentMailLookupOutcome

TARGET = "<abc123@example.edu>"


class StrictImapServer:
    """A fake `IMAP4_SSL` that answers a Sent-mailbox lookup."""

    def __init__(
        self,
        *,
        search_uids: tuple[int, ...] = (42,),
        headers: dict[int, bytes] | None = None,
        uidvalidity: int | str = 7,
        login_ok: bool = True,
        select_ok: bool = True,
    ) -> None:
        self.search_uids_result = search_uids
        self.headers = headers if headers is not None else {42: _header(TARGET)}
        self.uidvalidity = uidvalidity
        self.login_ok = login_ok
        self.select_ok = select_ok
        self.calls: list[tuple[Any, ...]] = []
        self.searches: list[str] = []
        self.fetches: list[tuple[str, str]] = []
        self.selects: list[tuple[str, bool]] = []
        self.construction: dict[str, Any] = {}

    def factory(self, host: str, port: int, **kwargs: Any) -> StrictImapServer:
        self.construction = {"host": host, "port": port, **kwargs}
        return self

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", username, password))
        return ("OK", [b"in"]) if self.login_ok else ("NO", [b"no"])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        self.selects.append((mailbox, readonly))
        return ("OK", [b"1"]) if self.select_ok else ("NO", [b"no such mailbox"])

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.calls.append(("response", code))
        return ("UIDVALIDITY", [str(self.uidvalidity).encode()])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, args))
        if command == "SEARCH":
            self.searches.append(str(args[-1]))
            return "OK", [" ".join(str(uid) for uid in self.search_uids_result).encode()]
        target = str(args[0])
        self.fetches.append((target, str(args[1])))
        uid = int(target)
        raw = self.headers.get(uid, _header(None))
        return "OK", [(b"1 (UID " + str(uid).encode() + b" BODY[HEADER]", raw)]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return "BYE", [b"bye"]


def _header(message_id: str | None) -> bytes:
    lines = [b"From: student@example.edu", b"Subject: Re: SE lab deadline"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}".encode())
    return b"\r\n".join(lines) + b"\r\n\r\n"


def _lookup(server: StrictImapServer, **overrides: Any) -> ImapSentMailLookup:
    return ImapSentMailLookup(
        host=overrides.get("host", "imap.example.edu"),
        username=overrides.get("username", "student@example.edu"),
        password=overrides.get("password", "test-secret"),
        port=overrides.get("port", 993),
        timeout_seconds=overrides.get("timeout_seconds", 30),
        factory=server.factory,
    )


# ------------------------------------------------------------------ conversation


async def test_a_lookup_is_one_read_only_conversation() -> None:
    server = StrictImapServer()

    result = await _lookup(server).find_message(
        mailbox_name="Sent", rfc_message_id=TARGET
    )

    assert result.outcome is SentMailLookupOutcome.FOUND
    assert [call[0] for call in server.calls] == [
        "login",
        "select",
        "response",
        "uid",
        "uid",
        "logout",
    ]
    assert server.selects == [("Sent", True)]  # read-only: a lookup never marks mail read
    assert server.fetches == [("42", "(BODY.PEEK[HEADER])")]
    assert server.searches == [f'HEADER Message-ID "{TARGET}"']


async def test_the_lookup_uses_tls_with_verification() -> None:
    server = StrictImapServer()

    await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)

    context = server.construction["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


# ---------------------------------------------------------------------- exactness


async def test_a_near_miss_from_the_server_is_not_a_match() -> None:
    """The server may answer a prefix or substring match; only the exact header counts."""
    server = StrictImapServer(
        search_uids=(41, 42, 43),
        headers={
            41: _header("<abc123@example.evil>"),
            42: _header("<abc123x@example.edu>"),
            43: _header(TARGET),
        },
    )

    result = await _lookup(server).find_message(
        mailbox_name="Sent", rfc_message_id=TARGET
    )

    assert result.outcome is SentMailLookupOutcome.FOUND
    assert [match.uid for match in result.matches] == [43]
    assert result.match is not None and result.match.uidvalidity == 7


async def test_two_exact_matches_are_ambiguous() -> None:
    server = StrictImapServer(
        search_uids=(42, 43),
        headers={42: _header(TARGET), 43: _header(TARGET)},
    )

    result = await _lookup(server).find_message(
        mailbox_name="Sent", rfc_message_id=TARGET
    )

    assert result.outcome is SentMailLookupOutcome.AMBIGUOUS
    assert [match.uid for match in result.matches] == [42, 43]


async def test_no_candidates_is_not_found() -> None:
    server = StrictImapServer(search_uids=(), headers={})

    result = await _lookup(server).find_message(
        mailbox_name="Sent", rfc_message_id=TARGET
    )

    assert result.outcome is SentMailLookupOutcome.NOT_FOUND
    assert result.matches == ()
    assert server.fetches == []


async def test_candidates_that_do_not_match_are_not_found() -> None:
    server = StrictImapServer(
        search_uids=(42,), headers={42: _header("<something-else@example.edu>")}
    )

    result = await _lookup(server).find_message(
        mailbox_name="Sent", rfc_message_id=TARGET
    )

    assert result.outcome is SentMailLookupOutcome.NOT_FOUND


async def test_the_search_criterion_is_quoted_and_bounded() -> None:
    server = StrictImapServer(search_uids=())

    await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)

    assert server.searches[0] == f'HEADER Message-ID "{TARGET}"'
    assert "\r" not in server.searches[0] and "\n" not in server.searches[0]


# ------------------------------------------------------------------------- errors


async def test_a_rejected_credential_is_mapped() -> None:
    server = StrictImapServer(login_ok=False)

    with pytest.raises(MailAuthenticationError):
        await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)


async def test_an_unselectable_mailbox_is_a_protocol_error() -> None:
    server = StrictImapServer(select_ok=False)

    with pytest.raises(MailProtocolError):
        await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)


async def test_a_server_that_reports_no_uidvalidity_is_a_protocol_error() -> None:
    server = StrictImapServer(uidvalidity="nonsense")

    with pytest.raises(MailProtocolError):
        await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)


async def test_an_error_during_the_conversation_propagates() -> None:
    class Exploding(StrictImapServer):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == "SEARCH":
                raise imaplib.IMAP4.abort("connection lost")
            return super().uid(command, *args)

    server = Exploding()

    with pytest.raises(MailConnectionError):
        await _lookup(server).find_message(mailbox_name="Sent", rfc_message_id=TARGET)
