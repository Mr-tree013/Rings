"""Read-only IMAP connectivity diagnostics, and the probe the composition root installs (ADR-0043).

```text
CONNECT (IMAP4_SSL, verified certificate)
   │  no credential available?  ──► REACHABLE  (the path works; nothing to authenticate with)
   ▼
LOGIN
   │
   ▼
SELECT <mailbox> READONLY
   │
   ▼
LOGOUT
```

Every step is chosen for what it *cannot* do. `select(..., readonly=True)` is the same call the
sync service already makes, so a test can never mark a message seen by accident. There is no
`store`, no `expunge`, no `append` and no `copy` in this module's IMAP vocabulary — not "we do not
call them", but "they are not here".

The SMTP half lives in `smtp.py`, because `smtplib` is allowed in exactly one file.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import ssl
from collections.abc import Callable

from assistant.adapters.mail.credentials import available_password
from assistant.adapters.mail.smtp import SmtpConnectionProbe
from assistant.domain.config import MailAccountConfig
from assistant.domain.mail_settings import (
    MailProbeEndpoint,
    MailProbeOutcome,
    MailProbeReport,
)

DEFAULT_PROBE_TIMEOUT_SECONDS = 20
"""Shorter than a sync's timeout on purpose: a person is waiting for this one."""


class ImapConnectionProbe:
    """Connect, authenticate, open the mailbox read-only, close."""

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
        factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._factory = factory

    async def probe(self, account: MailAccountConfig) -> MailProbeReport:
        """Run one whole IMAP conversation in a worker thread and report how it ended."""
        if not account.host or not account.username:
            return _report(MailProbeOutcome.NOT_CONFIGURED, "no_inbound_configuration")
        credential = available_password(account.id)
        return await asyncio.to_thread(self._probe_sync, account, credential)

    def _probe_sync(self, account: MailAccountConfig, password: str | None) -> MailProbeReport:
        factory = self._factory or imaplib.IMAP4_SSL
        connection: imaplib.IMAP4_SSL | None = None
        try:
            connection = factory(
                account.host,
                account.port,
                timeout=self._timeout,
                ssl_context=ssl.create_default_context(),
            )
        except ssl.SSLError as exc:
            return _report(MailProbeOutcome.TLS_FAILED, f"tls:{type(exc).__name__}")
        except OSError as exc:
            return _report(MailProbeOutcome.CONNECT_FAILED, f"socket:{type(exc).__name__}")
        except imaplib.IMAP4.error as exc:
            return _report(MailProbeOutcome.CONNECT_FAILED, f"imap:{type(exc).__name__}")
        try:
            if password is None:
                return _report(MailProbeOutcome.REACHABLE, "reachable_without_credential")
            try:
                status, _ = connection.login(account.username, password)
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
                return _report(
                    MailProbeOutcome.AUTHENTICATION_FAILED, f"imap:{type(exc).__name__}"
                )
            if status != "OK":
                return _report(MailProbeOutcome.AUTHENTICATION_FAILED, "status=not-ok")
            try:
                selected, _ = connection.select(account.mailbox, readonly=True)
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
                return _report(
                    MailProbeOutcome.MAILBOX_UNAVAILABLE, f"imap:{type(exc).__name__}"
                )
            if selected != "OK":
                return _report(MailProbeOutcome.MAILBOX_UNAVAILABLE, "select=not-ok")
            try:
                connection.list()
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
                return _report(
                    MailProbeOutcome.MAILBOX_UNAVAILABLE, f"list:{type(exc).__name__}"
                )
            return _report(
                MailProbeOutcome.OK,
                "login+select-readonly+list",
                authenticated=True,
                mailbox=account.mailbox,
            )
        finally:
            if connection is not None:
                # A failed logout must never mask the real outcome of the conversation.
                with contextlib.suppress(Exception):
                    connection.logout()


def _report(
    outcome: MailProbeOutcome,
    detail: str,
    *,
    authenticated: bool = False,
    mailbox: str | None = None,
) -> MailProbeReport:
    """One IMAP diagnostic. The detail is a short technical reason, never prose."""
    return MailProbeReport(
        endpoint=MailProbeEndpoint.IMAP,
        outcome=outcome,
        detail=detail,
        authenticated=authenticated,
        mailbox=mailbox,
    )


class MailConnectionProbes:
    """The pair of diagnostics the settings service depends on. It can only test."""

    def __init__(
        self,
        *,
        imap: ImapConnectionProbe | None = None,
        smtp: SmtpConnectionProbe | None = None,
    ) -> None:
        self._imap = imap if imap is not None else ImapConnectionProbe()
        self._smtp = smtp if smtp is not None else SmtpConnectionProbe()

    async def probe_imap(self, account: MailAccountConfig) -> MailProbeReport:
        """See `MailConnectionProbe.probe_imap`."""
        return await self._imap.probe(account)

    async def probe_smtp(self, account: MailAccountConfig) -> MailProbeReport:
        """See `MailConnectionProbe.probe_smtp`."""
        return await self._smtp.probe(account)


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ImapConnectionProbe",
    "MailConnectionProbes",
]
