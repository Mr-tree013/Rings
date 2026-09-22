"""Mail credential lookup: environment only, derived name, never configured (ADR-0020, ADR-0024).

An account password or app password is a secret, and secrets do not belong in a configuration
file that gets copied between machines and pasted into issues. The variable name is *derived*
from the account id, so a user cannot point the host at an arbitrary variable, and nothing here
ever logs, returns or stores the value.

Inbound and outbound credentials are separate variables on purpose: an IMAP app password and an
SMTP app password are often different values, and assuming they match would either leak one into
the other's protocol or break silently when a provider rotates only one of them.
"""

from __future__ import annotations

import os

from assistant.domain.errors import MailCredentialsMissing
from assistant.domain.mail import MailAccountId, validate_account_id

PREFIX = "GROWING_ASSISTANT_MAIL"


def password_env_var(account_id: MailAccountId) -> str:
    """The environment variable name that holds one account's secret.

    `smail` → `GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD`; hyphens become underscores and the id is
    upper-cased.
    """
    derived = validate_account_id(account_id).upper().replace("-", "_")
    return f"{PREFIX}_{derived}_PASSWORD"


def available_password(account_id: MailAccountId) -> str | None:
    """Return the secret from the environment, or `None` when it is unset or blank."""
    value = os.environ.get(password_env_var(account_id), "").strip()
    return value or None


def require_password(account_id: MailAccountId) -> str:
    """Return the secret or explain exactly which variable is missing.

    Raises:
        MailCredentialsMissing: no credential is available for this account.
    """
    value = available_password(account_id)
    if value is None:
        raise MailCredentialsMissing(
            f"no credential available for mail account {account_id!r}; "
            f"set {password_env_var(account_id)} in the environment"
        )
    return value


def smtp_password_env_var(account_id: MailAccountId) -> str:
    """The environment variable that holds one account's outbound secret.

    `smail` → `GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD`.
    """
    derived = validate_account_id(account_id).upper().replace("-", "_")
    return f"{PREFIX}_{derived}_SMTP_PASSWORD"


def available_smtp_password(account_id: MailAccountId) -> str | None:
    """Return the SMTP secret from the environment, or `None` when it is unset or blank."""
    value = os.environ.get(smtp_password_env_var(account_id), "").strip()
    return value or None


def require_smtp_password(account_id: MailAccountId) -> str:
    """Return the SMTP secret or explain exactly which variable is missing.

    Raises:
        MailCredentialsMissing: no outbound credential is available for this account.
    """
    value = available_smtp_password(account_id)
    if value is None:
        raise MailCredentialsMissing(
            f"no SMTP credential available for mail account {account_id!r}; "
            f"set {smtp_password_env_var(account_id)} in the environment"
        )
    return value


class EnvironmentMailCredentials:
    """The `MailCredentialSource` this host actually has: the process environment (ADR-0043 §15).

    Every method here reports a *fact about* a credential — whether one exists, and which variable
    names it — which is exactly what a settings page is allowed to know. Nothing hands out a value:
    the connection probes call `available_password` / `available_smtp_password` directly, inside
    this package, so a secret never travels through a core-layer signature on its way to a test.
    """

    def inbound_reference(self, account_id: str) -> str:
        """The environment variable that would hold this account's inbound secret."""
        return password_env_var(account_id)

    def outbound_reference(self, account_id: str) -> str:
        """The environment variable that would hold this account's outbound secret."""
        return smtp_password_env_var(account_id)

    def has_inbound(self, account_id: str) -> bool:
        """Whether an inbound credential is available right now."""
        return available_password(account_id) is not None

    def has_outbound(self, account_id: str) -> bool:
        """Whether an outbound credential is available right now."""
        return available_smtp_password(account_id) is not None


__all__ = [
    "PREFIX",
    "EnvironmentMailCredentials",
    "available_password",
    "available_smtp_password",
    "password_env_var",
    "require_password",
    "require_smtp_password",
    "smtp_password_env_var",
]
