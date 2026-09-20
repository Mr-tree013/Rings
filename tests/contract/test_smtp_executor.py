"""SMTP executor contract: the exact conversation, and what each ending means (ADR-0024).

`smtplib` is not contactable in tests, so the executor is driven through a strict fake that records
the conversation. What is checked is the promise this adapter makes to the approval boundary: the
TLS sequence, the ordered envelope, the exact bytes, and — most importantly — whether an ending is
a definite `FAILED` (nothing was handed over) or an ambiguous `UNKNOWN` (it may have been).
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.adapters.mail.smtp import (
    MAIL_SEND_ACTION_TYPE,
    SmtpMailExecutor,
    build_rfc822_message,
)
from assistant.domain.action import ActionRequest
from assistant.domain.execution import ExecutionRunStatus
from assistant.domain.mail_send import MailSendPayload
from tests.support.mail_send import (
    FROM_ADDRESS,
    SEND_ACCOUNT_ID,
    SMTP_PASSWORD,
    TO_ADDRESS,
    StrictSmtpServer,
    build_sendable_draft,
    smtp_account,
)

NOON = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def _payload(**overrides: object) -> MailSendPayload:
    draft = build_sendable_draft()
    values: dict[str, object] = {
        "draft_id": draft.id,
        "draft_version": draft.version,
        "account_id": SEND_ACCOUNT_ID,
        "from_address": FROM_ADDRESS,
        "to_addresses": (TO_ADDRESS,),
        "subject": draft.subject,
        "body_text": draft.body_text,
        "rfc_message_id": "<abc123@example.edu>",
        "date_header": "Tue, 22 Sep 2026 09:00:00 +0000",
        "in_reply_to_header": "<original@example.edu>",
        "references": ("<original@example.edu>",),
    }
    values.update(overrides)
    return MailSendPayload(**values)  # type: ignore[arg-type]


def _action(payload: MailSendPayload | None = None) -> ActionRequest:
    return ActionRequest.prepare(
        case_id=uuid4(),
        action_type="mail.send",
        payload=(payload or _payload()).to_payload(),
        at=NOON,
    )


def _executor(
    server: StrictSmtpServer,
    *,
    password: str | None = SMTP_PASSWORD,
    **account_overrides: object,
) -> SmtpMailExecutor:
    return SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account(**account_overrides)},
        password_lookup=lambda account_id: password,
        client_factory=lambda target: server,
    )


# --------------------------------------------------------------------- preflight


def test_the_preflight_is_offline_and_answers_whether_this_host_can_send() -> None:
    executor = _executor(StrictSmtpServer())

    assert executor.action_type == MAIL_SEND_ACTION_TYPE
    assert executor.supports(_action()) is True
    assert executor.accounts == (SEND_ACCOUNT_ID,)


def test_the_preflight_refuses_an_unconfigured_or_unknown_account() -> None:
    assert _executor(StrictSmtpServer(), password=None).supports(_action()) is False
    account = smtp_account()
    without_smtp = SmtpMailExecutor(
        {"other": account},
        password_lookup=lambda account_id: SMTP_PASSWORD,
    )
    assert without_smtp.supports(_action()) is False


def test_the_preflight_refuses_a_reconfigured_sender() -> None:
    """The approved From must still be the configured From, or the action is stale."""
    executor = _executor(StrictSmtpServer(), from_address="someone-else@example.edu")

    assert executor.supports(_action()) is False


def test_the_preflight_refuses_a_payload_it_cannot_read() -> None:
    broken = ActionRequest.prepare(
        case_id=uuid4(),
        action_type="mail.send",
        payload={"schema_version": 1},
        at=NOON,
    )

    assert _executor(StrictSmtpServer()).supports(broken) is False


def test_the_preflight_never_contacts_anything() -> None:
    """`supports` is asked before an approval is consumed, so it must be pure."""
    server = StrictSmtpServer()
    executor = _executor(server)

    executor.supports(_action())

    assert server.calls == []


# ------------------------------------------------------------------- conversation


async def test_a_starttls_send_follows_the_exact_sequence() -> None:
    server = StrictSmtpServer()
    payload = _payload()

    outcome = await _executor(server).execute(_action(payload))

    assert outcome.status is ExecutionRunStatus.SUCCEEDED
    assert server.stages == ["ehlo", "starttls", "ehlo", "login", "mail", "rcpt", "data", "quit"]
    assert server.calls[3][1] == (smtp_account().smtp_username, SMTP_PASSWORD)
    assert server.calls[4][1] == FROM_ADDRESS
    assert server.calls[5][1] == TO_ADDRESS
    assert server.sent_bytes is not None
    assert b"Message-ID: <abc123@example.edu>" in server.sent_bytes
    assert b"In-Reply-To: <original@example.edu>" in server.sent_bytes
    assert b"References: <original@example.edu>" in server.sent_bytes
    assert b"Subject: Re: SE lab deadline" in server.sent_bytes
    assert b"To: ada@example.edu" in server.sent_bytes
    assert b"From: student@example.edu" in server.sent_bytes
    assert payload.body_text.encode()[:20] in server.sent_bytes


async def test_an_ssl_send_skips_starttls() -> None:
    server = StrictSmtpServer(security="ssl")

    outcome = await _executor(server, smtp_security="ssl").execute(_action())

    assert outcome.status is ExecutionRunStatus.SUCCEEDED
    assert server.stages == ["ehlo", "login", "mail", "rcpt", "data", "quit"]


async def test_starttls_uses_a_verified_default_context() -> None:
    server = StrictSmtpServer()

    await _executor(server).execute(_action())

    assert server.contexts
    assert isinstance(server.contexts[0], ssl.SSLContext)
    # Verification is on and a hostname check is required: this project never skips either.
    assert server.contexts[0].verify_mode is ssl.CERT_REQUIRED
    assert server.contexts[0].check_hostname is True


async def test_every_recipient_is_sent_in_order() -> None:
    server = StrictSmtpServer()
    payload = _payload(to_addresses=("first@example.edu", "second@example.edu"))

    await _executor(server).execute(_action(payload))

    assert [value for name, value in server.calls if name == "rcpt"] == [
        "first@example.edu",
        "second@example.edu",
    ]


async def test_the_serialized_message_is_deterministic() -> None:
    payload = _payload()

    first = build_rfc822_message(payload)
    second = build_rfc822_message(payload)

    assert first == second
    assert first.endswith(b"\r\n")
    assert b"Content-Type: text/plain; charset=\"utf-8\"" in first


# ----------------------------------------------------------------------- outcomes


@pytest.mark.parametrize(
    ("error", "stage"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "login"),
        (smtplib.SMTPSenderRefused(550, b"no such sender", "student@example.edu"), "mail"),
        (
            smtplib.SMTPRecipientsRefused({"ada@example.edu": (550, b"no such user")}),
            "rcpt",
        ),
        (smtplib.SMTPDataError(554, b"rejected"), "data"),
    ],
)
async def test_a_definite_refusal_before_or_at_data_is_failed(
    error: Exception, stage: str
) -> None:
    """The server looked at something and said no: nothing was delivered."""
    server = StrictSmtpServer(fail_at=stage, error=error)

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.FAILED
    assert outcome.error_summary is not None


@pytest.mark.parametrize("stage", ["ehlo", "starttls", "login", "mail", "rcpt"])
async def test_a_transport_failure_before_data_is_failed(stage: str) -> None:
    """A connection lost before the body was handed over cannot have delivered anything."""
    server = StrictSmtpServer(fail_at=stage, error=smtplib.SMTPServerDisconnected("gone"))

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.FAILED


async def test_a_timeout_before_data_is_failed() -> None:
    server = StrictSmtpServer(fail_at="mail", error=TimeoutError("timed out"))

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.FAILED


async def test_a_loss_during_data_is_unknown() -> None:
    """The body may have been written before the connection died: never claim a failure."""
    server = StrictSmtpServer(fail_at="data", error=smtplib.SMTPServerDisconnected("dropped"))

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.UNKNOWN
    assert outcome.error_summary is not None
    assert "may or may not" in outcome.error_summary


async def test_a_protocol_error_after_data_is_unknown() -> None:
    server = StrictSmtpServer(fail_at="data", error=smtplib.SMTPException("protocol confusion"))

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.UNKNOWN


async def test_the_executor_never_logs_or_returns_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    server = StrictSmtpServer()
    with caplog.at_level(logging.DEBUG):
        outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.SUCCEEDED
    assert SMTP_PASSWORD not in caplog.text


async def test_a_failed_quit_does_not_change_the_outcome() -> None:
    class BrokenQuit(StrictSmtpServer):
        def quit(self) -> tuple[int, bytes]:
            self.calls.append(("quit", None))
            raise smtplib.SMTPServerDisconnected("gone during quit")

    server = BrokenQuit()

    outcome = await _executor(server).execute(_action())

    assert outcome.status is ExecutionRunStatus.SUCCEEDED
