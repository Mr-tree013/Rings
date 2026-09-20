"""Mail credential lookup: environment only, derived name, never configured (ADR-0020).

An account password or app password is a secret, and secrets do not belong in a configuration
file that gets copied between machines and pasted into issues. The variable name is *derived*
from the account id, so a user cannot point the host at an arbitrary variable, and nothing here
ever logs, returns or stores the value.
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


__all__ = ["PREFIX", "available_password", "password_env_var", "require_password"]
