"""Safe defaults, and a database from the future (ADR-0032 §12/§19/§23/§24/§25).

Two release-level promises live here. The sample configuration must be *safe by default*: copying it
to `~/.config/growing-assistant/config.toml` and starting the daemon must not contact a mail server,
a university system, a page watcher, a phone or an editor. And a runtime written by a newer binary
must fail closed everywhere it can be mutated — bootstrap, the daemon, a mutating CLI command —
while the read-only audit reports the incompatibility instead of touching the file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant.application.backup_service import MIGRATION_DIRECTORY
from assistant.bootstrap import config_loader
from assistant.cli import app
from assistant.domain.config import AssistantConfig
from assistant.store.db import Database
from assistant.store.errors import DatabaseMigrationIncompatible
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.ops import NOW

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPOSITORY_ROOT / "docs" / "examples" / "config.toml"
runner = CliRunner()
STAMP = "2026-09-26T09:00:00.000000+00:00"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


# ----------------------------------------------------------------------- example configuration


def test_the_sample_configuration_parses() -> None:
    """§24: the example is real configuration, not a comment block that looks like one."""
    config = _load_example()

    assert isinstance(config, AssistantConfig)
    assert config.mail.accounts == ()
    assert config.watchers.web == ()


def test_the_sample_configuration_enables_nothing_that_leaves_the_machine() -> None:
    """§25: no account, no target, no server, no editor — the defaults are the safe ones."""
    config = _load_example()

    assert config.mail.accounts == ()
    assert config.mail.enabled_accounts == ()
    assert not any(account.smtp_configured for account in config.mail.accounts)
    assert config.watchers.web == ()
    assert config.watchers.enabled_targets == ()
    assert config.ehall.enabled is False
    assert config.mobile.enabled is False
    assert config.mcp.enabled is False
    assert config.mcp.write_scope == "none"
    assert config.mcp.expose_knowledge is False


def test_the_sample_configuration_composes_no_executor(tmp_path: Path) -> None:
    """The composition root over the example config registers no external capability at all."""
    from assistant.bootstrap import registered_action_executors

    assert registered_action_executors(_load_example()) == {}


def test_the_sample_configuration_carries_no_secret_or_real_path() -> None:
    """§24: nothing that looks like a credential, a key or somebody's real document root."""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    # Only the *active* configuration counts: the comments deliberately explain that this file must
    # never hold a key, and explaining it is not the same as containing one.
    active = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).lower()

    # `max_output_tokens` is a legitimate key, so the check is about credential-shaped assignments.
    for forbidden in ("api_key", "password", "secret", "access_token", "sk-", "-----begin"):
        assert forbidden not in active, forbidden
    # Only documented fictional paths appear, and all of them are comments or placeholders.
    assert "/home/user/Documents" in text
    assert "/home/mrtree" not in text


def _load_example() -> AssistantConfig:
    import asyncio

    return asyncio.run(config_loader(EXAMPLE_CONFIG).load())


# ------------------------------------------------------------------------- future database


def _future_runtime(tmp_path: Path, clock: FakeClock) -> Path:
    """A fully migrated runtime that also claims a migration this build has never heard of."""
    runtime = tmp_path / "data" / "growing-assistant"
    database = Database.at(runtime / "assistant.db")
    apply_migrations(database, clock=clock, directory=MIGRATION_DIRECTORY)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            ("9999", "9999_future_feature.sql", STAMP),
        )
    return runtime


@pytest.fixture
def future_host(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = _future_runtime(tmp_path, clock)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    return runtime


def test_bootstrap_refuses_a_newer_database(future_host: Path, clock: FakeClock) -> None:
    """§19: opening the runtime must fail closed, not migrate "around" the unknown version."""
    from assistant import bootstrap

    with pytest.raises(DatabaseMigrationIncompatible):
        bootstrap.runtime_database(clock)


def test_a_mutating_command_refuses_a_newer_database(future_host: Path) -> None:
    """§23: a `pw` command that would write must refuse, and must not write anything."""
    result = runner.invoke(app, ["task", "add", "This must not be stored"])

    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output
    with Database.at(future_host / "assistant.db").connect() as connection:
        tasks = connection.execute("SELECT count(*) AS total FROM tasks").fetchone()["total"]
        still_future = connection.execute(
            "SELECT count(*) AS total FROM schema_migrations WHERE version = '9999'"
        ).fetchone()["total"]
    assert tasks == 0
    assert still_future == 1  # the audit did not quietly undo the record it refused


def test_the_daemon_refuses_a_newer_database(future_host: Path, tmp_path: Path) -> None:
    """§23: the daemon refuses at startup, before any service is supervised."""
    script = Path(sys.executable).parent / "assistantd"
    command = [str(script)] if script.is_file() else [
        sys.executable,
        "-c",
        "from assistant.daemon import main; main()",
    ]
    environment = {
        **os.environ,
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }

    completed = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=120
    )

    assert completed.returncode != 0
    assert "Traceback" not in completed.stderr
    assert "newer" in completed.stderr or "migration" in completed.stderr


def test_the_read_only_audit_reports_an_incompatible_database(future_host: Path) -> None:
    """§23: integrity says INCOMPATIBLE and FAIL, leaving the database exactly as it found it."""
    before = (future_host / "assistant.db").read_bytes()

    result = runner.invoke(app, ["integrity", "check"])

    assert result.exit_code == 1, result.output
    assert "INCOMPATIBLE" in result.output
    assert "FAIL" in result.output
    assert (future_host / "assistant.db").read_bytes() == before
