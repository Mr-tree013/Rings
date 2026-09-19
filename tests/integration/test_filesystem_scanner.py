"""Integration tests for the metadata scanner on a real filesystem.

The scanner is where the vault's safety rules are enforced, so these tests build real
directories, symlinks and a sparse multi-gigabyte file, and check what is (and is not)
read.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.filesystem.scanner import FilesystemScanner
from tests.support.fakes import FakeClock

START = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
SPARSE_FILE_SIZE = 2 * 1024**3


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def scanner(clock: FakeClock) -> FilesystemScanner:
    return FilesystemScanner(clock)


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def test_scans_files_with_metadata(scanner: FilesystemScanner, tmp_path: Path) -> None:
    _write(tmp_path / "Courses" / "OS" / "notes.txt")
    _write(tmp_path / "Courses" / "OS" / "report.pdf")
    _write(tmp_path / "README.md")

    snapshot = await scanner.scan(tmp_path)

    assert snapshot.complete
    assert snapshot.errors == ()
    assert snapshot.skipped_symlinks == ()
    assert [entry.relative_path for entry in snapshot.entries] == [
        "Courses/OS/notes.txt",
        "Courses/OS/report.pdf",
        "README.md",
    ]
    media_types = {entry.relative_path: entry.media_type for entry in snapshot.entries}
    assert media_types["Courses/OS/report.pdf"] == "application/pdf"
    assert media_types["Courses/OS/notes.txt"] == "text/plain"
    assert media_types["README.md"] == "text/markdown"
    assert all(entry.size_bytes > 0 for entry in snapshot.entries)
    assert all(entry.mtime_ns > 0 for entry in snapshot.entries)
    assert snapshot.started_at == START
    assert snapshot.finished_at == START


async def test_unknown_media_types_are_none(scanner: FilesystemScanner, tmp_path: Path) -> None:
    _write(tmp_path / "data.unknownextension")

    snapshot = await scanner.scan(tmp_path)

    assert snapshot.entries[0].media_type is None


async def test_metadata_scan_never_reads_or_hashes_file_contents(
    scanner: FilesystemScanner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sparse = tmp_path / "big.bin"
    with sparse.open("wb") as handle:
        handle.truncate(SPARSE_FILE_SIZE)

    def _forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a metadata scan must not hash file contents")

    monkeypatch.setattr(hashlib, "sha256", _forbidden)
    monkeypatch.setattr(hashlib, "sha1", _forbidden)
    monkeypatch.setattr(hashlib, "md5", _forbidden)

    snapshot = await scanner.scan(tmp_path)

    assert snapshot.complete
    assert [entry.size_bytes for entry in snapshot.entries] == [SPARSE_FILE_SIZE]


async def test_skips_file_and_directory_symlinks(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write(outside / "external.txt")
    root = tmp_path / "root"
    _write(root / "real.txt")
    (root / "external").symlink_to(outside, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "file-link.txt").symlink_to(root / "real.txt")

    snapshot = await scanner.scan(root)

    assert snapshot.complete
    assert [entry.relative_path for entry in snapshot.entries] == ["real.txt"]
    assert snapshot.skipped_symlinks == (
        "external",
        "file-link.txt",
        "loop",
    )


async def test_excludes_the_internal_pa_directory(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    _write(tmp_path / ".pa" / "vault.toml", 'format_version = 1\n')
    _write(tmp_path / ".pa" / "secret-internal.txt")
    _write(tmp_path / "Courses" / "report.pdf")

    snapshot = await scanner.scan(tmp_path)

    assert [entry.relative_path for entry in snapshot.entries] == ["Courses/report.pdf"]
    assert snapshot.complete


async def test_a_missing_root_makes_the_snapshot_incomplete(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    snapshot = await scanner.scan(tmp_path / "not-mounted")

    assert not snapshot.complete
    assert snapshot.entries == ()
    assert [error.relative_path for error in snapshot.errors] == ["."]
    assert snapshot.errors[0].error_type == "FileNotFoundError"


async def test_unreadable_directory_makes_the_snapshot_incomplete(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    locked = tmp_path / "locked"
    _write(locked / "hidden.txt")
    _write(tmp_path / "visible.txt")
    locked.chmod(0o000)
    try:
        snapshot = await scanner.scan(tmp_path)
    finally:
        locked.chmod(0o700)

    assert not snapshot.complete
    assert [entry.relative_path for entry in snapshot.entries] == ["visible.txt"]
    assert [error.relative_path for error in snapshot.errors] == ["locked"]
    assert snapshot.errors[0].error_type == "PermissionError"


async def test_error_messages_are_bounded(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    snapshot = await scanner.scan(tmp_path / ("x" * 300))

    assert not snapshot.complete
    assert len(snapshot.errors[0].message) <= 500


async def test_empty_directory_is_a_complete_empty_snapshot(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    snapshot = await scanner.scan(tmp_path)

    assert snapshot.complete
    assert snapshot.entries == ()


async def test_non_regular_files_are_ignored(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    os.mkfifo(tmp_path / "pipe")
    _write(tmp_path / "real.txt")

    snapshot = await scanner.scan(tmp_path)

    assert [entry.relative_path for entry in snapshot.entries] == ["real.txt"]
    assert snapshot.complete


async def test_repeated_scans_are_deterministic(
    scanner: FilesystemScanner, tmp_path: Path
) -> None:
    _write(tmp_path / "b" / "two.txt")
    _write(tmp_path / "a" / "one.txt")

    first = await scanner.scan(tmp_path)
    second = await scanner.scan(tmp_path)

    assert [entry.relative_path for entry in first.entries] == [
        entry.relative_path for entry in second.entries
    ]
    assert [entry.size_bytes for entry in first.entries] == [
        entry.size_bytes for entry in second.entries
    ]

