"""`pw mail` commands: accounts, sync, status, messages and show (ADR-0020)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app
from tests.support.mail_fakes import FakeMailSource, authentication_failure

runner = CliRunner()

RAW = (
    b"From: Ada <ada@example.edu>\r\n"
    b"To: me@example.edu\r\n"
    b"Subject: SE lab deadline\r\n"
    b"Message-ID: <a@example.edu>\r\n"
    b"Date: Fri, 23 Oct 2026 23:59:00 +0800\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"The deadline is Friday.\r\n"
)

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[mail]",
        "poll_interval_seconds = 60",
        "",
        "[[mail.accounts]]",
        'id = "smail"',
        'host = "imap.example.edu"',
        "port = 993",
        'username = "student@example.edu"',
        'mailbox = "INBOX"',
        "enabled = true",
        "",
    )
)


def _env(tmp_path: Path, *, configured: bool = True, secret: bool = True) -> dict[str, str]:
    config_home = tmp_path / ("config" if configured else "config-without-mail")
    if configured:
        directory = config_home / "growing-assistant"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    env = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "COLUMNS": "200",
    }
    if secret:
        env["GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD"] = "app-password"
    return env


def _install_source(monkeypatch: pytest.MonkeyPatch, source: FakeMailSource) -> None:
    monkeypatch.setattr(bootstrap, "mail_source", lambda account, **kwargs: source)


def _populated() -> FakeMailSource:
    source = FakeMailSource(uidvalidity=10)
    source.add(100, RAW)
    return source


# --------------------------------------------------------------------- accounts


def test_accounts_are_listed_without_a_password(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mail", "accounts"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "smail" in result.output
    assert "imap.example.edu" in result.output
    assert "student@example.edu" in result.output
    assert "INBOX" in result.output
    assert "present" in result.output
    assert "app-password" not in result.output


def test_accounts_report_a_missing_credential(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mail", "accounts"], env=_env(tmp_path, secret=False))

    assert result.exit_code == 0, result.output
    assert "missing" in result.output


def test_without_mail_configuration_everything_is_empty(tmp_path: Path) -> None:
    for command in ("accounts", "status", "messages"):
        result = runner.invoke(app, ["mail", command], env=_env(tmp_path, configured=False))
        assert result.exit_code == 0, (command, result.output)
        assert "no mail accounts configured" in result.output or "no mail messages" in result.output


# ------------------------------------------------------------------------- sync


def test_sync_stores_mail_and_bridges_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_source(monkeypatch, _populated())
    env = _env(tmp_path)

    synced = runner.invoke(app, ["mail", "sync"], env=env)
    status = runner.invoke(app, ["mail", "status"], env=env)
    messages = runner.invoke(app, ["mail", "messages"], env=env)

    assert synced.exit_code == 0, synced.output
    assert "smail" in synced.output and "synced" in synced.output
    assert "UIDVALIDITY" in synced.output
    assert status.exit_code == 0, status.output
    assert "10" in status.output and "100" in status.output
    assert messages.exit_code == 0, messages.output
    assert "SE lab deadline" in messages.output
    assert "ada@example.edu" in messages.output
    assert "available" in messages.output  # the body column reports status, not content


def test_status_before_the_first_sync_says_never_synced(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mail", "status"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "never synced" in result.output


def test_sync_can_target_one_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _populated()
    _install_source(monkeypatch, source)

    result = runner.invoke(app, ["mail", "sync", "--account", "smail"], env=_env(tmp_path))
    unknown = runner.invoke(app, ["mail", "sync", "--account", "nope"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert source.requests  # the configured account was polled
    assert unknown.exit_code == 1
    assert "no configured mail account" in unknown.output


def test_sync_reports_a_missing_credential_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.domain.errors import MailCredentialsMissing

    def factory(account: object, **kwargs: object) -> FakeMailSource:
        raise MailCredentialsMissing(
            "no credential available for mail account 'smail'; set "
            "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD in the environment"
        )

    monkeypatch.setattr(bootstrap, "mail_source", factory)

    result = runner.invoke(app, ["mail", "sync"], env=_env(tmp_path, secret=False))

    assert result.exit_code == 1
    assert "credentials_missing" in result.output
    assert "Traceback" not in result.output


def test_sync_reports_an_authentication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_source(monkeypatch, FakeMailSource(failure=authentication_failure()))

    result = runner.invoke(app, ["mail", "sync"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "auth_error" in result.output
    assert "rejected the credential" in result.output


def test_sync_help_states_that_it_connects(tmp_path: Path) -> None:
    result = runner.invoke(app, ["mail", "sync", "--help"], env=_env(tmp_path))

    assert result.exit_code == 0
    assert "Connects to configured IMAP servers and synchronizes mail." in result.output


# -------------------------------------------------------------------- messages


def test_messages_support_a_limit_and_account_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_source(monkeypatch, _populated())
    env = _env(tmp_path)
    runner.invoke(app, ["mail", "sync"], env=env)

    limited = runner.invoke(app, ["mail", "messages", "--limit", "1"], env=env)
    scoped = runner.invoke(app, ["mail", "messages", "--account", "personal"], env=env)
    invalid = runner.invoke(app, ["mail", "messages", "--limit", "0"], env=env)

    assert limited.exit_code == 0, limited.output
    assert "SE lab deadline" in limited.output
    assert scoped.exit_code == 0 and "no mail messages" in scoped.output
    assert invalid.exit_code == 1


def test_show_prints_the_message_but_no_physical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_source(monkeypatch, _populated())
    env = _env(tmp_path)
    runner.invoke(app, ["mail", "sync"], env=env)
    listing = runner.invoke(app, ["mail", "messages"], env=env)
    message_id = re.search(r"\b([0-9a-f]{8})\b", listing.output)
    assert message_id is not None

    shown = runner.invoke(app, ["mail", "show", message_id.group(1)], env=env)

    assert shown.exit_code == 0, shown.output
    assert "<a@example.edu>" in shown.output
    assert "The deadline is Friday." in shown.output
    assert "INBOX" in shown.output
    assert "10" in shown.output  # the UIDVALIDITY of its location
    assert "attachments: none" in shown.output
    assert str(tmp_path) not in shown.output  # no physical path anywhere


def test_show_resolves_prefixes_and_rejects_unknown_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_source(monkeypatch, _populated())
    env = _env(tmp_path)
    runner.invoke(app, ["mail", "sync"], env=env)

    unknown = runner.invoke(app, ["mail", "show", "ffffffff"], env=env)

    assert unknown.exit_code == 1
    assert "does not exist" in unknown.output


def test_an_oversize_message_is_visible_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(100, RAW + b"x" * (1024 * 1024 + 10))
    _install_source(monkeypatch, source)
    env = _env(tmp_path)
    # The configured limit is the smallest the product allows, so this message is oversize.
    directory = tmp_path / "config" / "growing-assistant"
    (directory / "config.toml").write_text(
        CONFIG.replace("poll_interval_seconds = 60", "max_message_bytes = 1048576"),
        encoding="utf-8",
    )
    runner.invoke(app, ["mail", "sync"], env=env)
    listing = runner.invoke(app, ["mail", "messages"], env=env)
    message_id = re.search(r"\b([0-9a-f]{8})\b", listing.output)
    assert message_id is not None

    shown = runner.invoke(app, ["mail", "show", message_id.group(1)], env=env)

    assert "oversize" in listing.output
    assert shown.exit_code == 0, shown.output
    assert "body not stored" in shown.output
