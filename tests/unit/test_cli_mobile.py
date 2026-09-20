"""`pw mobile` — pairing, sessions and approval links from the command line (ADR-0026)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app
from assistant.cli_mobile import mobile_app
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations

runner = CliRunner()
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[mobile]",
        "enabled = true",
        'bind = "lan"',
        "port = 8765",
        "",
    )
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "180")
    return tmp_path


def _database() -> Database:
    """The runtime database the CLI itself uses, so the test can seed and inspect it."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    apply_migrations(database, clock=clock)
    return database


def _seed_action(database: Database) -> str:
    """One open case with one prepared action, seeded through the real services."""
    clock = bootstrap.system_clock()
    from assistant.application.case_service import CaseService
    from assistant.store.actions import SqliteActionRepository
    from assistant.store.cases import SqliteCaseRepository

    cases = SqliteCaseRepository(database)
    actions = SqliteActionRepository(database)
    service = CaseService(cases, actions, clock)
    case = asyncio.run(service.create_case("Register for the course"))
    action = asyncio.run(
        service.prepare_action(
            case.id, "mail.send", {"to": "ada@example.edu", "body": "Please register me."}
        )
    )
    assert case.is_open
    return str(action.id)


def test_status_is_local_and_reports_candidates(isolated: Path) -> None:
    result = runner.invoke(app, ["mobile", "status"])

    assert result.exit_code == 0, result.output
    assert "Enabled" in result.output and "yes" in result.output
    assert "lan" in result.output
    assert "8765" in result.output
    assert "Active sessions" in result.output
    assert "Candidate URLs" in result.output
    assert "does not check whether the port is reachable" in result.output


def test_status_without_the_control_plane(isolated: Path) -> None:
    directory = Path(isolated) / "config" / "growing-assistant"
    (directory / "config.toml").write_text("format_version = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["mobile", "status"])

    assert result.exit_code == 0, result.output
    assert "no" in result.output
    assert "loopback" in result.output


def test_pair_prints_the_code_once_and_never_in_a_url(isolated: Path) -> None:
    result = runner.invoke(app, ["mobile", "pair"])

    assert result.exit_code == 0, result.output
    assert "/pair" in result.output
    assert "Pairing code:" in result.output
    code = re.search(r"Pairing code:\n(\S+)", result.output)
    assert code is not None, result.output
    assert "shown once" in result.output
    assert "never put in a URL" in result.output
    assert "?token" not in result.output
    # The code is stored as a hash, not as itself.
    database = _database()
    with database.connect() as connection:
        dumped = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM mobile_pairing_tokens").fetchall()
            for value in tuple(row)
        )
    assert code.group(1) not in dumped


def test_sessions_lists_and_revoke_cuts_one_off(isolated: Path) -> None:
    from assistant.domain.mobile import MobileWebSession, hash_mobile_token

    database = _database()
    session = MobileWebSession(
        session_hash=hash_mobile_token("SESSION-TOKEN-0123456789ABCDEFGHIJ"),
        csrf_hash=hash_mobile_token("CSRF-TOKEN-0123456789ABCDEFGHIJK"),
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
        last_seen_at=NOW,
    )
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mobile_sessions (id, session_hash, csrf_hash, created_at, expires_at, "
            "last_seen_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                str(session.id),
                session.session_hash,
                session.csrf_hash,
                session.created_at.isoformat(timespec="microseconds"),
                session.expires_at.isoformat(timespec="microseconds"),
                session.last_seen_at.isoformat(timespec="microseconds"),
            ),
        )

    listed = runner.invoke(app, ["mobile", "sessions"])
    revoked = runner.invoke(app, ["mobile", "revoke", str(session.id)[:8]])
    afterwards = runner.invoke(app, ["mobile", "sessions"])

    assert listed.exit_code == 0, listed.output
    assert "active" in listed.output
    assert revoked.exit_code == 0, revoked.output
    assert "revoked" in revoked.output
    assert "active" not in afterwards.output


def test_revoking_an_unknown_session_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["mobile", "revoke", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_an_approval_link_carries_the_token_in_the_fragment(isolated: Path) -> None:
    database = _database()
    action_id = _seed_action(database)

    result = runner.invoke(app, ["mobile", "approval-link", action_id[:8]])

    assert result.exit_code == 0, result.output
    assert f"/approve/{action_id}#token=" in result.output
    assert "?token=" not in result.output
    assert "never reaches the server's logs" in result.output
    assert "does not execute anything" in result.output
    assert "pw action execute" in result.output


def test_the_cli_has_no_command_that_serves_or_executes() -> None:
    """The command surface is pinned: pairing, sessions, links — and nothing that runs anything."""
    assert {command.name for command in mobile_app.registered_commands} == {
        "status",
        "pair",
        "sessions",
        "revoke",
        "approval-link",
    }
    help_text = runner.invoke(app, ["mobile", "--help"]).output
    for forbidden in ("serve", "execute", "send"):
        assert f"│ {forbidden}" not in help_text


def test_doctor_is_unaffected_by_the_mobile_control_plane(isolated: Path) -> None:
    """A LAN control plane needs no browser runtime: doctor reports on eHall, not on this."""
    result = runner.invoke(app, ["doctor"])

    assert "mobile" not in result.output.lower()
