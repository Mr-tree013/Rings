"""What a mail connection test may report, and nothing else (ADR-0043 §9-11, §25-26).

A connection test is a *diagnostic*. It answers "can this host reach that server, with this TLS
policy, with the credential the environment holds?" — and it answers it with a closed vocabulary, so
the product can say something useful and a reviewer can check that nothing was sent.

```text
OK                     connected, TLS negotiated, credential accepted
REACHABLE              connected and TLS is fine, but no credential was available to try
CREDENTIAL_MISSING     a credential was required for this step and the environment has none
NOT_CONFIGURED         this account has no inbound (or outbound) configuration at all
CONNECT_FAILED         the socket, DNS or the port did not answer
TLS_FAILED             the connection was refused by certificate or protocol policy
AUTHENTICATION_FAILED  the server rejected the credential
MAILBOX_UNAVAILABLE    the account is fine but the configured mailbox could not be opened
SERVER_ERROR           the server answered, and the answer was not a success
```

Two properties are the point. `REACHABLE` exists so that "the network path works, we just have no
password yet" is a *true* sentence rather than a failure dressed up as one. And nothing in this
module can describe having sent anything: there is no outcome for it, because the tests never do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from assistant.domain.errors import InvalidMailSettings

MAX_PROBE_DETAIL_CHARS = 300
"""How much of a server's own words a diagnostic may quote back."""


class MailProbeEndpoint(StrEnum):
    """Which half of an account was tested."""

    IMAP = "imap"
    SMTP = "smtp"


class MailProbeOutcome(StrEnum):
    """The closed result vocabulary above."""

    OK = "ok"
    REACHABLE = "reachable"
    CREDENTIAL_MISSING = "credential_missing"
    NOT_CONFIGURED = "not_configured"
    CONNECT_FAILED = "connect_failed"
    TLS_FAILED = "tls_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    MAILBOX_UNAVAILABLE = "mailbox_unavailable"
    SERVER_ERROR = "server_error"

    @property
    def is_success(self) -> bool:
        """Whether the network path itself is proven to work."""
        return self in (MailProbeOutcome.OK, MailProbeOutcome.REACHABLE)


@dataclass(frozen=True, slots=True)
class MailProbeReport:
    """One endpoint's diagnostic result, with no secret anywhere in it."""

    endpoint: MailProbeEndpoint
    outcome: MailProbeOutcome
    detail: str
    authenticated: bool = False
    mailbox: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, MailProbeEndpoint):
            raise InvalidMailSettings("a probe report names an endpoint")
        if not isinstance(self.outcome, MailProbeOutcome):
            raise InvalidMailSettings("a probe report names a known outcome")
        text = " ".join(self.detail.split())
        if not text:
            raise InvalidMailSettings("a probe report says something")
        if len(text) > MAX_PROBE_DETAIL_CHARS:
            text = text[: MAX_PROBE_DETAIL_CHARS - 1] + "…"
        object.__setattr__(self, "detail", text)
        if self.outcome is MailProbeOutcome.OK and not self.authenticated:
            raise InvalidMailSettings("only an authenticated probe may report OK")

    def to_payload(self) -> dict[str, object]:
        """The bounded representation a browser or a conversation may show."""
        return {
            "endpoint": self.endpoint.value,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "authenticated": self.authenticated,
            "mailbox": self.mailbox,
            "reachable": self.outcome.is_success,
        }


__all__ = [
    "MAX_PROBE_DETAIL_CHARS",
    "MailProbeEndpoint",
    "MailProbeOutcome",
    "MailProbeReport",
]
