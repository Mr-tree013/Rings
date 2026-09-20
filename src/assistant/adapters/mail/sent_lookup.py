"""IMAP implementation of the Sent-mail lookup (ADR-0024).

Read-only and narrow: `connect → login → select(readonly=True) → UID SEARCH HEADER Message-ID →
UID FETCH BODY.PEEK[HEADER] → logout`, all in one worker thread, exactly like the inbound fetch.

The important behaviour is what it does *not* trust. A server's `SEARCH` may return prefix or
substring matches, so every candidate's header is fetched and its `Message-ID` is normalized and
compared byte-for-byte. A server answering `<x@example>` when we asked for `<x@example.evil>` is
not a match, and a second message carrying the same id makes the answer ambiguous rather than a
coin flip between two UIDs.
"""

from __future__ import annotations

import asyncio
import imaplib
from collections.abc import Callable

from assistant.adapters.mail.imap import ImapSession
from assistant.adapters.mail.parser import parse_header_only_message
from assistant.domain.mail import normalize_message_id
from assistant.ports.sent_mail_lookup import (
    SentMailLookupOutcome,
    SentMailLookupResult,
    SentMailMatch,
)

DEFAULT_IMAP_PORT = 993
MAX_SENT_CANDIDATES = 25
"""How many `SEARCH` candidates are fetched and checked before the answer is called ambiguous."""


class ImapSentMailLookup:
    """A `SentMailLookup` over one IMAP account's Sent mailbox."""

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
        return f"ImapSentMailLookup(host={self._host!r}, username={self._username!r})"

    async def find_message(
        self, *, mailbox_name: str, rfc_message_id: str
    ) -> SentMailLookupResult:
        """Run one whole IMAP conversation in a worker thread."""
        return await asyncio.to_thread(
            self._find_sync, mailbox_name, rfc_message_id
        )

    def _find_sync(self, mailbox_name: str, rfc_message_id: str) -> SentMailLookupResult:
        target = normalize_message_id(rfc_message_id)
        if target is None:  # pragma: no cover - callers pass a validated id
            return SentMailLookupResult(outcome=SentMailLookupOutcome.NOT_FOUND)
        with self._session() as session:
            uidvalidity = session.select_readonly(mailbox_name)
            # `HEADER` is a server-side hint; the exact comparison happens below.
            candidates = session.search_uids(f'HEADER Message-ID "{target}"')
            matches: list[SentMailMatch] = []
            for uid in candidates[:MAX_SENT_CANDIDATES]:
                parsed = parse_header_only_message(session.fetch_header(uid))
                if normalize_message_id(parsed.message_id_header) != target:
                    continue
                matches.append(
                    SentMailMatch(
                        mailbox_name=mailbox_name, uidvalidity=uidvalidity, uid=uid
                    )
                )
                if len(matches) > 1:
                    # Two exact matches is the answer; no need to read the rest.
                    return SentMailLookupResult(
                        outcome=SentMailLookupOutcome.AMBIGUOUS, matches=tuple(matches)
                    )
        if not matches:
            return SentMailLookupResult(outcome=SentMailLookupOutcome.NOT_FOUND)
        return SentMailLookupResult(
            outcome=SentMailLookupOutcome.FOUND, matches=tuple(matches)
        )

    def _session(self) -> ImapSession:
        return ImapSession(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            timeout_seconds=self._timeout,
            factory=self._factory,
        )

__all__ = ["MAX_SENT_CANDIDATES", "ImapSentMailLookup"]
