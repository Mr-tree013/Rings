"""Tests for the `pw` CLI: `status` runs, `doctor` passes on Python 3.13."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile, manifest_path_for
from assistant.adapters.system_clock import SystemClock
from assistant.cli import app
from assistant.domain.catalog import FilesystemSnapshot, ScanError
from assistant.store.db import Database

runner = CliRunner()


def test_status_runs_and_reports_the_core_state() -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "durable event pipeline" in result.output
    assert "not implemented" in result.output


def test_help_lists_only_implemented_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "status" in result.output
    assert "doctor" in result.output
    assert "approve" not in result.output


def test_doctor_succeeds_on_python_313() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "3.13" in result.output
    assert "environment looks usable" in result.output


def test_doctor_reports_the_runtime_database_path() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "runtime db" in result.output
    assert "assistant.db" in result.output


def test_vault_help_exposes_only_safe_commands() -> None:
    result = runner.invoke(app, ["vault", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("init", "status", "scan"):
        assert name in result.output
    for name in ("move", "delete", "organize", "search"):
        assert name not in result.output


def test_vault_init_creates_a_manifest_without_touching_the_catalog(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    result = runner.invoke(
        app,
        ["vault", "init", str(vault), "--id", "archive-main", "--label", "Personal Archive"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert manifest_path_for(vault).is_file()
    assert not (tmp_path / "xdg" / "growing-assistant").exists()


def test_vault_init_refuses_a_missing_root(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "vault",
            "init",
            str(tmp_path / "not-mounted"),
            "--id",
            "archive-main",
            "--label",
            "Personal Archive",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "never created" in result.output


def test_vault_status_reports_the_manifest_without_a_database(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)

    result = runner.invoke(app, ["vault", "status", str(vault)], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "archive-main" in result.output
    assert "Personal Archive" in result.output
    assert "format version" in result.output
    assert not (tmp_path / "xdg" / "growing-assistant").exists()


def test_vault_status_rejects_an_uninitialised_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["vault", "status", str(_vault(tmp_path))], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "manifest" in result.output


def test_vault_scan_updates_the_host_catalog(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "Courses" / "report.pdf")

    result = runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "seen" in result.output
    database = Database.at(tmp_path / "xdg" / "growing-assistant" / "assistant.db")
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT relative_path FROM catalog_entries ORDER BY relative_path"
        ).fetchall()
        roots = connection.execute("SELECT root_id, kind FROM storage_roots").fetchall()
    assert [str(row[0]) for row in rows] == ["Courses/report.pdf"]
    assert [(str(row[0]), str(row[1])) for row in roots] == [("archive-main", "vault")]


def test_vault_scan_refuses_an_uninitialised_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write(vault / "a.txt")

    result = runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "manifest" in result.output
    assert not (vault / ".pa").exists()


def test_vault_scan_reports_an_incomplete_scan_with_a_nonzero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "a.txt")
    incomplete = FilesystemSnapshot(
        entries=(),
        complete=False,
        errors=(ScanError(relative_path=".", error_type="PermissionError", message="denied"),),
        skipped_symlinks=(),
        started_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
    )

    async def fake_scan(self: FilesystemScanner, path: Path) -> FilesystemSnapshot:
        return incomplete

    monkeypatch.setattr(FilesystemScanner, "scan", fake_scan)

    result = runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path))

    assert result.exit_code == 2
    assert "missing detection skipped" in result.output


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "usb"
    root.mkdir()
    return root


def _env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(tmp_path / "xdg")}


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _initialise_vault(vault: Path) -> None:
    asyncio.run(
        VaultManifestFile(SystemClock()).initialize(
            vault, vault_id="archive-main", label="Personal Archive"
        )
    )
