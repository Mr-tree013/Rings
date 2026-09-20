"""Tests for the `pw` CLI: `status` runs, `doctor` passes on Python 3.13."""

from __future__ import annotations

import asyncio
import shutil
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
    # Phase 6A added the case/action groups; approval lives inside `pw action`, and there is no
    # bare top-level `approve`, `send` or `execute` command.
    assert "cases" in result.output
    assert "actions" in result.output
    names = {
        line.split("\u2502", 2)[1].strip().split(" ")[0]
        for line in result.output.splitlines()
        if line.startswith("\u2502") and line.split("\u2502", 2)[1].strip()
    }
    assert "approve" not in names
    assert "execute" not in names
    assert "send" not in names


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
    return {
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }


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


def test_doctor_reports_search_capabilities() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "sqlite FTS5" in result.output
    assert "FTS5 trigram" in result.output
    assert "pypdf" in result.output


def test_reindex_without_any_root_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["reindex"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "no storage roots" in result.output


def test_reindex_and_search_end_to_end(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "notes.md", "the important deadline is Friday\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0

    reindex = runner.invoke(app, ["reindex", "--root", "archive-main"], env=_env(tmp_path))
    search = runner.invoke(app, ["search", "deadline"], env=_env(tmp_path))

    assert reindex.exit_code == 0, reindex.output
    assert "indexed" in reindex.output
    assert search.exit_code == 0, search.output
    assert "vault://archive-main/notes.md" in search.output
    assert "lines 1-" in search.output
    assert "deadline" in search.output


def test_reindex_does_not_scan_new_files(tmp_path: Path) -> None:
    """The two-step model: catalog first, then index."""
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "first.md", "first content\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0
    _write(vault / "second.md", "second content\n")

    result = runner.invoke(app, ["reindex", "--root", "archive-main"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "seen" in result.output
    assert runner.invoke(app, ["search", "second"], env=_env(tmp_path)).output.count(
        "second content"
    ) == 0


def test_search_treats_operators_as_plain_text(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "notes.md", "plain prose only\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0
    assert runner.invoke(app, ["reindex"], env=_env(tmp_path)).exit_code == 0

    result = runner.invoke(app, ["search", 'a:b OR NEAR(x)'], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "no matches" in result.output


def test_search_rejects_an_out_of_range_limit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["search", "deadline", "--limit", "0"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "limit" in result.output


def test_search_reports_metadata_hits_separately(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "budget-2026.md", "unrelated body\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0
    assert runner.invoke(app, ["reindex"], env=_env(tmp_path)).exit_code == 0

    result = runner.invoke(app, ["search", "budget"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "[metadata]" in result.output
    assert "content unavailable / not indexed" in result.output


def test_search_reports_offline_roots_and_keeps_metadata(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "deadline-notes.md", "the important deadline is Friday\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0
    assert runner.invoke(app, ["reindex"], env=_env(tmp_path)).exit_code == 0
    shutil.rmtree(vault)

    result = runner.invoke(app, ["search", "deadline"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Offline roots skipped for content search:" in result.output
    assert "archive-main" in result.output
    assert "[metadata]" in result.output


def test_reindex_reports_an_offline_root_with_a_nonzero_exit(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _initialise_vault(vault)
    _write(vault / "notes.md", "content\n")
    assert runner.invoke(app, ["vault", "scan", str(vault)], env=_env(tmp_path)).exit_code == 0
    shutil.rmtree(vault)

    result = runner.invoke(app, ["reindex"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "skipped" in result.output


def _write_config(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _local_root_config(tmp_path: Path, documents: Path, *, interval: int = 3600) -> Path:
    return _write_config(
        tmp_path,
        "\n".join(
            (
                "format_version = 1",
                "[indexing]",
                f"interval_seconds = {interval}",
                "run_on_startup = true",
                "[[storage.roots]]",
                'kind = "local"',
                'id = "university"',
                'label = "University"',
                f'path = "{documents}"',
                "enabled = true",
                "",
            )
        ),
    )


def test_roots_list_without_a_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["roots", "list"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "No storage roots configured." in result.output


def test_roots_list_shows_configured_roots(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n".join(
            (
                "format_version = 1",
                "[[storage.roots]]",
                'kind = "local"',
                'id = "university"',
                'label = "University"',
                'path = "/home/someone/uni"',
                "[[storage.roots]]",
                'kind = "vault"',
                'id = "archive-main"',
                'path = "/mnt/e/archive"',
                "enabled = false",
                "",
            )
        ),
    )

    result = runner.invoke(app, ["roots", "list"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "university" in result.output
    assert "local" in result.output
    assert "archive-main" in result.output
    assert "vault" in result.output
    assert "/mnt/e/archive" in result.output


def test_roots_list_rejects_an_invalid_config(tmp_path: Path) -> None:
    _write_config(tmp_path, "this is not = = toml")

    result = runner.invoke(app, ["roots", "list"], env=_env(tmp_path))

    assert result.exit_code == 2
    assert "invalid configuration" in result.output


def test_sync_without_roots_exits_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["sync"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "No configured roots." in result.output


def test_sync_local_root_end_to_end(tmp_path: Path) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "the important deadline is Friday\n")
    _local_root_config(tmp_path, documents)

    sync = runner.invoke(app, ["sync"], env=_env(tmp_path))
    search = runner.invoke(app, ["search", "deadline"], env=_env(tmp_path))

    assert sync.exit_code == 0, sync.output
    assert "status: synced" in sync.output
    assert "catalog: seen=1" in sync.output
    assert "knowledge: indexed=1" in sync.output
    assert "index.sqlite3" not in sync.output
    assert search.exit_code == 0, search.output
    assert "local://university/notes.md" in search.output


def test_sync_offline_vault_exits_zero(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "\n".join(
            (
                "format_version = 1",
                "[[storage.roots]]",
                'kind = "vault"',
                'id = "archive-main"',
                f'path = "{tmp_path / "usb" / "archive"}"',
                "",
            )
        ),
    )

    result = runner.invoke(app, ["sync"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "status: offline" in result.output


def test_sync_identity_mismatch_exits_one(tmp_path: Path) -> None:
    vault = tmp_path / "usb" / "archive"
    vault.mkdir(parents=True)
    asyncio.run(
        VaultManifestFile(SystemClock()).initialize(
            vault, vault_id="someone-else", label="Other"
        )
    )
    _write_config(
        tmp_path,
        "\n".join(
            (
                "format_version = 1",
                "[[storage.roots]]",
                'kind = "vault"',
                'id = "archive-main"',
                f'path = "{vault}"',
                "",
            )
        ),
    )

    result = runner.invoke(app, ["sync"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "status: identity_mismatch" in result.output


def test_sync_incomplete_scan_exits_one_and_skips_indexing(
    tmp_path: Path, monkeypatch
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "content\n")
    _local_root_config(tmp_path, documents)
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

    result = runner.invoke(app, ["sync"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "status: incomplete" in result.output
    assert "knowledge indexing skipped" in result.output


def test_sync_unknown_root_is_rejected(tmp_path: Path) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "content\n")
    _local_root_config(tmp_path, documents)

    result = runner.invoke(app, ["sync", "--root", "unknown"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "no configured storage root" in result.output


def test_doctor_reports_config_status(tmp_path: Path) -> None:
    documents = tmp_path / "documents" / "university"
    documents.mkdir(parents=True)
    _local_root_config(tmp_path, documents)

    healthy = runner.invoke(app, ["doctor"], env=_env(tmp_path))
    _write_config(tmp_path, "format_version = 99")
    broken = runner.invoke(app, ["doctor"], env=_env(tmp_path))

    assert healthy.exit_code == 0, healthy.output
    assert "config" in healthy.output
    assert "1 roots" in healthy.output
    assert broken.exit_code == 1
    assert "ERROR" in broken.output
