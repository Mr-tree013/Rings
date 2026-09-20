"""A `.gab` file ends where it says it ends (ADR-0032 §17/§18, closing the Phase 9A gap).

Phase 9A recorded that appended bytes after the ZIP end-of-central-directory record were accepted,
because every ZIP reader searches backwards for its end record. For a format whose promise is "these
bytes are the state I backed up", "and also some junk" is not acceptable, so the reader now requires
the final EOCD record to sit at the very end of the file with a zero-length comment, and the central
directory it names to end exactly where that record begins.

`zipfile` still does the member reading; this is only a boundary check, which is why it can be
strict without reimplementing the format.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant.adapters.backup.archive import open_archive
from assistant.application.backup_service import BackupService
from assistant.cli import app
from assistant.domain.errors import InvalidBackupArchive
from tests.support.ops import RuntimeFixture

runner = CliRunner()


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real, valid backup of a small runtime, plus a temp XDG root for the CLI."""
    runtime_root = tmp_path / "data" / "growing-assistant"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    fixture = RuntimeFixture(runtime_root)
    service: BackupService = fixture.backup_service()
    return service.create(tmp_path / "backup.gab").path


def _copy_with(archive: Path, tmp_path: Path, *, suffix: bytes = b"", name: str) -> Path:
    target = tmp_path / name
    target.write_bytes(archive.read_bytes() + suffix)
    return target


def _commented_copy(archive: Path, tmp_path: Path) -> Path:
    """A valid archive plus a ZIP comment: the end record is no longer the last thing in it."""
    target = tmp_path / "commented.gab"
    target.write_bytes(archive.read_bytes())
    with zipfile.ZipFile(target, "a") as handle:
        handle.comment = b"annotated by some other tool"
    return target


def test_the_valid_archive_itself_verifies(archive: Path) -> None:
    """The control: strictness must not reject the archives this project writes."""
    contents = open_archive(archive)

    assert contents.manifest.format_version == 1
    assert "runtime.sqlite3" in contents.entry_names


def test_trailing_garbage_is_refused(archive: Path, tmp_path: Path) -> None:
    """`valid.gab + junk` is not the state that was backed up."""
    tampered = _copy_with(archive, tmp_path, suffix=b"TRAILING", name="trailing.gab")

    with pytest.raises(InvalidBackupArchive) as refused:
        open_archive(tampered)

    assert "after its end-of-central-directory record" in str(refused.value)


def test_an_appended_archive_is_refused(archive: Path, tmp_path: Path) -> None:
    """A second archive glued behind the first is refused rather than read as the second one."""
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as handle:
        handle.writestr("smuggled.txt", b"not part of the backup")

    tampered = _copy_with(
        archive, tmp_path, suffix=inner.read_bytes(), name="appended.gab"
    )

    with pytest.raises(InvalidBackupArchive):
        open_archive(tampered)


def test_a_zip_comment_is_refused(archive: Path, tmp_path: Path) -> None:
    """A comment sits behind the end record, so the file no longer ends where the archive says."""
    with pytest.raises(InvalidBackupArchive) as refused:
        open_archive(_commented_copy(archive, tmp_path))

    assert "comment" in str(refused.value)


def test_verify_and_restore_refuse_every_appended_variant(
    archive: Path, tmp_path: Path
) -> None:
    """§18/§51: the CLI says INVALID for all three, and no destination is ever created."""
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as handle:
        handle.writestr("smuggled.txt", b"payload")

    variants = (
        _copy_with(archive, tmp_path, suffix=b"TRAILING", name="a.gab"),
        _copy_with(archive, tmp_path, suffix=inner.read_bytes(), name="b.gab"),
        _commented_copy(archive, tmp_path),
    )
    destination = tmp_path / "recovery"

    for variant in variants:
        verified = runner.invoke(app, ["backup", "verify", str(variant)])
        restored = runner.invoke(
            app, ["backup", "restore", str(variant), "--to", str(destination)]
        )

        assert verified.exit_code == 1, variant.name
        # A structural refusal is an error with a reason (exit 1), like every other unreadable
        # archive; the point is that it is refused, explained, and never described as VALID.
        assert "VALID" not in verified.output, verified.output
        assert "Traceback" not in verified.output
        assert verified.output.strip(), variant.name
        assert restored.exit_code != 0, variant.name
        assert "Traceback" not in restored.output
        assert not destination.exists()
        assert list(tmp_path.glob(".recovery.restore-*")) == []


def test_the_restore_refusal_leaves_the_working_directory_untouched(
    archive: Path, tmp_path: Path
) -> None:
    """§18: refusing an archive creates no file outside the destination, staging included."""
    tampered = _copy_with(archive, tmp_path, suffix=b"TRAILING", name="trailing.gab")
    before = sorted(path.name for path in tmp_path.iterdir())

    result = runner.invoke(
        app,
        ["backup", "restore", str(tampered), "--to", str(tmp_path / "recovery")],
    )

    assert result.exit_code != 0
    assert sorted(path.name for path in tmp_path.iterdir()) == before
