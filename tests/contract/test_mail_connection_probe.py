"""The real probes: what they do to a server, and what they refuse to have (ADR-0043 §9-11, §25-26).

Fakes drive both sides. The SMTP side uses a scripted `SmtpClient`, so a test can assert on the
*exact* sequence of commands — which is the only honest way to show that `DATA` is never reached.
The IMAP side uses a scripted `IMAP4_SSL`, for the same reason.
"""

from __future__ import annotations

import imaplib
import ssl

import pytest

from assistant.adapters.mail.connection_test import ImapConnectionProbe
from assistant.adapters.mail.smtp import SmtpConnectionProbe
from assistant.domain.mail_settings import MailProbeOutcome
from tests.support.mail_settings import account, sending_account


class ScriptedSmtpClient:
    """A server that records every command it is given."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.login_credentials: tuple[str, str] | None = None

    def ehlo(self) -> tuple[int, bytes]:
        self.commands.append("EHLO")
        return 250, b"ok"

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.commands.append("STARTTLS")
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.commands.append("LOGIN")
        self.login_credentials = (user, password)
        return 235, b"ok"

    def noop(self) -> tuple[int, bytes]:
        self.commands.append("NOOP")
        return 250, b"ok"

    def mail(self, sender: str) -> tuple[int, bytes]:  # pragma: no cover - must never run
        self.commands.append("MAIL")
        return 250, b"ok"

    def rcpt(self, recipient: str) -> tuple[int, bytes]:  # pragma: no cover - must never run
        self.commands.append("RCPT")
        return 250, b"ok"

    def data(self, message: bytes | str) -> tuple[int, bytes]:  # pragma: no cover - must never run
        self.commands.append("DATA")
        return 250, b"ok"

    def quit(self) -> tuple[int, bytes]:
        self.commands.append("QUIT")
        return 221, b"bye"


class ScriptedImapConnection:
    """An IMAP server that records the commands it is asked to run."""

    def __init__(self, *, select_status: str = "OK") -> None:
        self.commands: list[str] = []
        self.select_status = select_status
        self.login_credentials: tuple[str, str] | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.commands.append("LOGIN")
        self.login_credentials = (user, password)
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(f"SELECT:{mailbox}:readonly={readonly}")
        return self.select_status, [b"selected"]

    def list(self) -> tuple[str, list[bytes]]:
        self.commands.append("LIST")
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append("LOGOUT")
        return "BYE", [b"bye"]


@pytest.fixture
def smtp_client() -> ScriptedSmtpClient:
    return ScriptedSmtpClient()


@pytest.fixture
def imap_connection() -> ScriptedImapConnection:
    return ScriptedImapConnection()


# -------------------------------------------------------------------------- SMTP


async def test_the_smtp_probe_stops_at_noop(
    smtp_client: ScriptedSmtpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion the phase exists for: connect, TLS, auth, NOOP, quit — and no DATA."""
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD", "PROBE-SENTINEL")
    probe = SmtpConnectionProbe(client_factory=lambda target: smtp_client)

    report = await probe.probe(sending_account())

    assert report.outcome is MailProbeOutcome.OK
    assert report.authenticated is True
    assert smtp_client.commands == ["EHLO", "STARTTLS", "EHLO", "LOGIN", "NOOP", "QUIT"]
    for forbidden in ("MAIL", "RCPT", "DATA"):
        assert forbidden not in smtp_client.commands
    # And the credential it used came from the environment, not from its caller.
    assert smtp_client.login_credentials[1] == "PROBE-SENTINEL"


async def test_the_smtp_probe_reports_reachable_when_no_credential_exists(
    smtp_client: ScriptedSmtpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD", raising=False)
    probe = SmtpConnectionProbe(client_factory=lambda target: smtp_client)

    report = await probe.probe(sending_account())

    assert report.outcome is MailProbeOutcome.REACHABLE
    assert report.authenticated is False
    assert smtp_client.commands == ["EHLO", "STARTTLS", "EHLO", "QUIT"]


async def test_the_smtp_probe_does_not_invent_an_endpoint() -> None:
    probe = SmtpConnectionProbe()
    inbound_only = account()

    report = await probe.probe(inbound_only)

    assert report.outcome is MailProbeOutcome.NOT_CONFIGURED
    assert report.detail == "no_outbound_configuration"


async def test_a_refused_credential_is_reported_as_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smtplib

    class Refusing(ScriptedSmtpClient):
        def login(self, user: str, password: str) -> tuple[int, bytes]:
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD", "PROBE-SENTINEL")
    client = Refusing()
    probe = SmtpConnectionProbe(client_factory=lambda target: client)

    report = await probe.probe(sending_account())

    assert report.outcome is MailProbeOutcome.AUTHENTICATION_FAILED
    assert "PROBE-SENTINEL" not in report.detail
    assert client.commands == ["EHLO", "STARTTLS", "EHLO", "QUIT"]


# -------------------------------------------------------------------------- IMAP


def _imap_probe(connection: ScriptedImapConnection) -> ImapConnectionProbe:
    return ImapConnectionProbe(factory=lambda *args, **kwargs: connection)


async def test_the_imap_probe_selects_read_only(
    imap_connection: ScriptedImapConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "PROBE-SENTINEL")
    probe = _imap_probe(imap_connection)

    report = await probe.probe(sending_account())

    assert report.outcome is MailProbeOutcome.OK
    assert report.authenticated is True
    assert report.mailbox == "INBOX"
    assert imap_connection.commands == ["LOGIN", "SELECT:INBOX:readonly=True", "LIST", "LOGOUT"]
    for forbidden in ("STORE", "EXPUNGE", "APPEND", "COPY", "FETCH"):
        assert not [command for command in imap_connection.commands if forbidden in command]


async def test_the_imap_probe_reports_reachable_without_a_credential(
    imap_connection: ScriptedImapConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", raising=False)
    probe = _imap_probe(imap_connection)

    report = await probe.probe(sending_account())

    assert report.outcome is MailProbeOutcome.REACHABLE
    assert imap_connection.commands == ["LOGOUT"]


async def test_a_mailbox_that_cannot_be_opened_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "PROBE-SENTINEL")
    connection = ScriptedImapConnection(select_status="NO")

    report = await _imap_probe(connection).probe(sending_account())

    assert report.outcome is MailProbeOutcome.MAILBOX_UNAVAILABLE
    assert connection.commands == ["LOGIN", "SELECT:INBOX:readonly=True", "LOGOUT"]


async def test_a_server_that_cannot_be_reached_is_reported_as_a_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "PROBE-SENTINEL")

    def refuse(*args: object, **kwargs: object) -> ScriptedImapConnection:
        raise OSError("no route to host")

    report = await ImapConnectionProbe(factory=refuse).probe(sending_account())

    assert report.outcome is MailProbeOutcome.CONNECT_FAILED
    assert "no route to host" not in report.detail


async def test_a_tls_failure_is_reported_as_a_tls_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "PROBE-SENTINEL")

    def refuse_tls(*args: object, **kwargs: object) -> ScriptedImapConnection:
        raise ssl.SSLError("certificate verify failed")

    report = await ImapConnectionProbe(factory=refuse_tls).probe(sending_account())

    assert report.outcome is MailProbeOutcome.TLS_FAILED


def test_the_probe_outcome_vocabulary_has_no_way_to_describe_a_send() -> None:
    """There is no outcome for "the message was accepted", because no probe can produce one."""
    from assistant.domain.mail_settings import MailProbeOutcome

    words = {outcome.value for outcome in MailProbeOutcome}

    assert not {word for word in words if word in ("sent", "delivered", "queued", "accepted")}
    assert words == {
        "ok",
        "reachable",
        "credential_missing",
        "not_configured",
        "connect_failed",
        "tls_failed",
        "authentication_failed",
        "mailbox_unavailable",
        "server_error",
    }
    assert imaplib is not None
