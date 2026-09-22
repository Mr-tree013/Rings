"""The `mail.send` executor: approved SMTP delivery, and nothing else (ADR-0024).

```text
supports(action)        pure, offline: account configured? credential present? payload valid?
        │
execute(action)         connect → EHLO → [STARTTLS → EHLO] → LOGIN → MAIL FROM → RCPT TO
        │                        → DATA → . → 2xx → QUIT
        └── the stage is tracked, because the stage is what decides FAILED vs UNKNOWN
```

Why the SMTP sequence is written out instead of calling `sendmail()`: a black box cannot tell the
caller *where* it failed, and the difference between "the server refused the sender" (nothing was
sent: FAILED) and "the connection died after the message data was written" (the message may exist:
UNKNOWN) is the difference between a safe retry and a duplicate email. Tracking the stage is what
makes that distinction honest.

Nothing here retries. Nothing here reads a model. Nothing here can be reached from a daemon, an
event handler or a scheduler: the only caller is `ActionExecutionService`, and only after an exact
human approval has been consumed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from enum import StrEnum
from typing import Protocol

from assistant.adapters.mail.credentials import available_smtp_password
from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.config import MailAccountConfig
from assistant.domain.errors import InvalidMailSend
from assistant.domain.execution import ExecutionOutcome
from assistant.domain.mail_send import MailSendPayload, validate_rfc_message_id
from assistant.domain.mail_settings import (
    MailProbeEndpoint,
    MailProbeOutcome,
    MailProbeReport,
)

LOGGER = logging.getLogger("assistant.mail")

MAIL_SEND_ACTION_TYPE = ActionType("mail.send")
"""The one action type this executor handles."""

DEFAULT_SMTP_TIMEOUT_SECONDS = 30


class _Stage(StrEnum):
    """How far the conversation got. The stage decides what an error means."""

    CONNECTING = "connecting"
    EHLO = "ehlo"
    STARTTLS = "starttls"
    LOGIN = "login"
    MAIL_FROM = "mail_from"
    RCPT = "rcpt"
    DATA_STARTED = "data_started"
    DATA_SENT = "data_sent"


_STAGES_BEFORE_DATA = frozenset(
    {
        _Stage.CONNECTING,
        _Stage.EHLO,
        _Stage.STARTTLS,
        _Stage.LOGIN,
        _Stage.MAIL_FROM,
        _Stage.RCPT,
    }
)
"""Failure here is definite: the message body was never handed over."""


class SmtpClient(Protocol):
    """The slice of `smtplib` this executor uses, so a strict fake can stand in."""

    def ehlo(self) -> tuple[int, bytes]: ...

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def noop(self) -> tuple[int, bytes]: ...

    def mail(self, sender: str) -> tuple[int, bytes]: ...

    def rcpt(self, recipient: str) -> tuple[int, bytes]: ...

    def data(self, message: bytes | str) -> tuple[int, bytes]: ...

    def quit(self) -> tuple[int, bytes]: ...


@dataclass(frozen=True, slots=True)
class _SmtpTarget:
    """Everything one send needs from configuration, and nothing secret beyond the credential."""

    host: str
    port: int
    security: str
    username: str
    password: str


class SmtpMailExecutor:
    """Sends one approved `mail.send` action over TLS."""

    action_type = MAIL_SEND_ACTION_TYPE

    def __init__(
        self,
        accounts: Mapping[str, MailAccountConfig],
        *,
        password_lookup: Callable[[str], str | None],
        timeout_seconds: int = DEFAULT_SMTP_TIMEOUT_SECONDS,
        client_factory: Callable[[_SmtpTarget], SmtpClient] | None = None,
    ) -> None:
        self._accounts = dict(accounts)
        self._password_lookup = password_lookup
        self._timeout = timeout_seconds
        self._client_factory = client_factory

    @property
    def accounts(self) -> tuple[str, ...]:
        """The account ids this executor knows about, sorted."""
        return tuple(sorted(self._accounts))

    def supports(self, action: ActionRequest) -> bool:
        """Whether this deployment could deliver `action` right now. Pure and offline."""
        if action.action_type != MAIL_SEND_ACTION_TYPE:
            return False
        try:
            payload = MailSendPayload.from_payload(action.payload)
        except InvalidMailSend:
            return False
        account = self._accounts.get(payload.account_id)
        if account is None or not account.smtp_configured:
            return False
        if account.from_address != payload.from_address:
            # The approved sender must be the configured sender: a mismatch means the account was
            # reconfigured after approval, and the action no longer describes what would happen.
            return False
        return self._password_lookup(payload.account_id) is not None

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        """Deliver one approved message and report how it ended."""
        payload = MailSendPayload.from_payload(action.payload)
        target = self._target(payload)
        raw = build_rfc822_message(payload)
        return await asyncio.to_thread(self._deliver, target, payload, raw)

    # ------------------------------------------------------------------ internals

    def _target(self, payload: MailSendPayload) -> _SmtpTarget:
        account = self._accounts.get(payload.account_id)
        if account is None or not account.smtp_configured:
            raise InvalidMailSend(
                f"mail account {payload.account_id!r} has no outbound configuration"
            )
        password = self._password_lookup(payload.account_id)
        if password is None:
            raise InvalidMailSend(
                f"mail account {payload.account_id!r} has no SMTP credential available"
            )
        return _SmtpTarget(
            host=account.smtp_host or "",
            port=account.smtp_port or 0,
            security=account.smtp_security or "starttls",
            username=account.smtp_username or "",
            password=password,
        )

    def _deliver(
        self, target: _SmtpTarget, payload: MailSendPayload, raw: bytes
    ) -> ExecutionOutcome:
        stage = _Stage.CONNECTING
        client: SmtpClient | None = None
        try:
            client = self._connect(target)
            stage = _Stage.EHLO
            client.ehlo()
            if target.security == "starttls":
                stage = _Stage.STARTTLS
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            stage = _Stage.LOGIN
            client.login(target.username, target.password)
            stage = _Stage.MAIL_FROM
            client.mail(payload.from_address)
            stage = _Stage.RCPT
            for recipient in payload.to_addresses:
                client.rcpt(recipient)
            stage = _Stage.DATA_STARTED
            # `data()` writes the message and the terminating dot, then reads the server's reply.
            client.data(raw)
            stage = _Stage.DATA_SENT
        except smtplib.SMTPAuthenticationError as exc:
            return _failed(stage, f"the SMTP server rejected the credential ({exc.smtp_code})")
        except smtplib.SMTPSenderRefused as exc:
            return _failed(stage, f"the SMTP server refused the sender ({exc.smtp_code})")
        except smtplib.SMTPRecipientsRefused as exc:
            refused = len(exc.recipients)
            return _failed(stage, f"the SMTP server refused every recipient ({refused})")
        except smtplib.SMTPDataError as exc:
            # A response-level rejection of the message data is definite: the server looked at it
            # and said no.
            return _failed(stage, f"the SMTP server rejected the message data ({exc.smtp_code})")
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return _ambiguous(stage, exc)
        finally:
            self._quit(client)
        LOGGER.info("mail send delivered")
        return ExecutionOutcome.succeeded()

    def _connect(self, target: _SmtpTarget) -> SmtpClient:
        if self._client_factory is not None:
            return self._client_factory(target)
        if target.security == "ssl":
            return smtplib.SMTP_SSL(
                target.host,
                target.port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(target.host, target.port, timeout=self._timeout)

    def _quit(self, client: SmtpClient | None) -> None:
        if client is None:
            return
        try:
            client.quit()
        except Exception:
            LOGGER.debug("smtp quit failed after the outcome was decided")


class SmtpConnectionProbe:
    """A connectivity test that cannot send anything (ADR-0043 §26).

    It lives in this module rather than in a new one on purpose: `smtplib` is allowed in exactly one
    file, and widening that pin to a second module would trade a real boundary for tidiness.

    The conversation stops at `NOOP`. There is no `mail()`, no `rcpt()` and no `data()` below, so a
    "test send" cannot exist even by accident — and because there is no message, there is nothing
    for an `ActionRequest`, an `Approval` or an `ExecutionRun` to describe. A test that needed an
    approval would be a send, not a test.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_SMTP_TIMEOUT_SECONDS,
        client_factory: Callable[[_SmtpTarget], SmtpClient] | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._client_factory = client_factory

    async def probe(self, account: MailAccountConfig) -> MailProbeReport:
        """Connect, TLS, EHLO, maybe LOGIN, NOOP, QUIT — and never DATA.

        The credential is looked up here, in the adapter package that already owns that lookup,
        rather than being handed in. The core layers therefore never hold a secret value on its way
        to a test, which is one fewer place it could be copied into.
        """
        if not account.smtp_configured:
            return _report(MailProbeOutcome.NOT_CONFIGURED, "no_outbound_configuration")
        credential = available_smtp_password(account.id)
        target = _SmtpTarget(
            host=account.smtp_host or "",
            port=account.smtp_port or 0,
            security=account.smtp_security or "starttls",
            username=account.smtp_username or "",
            password=credential or "",
        )
        return await asyncio.to_thread(self._probe_sync, target, credential)

    def _probe_sync(self, target: _SmtpTarget, password: str | None) -> MailProbeReport:
        client: SmtpClient | None = None
        try:
            client = self._connect(target)
            client.ehlo()
            if target.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        except smtplib.SMTPAuthenticationError as exc:  # pragma: no cover - TLS never authenticates
            return _report(MailProbeOutcome.AUTHENTICATION_FAILED, f"smtp_code={exc.smtp_code}")
        except ssl.SSLError as exc:
            return _report(MailProbeOutcome.TLS_FAILED, f"tls:{type(exc).__name__}")
        except smtplib.SMTPException as exc:
            return _report(MailProbeOutcome.SERVER_ERROR, f"protocol:{type(exc).__name__}")
        except OSError as exc:
            return _report(MailProbeOutcome.CONNECT_FAILED, f"socket:{type(exc).__name__}")
        try:
            if password is None:
                # Reachable and TLS-clean, and honestly not authenticated: there is nothing to try.
                return _report(MailProbeOutcome.REACHABLE, "reachable_without_credential")
            try:
                client.login(target.username, password)
            except smtplib.SMTPAuthenticationError as exc:
                return _report(
                    MailProbeOutcome.AUTHENTICATION_FAILED, f"smtp_code={exc.smtp_code}"
                )
            client.noop()
            return _report(MailProbeOutcome.OK, "ehlo+tls+auth+noop", authenticated=True)
        except ssl.SSLError as exc:
            return _report(MailProbeOutcome.TLS_FAILED, f"tls:{type(exc).__name__}")
        except smtplib.SMTPException as exc:
            return _report(MailProbeOutcome.SERVER_ERROR, f"protocol:{type(exc).__name__}")
        except OSError as exc:
            return _report(MailProbeOutcome.CONNECT_FAILED, f"socket:{type(exc).__name__}")
        finally:
            self._quit(client)

    def _connect(self, target: _SmtpTarget) -> SmtpClient:
        if self._client_factory is not None:
            return self._client_factory(target)
        if target.security == "ssl":
            return smtplib.SMTP_SSL(
                target.host,
                target.port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(target.host, target.port, timeout=self._timeout)

    def _quit(self, client: SmtpClient | None) -> None:
        if client is None:
            return
        with contextlib.suppress(Exception):
            client.quit()


def _report(
    outcome: MailProbeOutcome, detail: str, *, authenticated: bool = False
) -> MailProbeReport:
    """One SMTP diagnostic. The detail is a short technical reason, never prose.

    Product language is written by the application layer: an adapter that started rendering
    sentences would be a second place where the product's voice lives, and this module is allowed to
    speak about mail servers rather than to people.
    """
    return MailProbeReport(
        endpoint=MailProbeEndpoint.SMTP,
        outcome=outcome,
        detail=detail,
        authenticated=authenticated,
    )


def _failed(stage: _Stage, summary: str) -> ExecutionOutcome:
    """A definite refusal: nothing was delivered, and a retry would need a new approval."""
    return ExecutionOutcome.failed(
        f"{summary} before the message was accepted ({stage})"
    )


def _ambiguous(stage: _Stage, exc: BaseException) -> ExecutionOutcome:
    """An unclear ending. Before DATA it is still a definite failure; after it, it is not."""
    detail = f"{type(exc).__name__}: {exc}"
    if stage in _STAGES_BEFORE_DATA:
        return ExecutionOutcome.failed(
            f"{detail} before any message data was transferred ({stage})"
        )
    return ExecutionOutcome.unknown(
        f"{detail} during or after the message data was transferred ({stage}); "
        "the server may or may not have accepted it"
    )


def build_rfc822_message(payload: MailSendPayload) -> bytes:
    """Serialize the approved payload into the exact bytes that will be sent.

    Deterministic for a given payload: every header comes from the approved action, and the model
    has no opportunity to touch the message at send time. The `SMTP` policy is used so the result
    is already wire-formatted (CRLF line endings) instead of relying on `smtplib` to normalize it.
    """
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = payload.from_address
    message["To"] = ", ".join(payload.to_addresses)
    message["Subject"] = payload.subject
    message["Date"] = payload.date_header
    message["Message-ID"] = validate_rfc_message_id(payload.rfc_message_id)
    if payload.in_reply_to_header is not None:
        message["In-Reply-To"] = payload.in_reply_to_header
    if payload.references:
        message["References"] = " ".join(payload.references)
    message.set_content(payload.body_text, subtype="plain", charset="utf-8")
    return message.as_bytes()


def rfc2822_date(value: datetime) -> str:
    """Render one aware datetime as an RFC 5322 `Date` header value."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidMailSend("a send date must be timezone-aware")
    return format_datetime(value)


__all__ = [
    "DEFAULT_SMTP_TIMEOUT_SECONDS",
    "MAIL_SEND_ACTION_TYPE",
    "SmtpClient",
    "SmtpMailExecutor",
    "build_rfc822_message",
    "rfc2822_date",
]
