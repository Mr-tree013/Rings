"""The diagnostic boundary for mail connectivity (ADR-0043 §9-11, §25-26).

Two protocols, deliberately separate, and neither of them carries a credential *value*:

* `MailConnectionProbe` speaks to a server. It receives an account and looks up whatever it needs
  itself, in the adapter package that already knows how; it can do nothing else, because there is no
  method on it that sends, deletes, flags or fetches a message.
* `MailCredentialSource` reports *whether* a credential exists and what reference names it.

That split is the whole reason the core layers can answer "is a password configured?" without ever
holding one. A port that accepted a credential would put the value in a domain-level signature, and
from there it is one refactor away from a dataclass field. A port that accepts an account id cannot.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.config import MailAccountConfig
from assistant.domain.mail_settings import MailProbeReport


class MailCredentialSource(Protocol):
    """Where mail credentials come from, without disclosing them."""

    def inbound_reference(self, account_id: str) -> str:
        """The name of the environment variable that holds the inbound secret."""
        ...

    def outbound_reference(self, account_id: str) -> str:
        """The name of the environment variable that holds the outbound secret."""
        ...

    def has_inbound(self, account_id: str) -> bool:
        """Whether an inbound credential is available right now."""
        ...

    def has_outbound(self, account_id: str) -> bool:
        """Whether an outbound credential is available right now."""
        ...


class MailConnectionProbe(Protocol):
    """Read-only connectivity diagnostics for one account."""

    async def probe_imap(self, account: MailAccountConfig) -> MailProbeReport:
        """Connect, negotiate TLS, authenticate if this host has a credential, close.

        The mailbox is selected read-only and never mutated: no delete, no flag change, no fetch of
        a body. No credential available means a `REACHABLE` result rather than a failure.
        """
        ...

    async def probe_smtp(self, account: MailAccountConfig) -> MailProbeReport:
        """Connect, negotiate TLS, EHLO, authenticate if possible, NOOP, QUIT.

        It never issues `MAIL FROM`, `RCPT TO` or `DATA`, and it never creates an `ActionRequest`:
        a connectivity test is not a send, and there is no approval anywhere near it.
        """
        ...


__all__ = ["MailConnectionProbe", "MailCredentialSource"]
