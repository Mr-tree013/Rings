"""Builders for the mail-settings tests: a scripted probe, a scripted credential source.

Neither builder carries a credential value, because the boundary they stand in for does not either:
the service asks *whether* one exists and the probe looks the value up itself, inside the adapter
package. A fake that took a value would be modelling a design this project deliberately does not
have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from assistant.domain.config import MailAccountConfig, parse_mail_account
from assistant.domain.mail_settings import (
    MailProbeEndpoint,
    MailProbeOutcome,
    MailProbeReport,
)


@dataclass
class ScriptedCredentials:
    """A credential source that answers about existence and naming, and nothing else."""

    inbound: bool = False
    outbound: bool = False
    asked: list[tuple[str, str]] = field(default_factory=list)

    def inbound_reference(self, account_id: str) -> str:
        return f"GROWING_ASSISTANT_MAIL_{account_id.upper().replace('-', '_')}_PASSWORD"

    def outbound_reference(self, account_id: str) -> str:
        return f"GROWING_ASSISTANT_MAIL_{account_id.upper().replace('-', '_')}_SMTP_PASSWORD"

    def has_inbound(self, account_id: str) -> bool:
        self.asked.append(("inbound", account_id))
        return self.inbound

    def has_outbound(self, account_id: str) -> bool:
        self.asked.append(("outbound", account_id))
        return self.outbound


@dataclass
class ScriptedProbe:
    """A probe that reports whatever a test decided, and records what it was asked to do."""

    imap_outcome: MailProbeOutcome = MailProbeOutcome.OK
    smtp_outcome: MailProbeOutcome = MailProbeOutcome.OK
    calls: list[str] = field(default_factory=list)

    async def probe_imap(self, account: MailAccountConfig) -> MailProbeReport:
        self.calls.append("imap")
        return MailProbeReport(
            endpoint=MailProbeEndpoint.IMAP,
            outcome=self.imap_outcome,
            detail="login+select-readonly+list",
            authenticated=self.imap_outcome is MailProbeOutcome.OK,
            mailbox=account.mailbox,
        )

    async def probe_smtp(self, account: MailAccountConfig) -> MailProbeReport:
        self.calls.append("smtp")
        return MailProbeReport(
            endpoint=MailProbeEndpoint.SMTP,
            outcome=self.smtp_outcome,
            detail="ehlo+tls+auth+noop",
            authenticated=self.smtp_outcome is MailProbeOutcome.OK,
        )


def account(identifier: str = "smail", **overrides: object) -> MailAccountConfig:
    """One inbound-only account, or whatever the overrides say."""
    values: dict[str, object] = {
        "id": identifier,
        "host": "imap.example.edu",
        "port": 993,
        "username": "student@example.edu",
        "mailbox": "INBOX",
        "enabled": True,
    }
    values.update(overrides)
    return parse_mail_account(values)


def sending_account(identifier: str = "smail") -> MailAccountConfig:
    """One fully configured account, inbound and outbound."""
    return account(
        identifier,
        smtp_host="smtp.example.edu",
        smtp_port=587,
        smtp_security="starttls",
        smtp_username="student@example.edu",
        from_address="student@example.edu",
    )


__all__ = [
    "ScriptedCredentials",
    "ScriptedProbe",
    "account",
    "sending_account",
]
