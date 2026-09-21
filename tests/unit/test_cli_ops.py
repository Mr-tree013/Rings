"""`pw integrity` and `pw backup` from the command line (ADR-0031).

The commands an operator actually runs in a bad week: check the runtime, take a backup, verify it,
look inside it, restore it somewhere new. The tests care about the same things the operator does —
exit codes, staging-only restore, nothing overwritten and no credential in a snippet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant.cli import app

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    # A host that has run at least one command: its runtime database exists and is migrated.
    from assistant import bootstrap

    bootstrap.runtime_database(bootstrap.system_clock())
    return tmp_path


# ------------------------------------------------------------------------ integrity


def test_integrity_check_passes_on_a_fresh_runtime(isolated: Path) -> None:
    result = runner.invoke(app, ["integrity", "check"])

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "migrations" in result.output
    assert "mail raw 0/0 valid" in result.output


def test_integrity_check_reports_a_critical_finding(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered payload is reported, not repaired, and the command exits non-zero."""
    import asyncio

    from assistant import bootstrap
    from assistant.application.case_service import CaseService
    from assistant.store.actions import SqliteActionRepository
    from assistant.store.cases import SqliteCaseRepository

    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = CaseService(
        SqliteCaseRepository(database), SqliteActionRepository(database), clock
    )

    async def _prepare() -> None:
        case = await service.create_case("A case")
        action = await service.prepare_action(case.id, "mail.send", {"to": "a@b.edu"})
        with database.connect() as connection:
            connection.execute(
                "UPDATE action_requests SET payload_json = ? WHERE id = ?",
                ('{"to":"attacker@example.com"}', str(action.id)),
            )

    asyncio.run(_prepare())

    result = runner.invoke(app, ["integrity", "check"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "critical" in result.output
    assert "attacker@example.com" not in result.output


# --------------------------------------------------------------------------- backup


def test_the_backup_commands_round_trip(isolated: Path) -> None:
    created = runner.invoke(app, ["backup", "create", str(isolated / "backup.gab")])
    verified = runner.invoke(app, ["backup", "verify", str(isolated / "backup.gab")])
    inspected = runner.invoke(app, ["backup", "inspect", str(isolated / "backup.gab")])

    assert created.exit_code == 0, created.output
    assert "Backup written" in created.output
    assert "does not contain credentials" in created.output
    assert verified.exit_code == 0, verified.output
    assert "VALID" in verified.output
    assert inspected.exit_code == 0, inspected.output
    assert "Format version" in inspected.output
    assert "0019_contacts_and_outbound_mail.sql" in inspected.output


def test_a_backup_is_never_overwritten(isolated: Path) -> None:
    target = isolated / "backup.gab"
    runner.invoke(app, ["backup", "create", str(target)])
    before = target.read_bytes()

    again = runner.invoke(app, ["backup", "create", str(target)])

    assert again.exit_code == 1
    assert "already exists" in again.output
    assert target.read_bytes() == before


def test_verifying_a_damaged_archive_exits_one(isolated: Path) -> None:
    damaged = isolated / "damaged.gab"
    damaged.write_bytes(b"not a zip archive")

    result = runner.invoke(app, ["backup", "verify", str(damaged)])

    assert result.exit_code == 1
    assert "not a readable zip" in result.output
    assert "Traceback" not in result.output


def test_restore_writes_a_new_directory_and_reports_what_it_invalidated(
    isolated: Path,
) -> None:
    archive = isolated / "backup.gab"
    destination = isolated / "recovered"
    runner.invoke(app, ["backup", "create", str(archive)])

    restored = runner.invoke(
        app, ["backup", "restore", str(archive), "--to", str(destination)]
    )

    assert restored.exit_code == 0, restored.output
    assert "Restore completed successfully." in restored.output
    assert (destination / "assistant.db").is_file()
    assert "Credentials were not restored" in restored.output
    assert "unresolved" in restored.output


def test_restore_refuses_the_live_runtime_directory(isolated: Path) -> None:
    archive = isolated / "backup.gab"
    runner.invoke(app, ["backup", "create", str(archive)])
    live = isolated / "data" / "growing-assistant"

    result = runner.invoke(
        app, ["backup", "restore", str(archive), "--to", str(live)]
    )

    assert result.exit_code == 1
    assert "refused" in result.output
    assert (live / "assistant.db").is_file()  # the live runtime is untouched


def test_restore_refuses_a_non_empty_destination(isolated: Path) -> None:
    archive = isolated / "backup.gab"
    runner.invoke(app, ["backup", "create", str(archive)])
    destination = isolated / "recovered"
    destination.mkdir()
    (destination / "mine.txt").write_text("keep me", encoding="utf-8")

    result = runner.invoke(
        app, ["backup", "restore", str(archive), "--to", str(destination)]
    )

    assert result.exit_code == 1
    assert (destination / "mine.txt").read_text(encoding="utf-8") == "keep me"


def test_a_missing_archive_is_an_invocation_failure(isolated: Path) -> None:
    # 2, not 1: a missing argument is a usage problem, a corrupt archive is a recovery problem.
    missing = str(isolated / "nope.gab")
    commands = (
        ["backup", "verify", missing],
        ["backup", "inspect", missing],
        ["backup", "restore", missing, "--to", str(isolated / "recovered")],
    )

    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 2, command
        assert "not a file" in result.output
    assert not (isolated / "recovered").exists()


def test_the_backup_commands_are_offline_only(isolated: Path) -> None:
    """Nothing in this group opens a socket: the suite's guard would fail the test if it did."""
    created = runner.invoke(app, ["backup", "create", str(isolated / "backup.gab")])
    inspected = runner.invoke(app, ["backup", "inspect", str(isolated / "backup.gab")])

    assert created.exit_code == 0
    assert inspected.exit_code == 0
