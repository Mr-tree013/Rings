"""The managed mail-accounts overlay: atomicity, privacy and lossless precedence (ADR-0043).

The properties under test are the ones that would hurt a user: a half-written configuration file, a
world-readable one, a secret that found its way in, or a settings page that dropped the accounts the
user had already written into `config.toml` by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from assistant.adapters.config.mail_overlay import (
    MAIL_ACCOUNTS_FILENAME,
    merge_accounts,
    overlay_path,
    read_overlay,
    write_overlay,
)
from assistant.adapters.config.toml_config import TomlConfigLoader
from assistant.domain.config import parse_mail_account
from assistant.domain.errors import InvalidAssistantConfig


def _account(identifier: str = "smail", **overrides: object):
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


def test_a_missing_overlay_is_not_an_error(tmp_path: Path) -> None:
    assert read_overlay(overlay_path(tmp_path / "config.toml")) == ()


def test_the_overlay_lives_beside_the_primary_configuration(tmp_path: Path) -> None:
    assert overlay_path(tmp_path / "config.toml") == tmp_path / MAIL_ACCOUNTS_FILENAME


def test_a_round_trip_preserves_every_managed_field(tmp_path: Path) -> None:
    path = overlay_path(tmp_path / "config.toml")
    account = _account(
        smtp_host="smtp.example.edu",
        smtp_port=587,
        smtp_security="starttls",
        smtp_username="student@example.edu",
        from_address="student@example.edu",
        sent_mailbox="Sent Items",
    )

    write_overlay(path, (account,))
    recovered = read_overlay(path)

    assert recovered == (account,)
    assert recovered[0].smtp_security == "starttls"
    assert recovered[0].sent_mailbox == "Sent Items"


def test_a_round_trip_survives_awkward_but_legal_values(tmp_path: Path) -> None:
    """A quote or a backslash in a label must not produce a file nobody can read back."""
    path = overlay_path(tmp_path / "config.toml")
    account = _account(mailbox='INBOX/"怪"\\name')

    write_overlay(path, (account,))

    assert read_overlay(path)[0].mailbox == account.mailbox


def test_the_overlay_is_private_and_is_written_atomically(tmp_path: Path) -> None:
    path = overlay_path(tmp_path / "config.toml")

    write_overlay(path, (_account(),))

    assert path.stat().st_mode & 0o077 == 0, "the overlay must not be group or other readable"
    # Atomicity, observed rather than assumed: no temporary file is left behind, and the file that
    # exists is complete TOML.
    assert [entry.name for entry in tmp_path.iterdir()] == [MAIL_ACCOUNTS_FILENAME]
    assert read_overlay(path)


def test_rewriting_replaces_the_whole_managed_list(tmp_path: Path) -> None:
    path = overlay_path(tmp_path / "config.toml")
    write_overlay(path, (_account("one"), _account("two")))

    write_overlay(path, (_account("two"),))

    assert [account.id for account in read_overlay(path)] == ["two"]


def test_a_credential_key_cannot_be_smuggled_into_the_overlay(tmp_path: Path) -> None:
    """The same strict parser reads this file, so there is no looser second reader to defeat."""
    path = overlay_path(tmp_path / "config.toml")
    path.write_text(
        "[[mail.accounts]]\n"
        'id = "smail"\n'
        'host = "imap.example.edu"\n'
        'username = "u"\n'
        'mailbox = "INBOX"\n'
        'password = "hunter2"\n',
        encoding="utf-8",
    )

    with pytest.raises(InvalidAssistantConfig):
        read_overlay(path)


def test_the_overlay_refuses_unmanaged_keys(tmp_path: Path) -> None:
    path = overlay_path(tmp_path / "config.toml")
    path.write_text("[mail]\npoll_interval_seconds = 5\n", encoding="utf-8")

    with pytest.raises(InvalidAssistantConfig):
        read_overlay(path)


def test_the_overlay_written_by_the_product_never_contains_a_secret(tmp_path: Path) -> None:
    path = overlay_path(tmp_path / "config.toml")
    write_overlay(path, (_account(),))

    text = path.read_text(encoding="utf-8")

    assert "password" not in text
    assert "secret" not in text
    assert "token" not in text
    # And it does say, in words a human will read, where the credential actually comes from.
    assert "GROWING_ASSISTANT_MAIL_" in text


# --------------------------------------------------------------------- merging


def test_the_overlay_wins_by_id_and_keeps_the_original_order() -> None:
    legacy = _account("legacy", host="imap.legacy.edu")
    managed_one = _account("smail", host="imap.one.edu")
    managed_two = _account("smail", host="imap.two.edu")

    merged = merge_accounts((legacy, managed_one), (managed_two,))

    assert [account.id for account in merged] == ["legacy", "smail"]
    assert merged[0] is legacy
    assert merged[1].host == "imap.two.edu"


def test_a_managed_account_that_is_not_in_the_primary_file_is_added() -> None:
    merged = merge_accounts((_account("legacy"),), (_account("smail"),))

    assert [account.id for account in merged] == ["legacy", "smail"]


# ------------------------------------------------------------------- the loader


CONFIG = """format_version = 1

[planning]
timezone = "Asia/Shanghai"

[[mail.accounts]]
id = "legacy"
host = "imap.legacy.edu"
port = 993
username = "student@example.edu"
mailbox = "INBOX"
enabled = true
"""


async def test_the_loader_merges_the_overlay_into_the_effective_configuration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    write_overlay(
        overlay_path(config_path),
        (_account("legacy", host="imap.edited.edu", enabled=False), _account("new")),
    )

    config = await TomlConfigLoader(config_path).load()

    assert [account.id for account in config.mail.accounts] == ["legacy", "new"]
    assert config.mail.accounts[0].host == "imap.edited.edu"
    assert config.mail.accounts[0].enabled is False


async def test_a_host_without_an_overlay_reads_exactly_what_it_always_did(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")

    config = await TomlConfigLoader(config_path).load()

    assert [account.id for account in config.mail.accounts] == ["legacy"]
    assert not overlay_path(config_path).exists()


async def test_the_primary_configuration_is_never_rewritten(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    before = config_path.read_bytes()
    stat_before = config_path.stat().st_mtime_ns

    write_overlay(overlay_path(config_path), (_account("smail"),))
    await TomlConfigLoader(config_path).load()

    assert config_path.read_bytes() == before
    assert config_path.stat().st_mtime_ns == stat_before
    # The overlay is exactly one extra file beside it, and nothing else appeared.
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "config.toml",
        MAIL_ACCOUNTS_FILENAME,
    ]


async def test_the_loader_can_be_told_to_ignore_the_overlay(tmp_path: Path) -> None:
    """A diagnostic that inspects the user's own file must be able to see it alone."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    write_overlay(overlay_path(config_path), (_account("smail"),))

    config = await TomlConfigLoader(config_path, managed_overlay=False).load()

    assert [account.id for account in config.mail.accounts] == ["legacy"]


async def test_the_overlay_follows_the_configuration_directory(tmp_path: Path) -> None:
    """XDG_CONFIG_HOME is honoured because the overlay path is derived, not hard-coded."""
    config_directory = tmp_path / "xdg" / "growing-assistant"
    config_directory.mkdir(parents=True)
    config_path = config_directory / "config.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    try:
        write_overlay(overlay_path(config_path), (_account("smail"),))
        config = await TomlConfigLoader(config_path).load()
        assert [account.id for account in config.mail.accounts] == ["legacy", "smail"]
        assert (config_directory / MAIL_ACCOUNTS_FILENAME).is_file()
    finally:
        os.environ.pop("XDG_CONFIG_HOME", None)
