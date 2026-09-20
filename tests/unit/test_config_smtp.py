"""SMTP configuration: optional as a group, complete when present, TLS only (ADR-0024)."""

from __future__ import annotations

import pytest

from assistant.domain.config import (
    DEFAULT_SENT_MAILBOX,
    DEFAULT_SMTP_PORT,
    DEFAULT_SMTP_SECURITY,
    AssistantConfig,
    MailAccountConfig,
)
from assistant.domain.errors import InvalidAssistantConfig


def _account(**overrides: object) -> MailAccountConfig:
    values: dict[str, object] = {
        "id": "smail",
        "host": "imap.example.edu",
        "username": "student@example.edu",
        "mailbox": "INBOX",
    }
    values.update(overrides)
    return MailAccountConfig(**values)  # type: ignore[arg-type]


def test_an_account_without_smtp_is_receive_only() -> None:
    account = _account()

    assert account.smtp_configured is False
    assert account.smtp_host is None
    assert account.from_address is None


def test_a_complete_smtp_block_is_accepted_and_defaulted() -> None:
    account = _account(
        smtp_host="smtp.example.edu",
        smtp_username="student@example.edu",
        from_address="student@example.edu",
    )

    assert account.smtp_configured is True
    assert account.smtp_port == DEFAULT_SMTP_PORT
    assert account.smtp_security == DEFAULT_SMTP_SECURITY
    assert account.sent_mailbox == DEFAULT_SENT_MAILBOX


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_both_tls_modes_are_accepted(security: str) -> None:
    account = _account(
        smtp_host="smtp.example.edu",
        smtp_username="student@example.edu",
        from_address="student@example.edu",
        smtp_security=security,
    )

    assert account.smtp_security == security


@pytest.mark.parametrize("security", ["plain", "none", "STARTTLS", "", "  "])
def test_plaintext_or_unknown_security_is_refused(security: str) -> None:
    with pytest.raises(InvalidAssistantConfig):
        _account(
            smtp_host="smtp.example.edu",
            smtp_username="student@example.edu",
            from_address="student@example.edu",
            smtp_security=security,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_host": "smtp.example.edu"},
        {"smtp_username": "student@example.edu"},
        {"from_address": "student@example.edu"},
        {"sent_mailbox": "Sent"},
        {"smtp_host": "smtp.example.edu", "smtp_username": "student@example.edu"},
        {"smtp_host": "", "smtp_username": "s@example.edu", "from_address": "s@example.edu"},
    ],
)
def test_a_partial_smtp_block_is_refused(overrides: dict[str, object]) -> None:
    """Half a sender is a trap: the user would believe mail can leave when it cannot."""
    with pytest.raises(InvalidAssistantConfig):
        _account(**overrides)


def test_the_from_address_must_be_a_bare_mailbox() -> None:
    with pytest.raises(InvalidAssistantConfig):
        _account(
            smtp_host="smtp.example.edu",
            smtp_username="student@example.edu",
            from_address="Student <student@example.edu>",
        )
    with pytest.raises(InvalidAssistantConfig):
        _account(
            smtp_host="smtp.example.edu",
            smtp_username="student@example.edu",
            from_address="not-an-address",
        )


def test_the_smtp_port_must_be_a_real_port() -> None:
    for port in (0, 70000):
        with pytest.raises(InvalidAssistantConfig):
            _account(
                smtp_host="smtp.example.edu",
                smtp_username="student@example.edu",
                from_address="student@example.edu",
                smtp_port=port,
            )


def test_the_parser_rejects_a_credential_or_verification_key() -> None:
    from assistant.domain.config import AssistantConfig

    for key in ("password", "smtp_password", "verify_tls", "api_key"):
        with pytest.raises(InvalidAssistantConfig):
            AssistantConfig.from_mapping(
                {
                    "format_version": 1,
                    "mail": {
                        "accounts": [
                            {
                                "id": "smail",
                                "host": "imap.example.edu",
                                "username": "student@example.edu",
                                "mailbox": "INBOX",
                                "smtp_host": "smtp.example.edu",
                                key: "x",
                            }
                        ]
                    },
                }
            )


def test_the_parser_reads_a_complete_block() -> None:
    config = AssistantConfig.from_mapping(
        {
            "format_version": 1,
            "mail": {
                "accounts": [
                    {
                        "id": "smail",
                        "host": "imap.example.edu",
                        "username": "student@example.edu",
                        "mailbox": "INBOX",
                        "smtp_host": "smtp.example.edu",
                        "smtp_port": 465,
                        "smtp_security": "ssl",
                        "smtp_username": "student@example.edu",
                        "from_address": "student@example.edu",
                        "sent_mailbox": "Sent Items",
                    }
                ]
            },
        }
    )

    account = config.mail.accounts[0]
    assert account.smtp_configured is True
    assert account.smtp_port == 465
    assert account.smtp_security == "ssl"
    assert account.sent_mailbox == "Sent Items"


def test_receive_only_and_sending_accounts_can_coexist() -> None:
    config = AssistantConfig(
        mail=_mail_config(
            _account(id="smail", smtp_host="smtp.example.edu",
                     smtp_username="s@example.edu", from_address="s@example.edu"),
            _account(id="personal"),
        )
    )

    assert [account.smtp_configured for account in config.mail.accounts] == [True, False]


def _mail_config(*accounts: MailAccountConfig):
    from assistant.domain.config import MailConfig

    return MailConfig(accounts=accounts)
