"""Unit tests for catalog value objects: invariants and the meaning of presence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.domain.catalog import (
    CatalogEntry,
    CatalogPresence,
    CatalogScanResult,
    FileSnapshotEntry,
    FilesystemSnapshot,
)
from assistant.domain.errors import InvalidCatalogEntry, InvalidStorageUri
from assistant.domain.storage import StorageKind

SEEN_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _entry(**overrides: object) -> CatalogEntry:
    values: dict[str, object] = {
        "id": uuid4(),
        "root_id": "archive-main",
        "storage_kind": StorageKind.VAULT,
        "relative_path": "Courses/OS/report.pdf",
        "name": "report.pdf",
        "suffix": ".pdf",
        "size_bytes": 1024,
        "mtime_ns": 1_700_000_000_000_000_000,
        "media_type": "application/pdf",
        "presence": CatalogPresence.PRESENT,
        "first_seen_at": SEEN_AT,
        "last_seen_at": SEEN_AT,
        "metadata_updated_at": SEEN_AT,
    }
    values.update(overrides)
    return CatalogEntry(**values)  # type: ignore[arg-type]


def test_presence_has_exactly_two_meaningful_states() -> None:
    assert {member.value for member in CatalogPresence} == {"present", "missing"}
    assert not hasattr(CatalogPresence, "OFFLINE")


def test_entry_exposes_a_stable_logical_uri() -> None:
    entry = _entry()

    assert str(entry.logical_uri) == "vault://archive-main/Courses/OS/report.pdf"
    assert entry.is_present
    assert not _entry(presence=CatalogPresence.MISSING).is_present


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_path", "../escape.txt"),
        ("name", "   "),
        ("size_bytes", -1),
        ("mtime_ns", -1),
        ("suffix", "pdf"),
        ("last_seen_at", datetime(2026, 9, 20, 9, 0)),
    ],
)
def test_entry_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises((InvalidCatalogEntry, InvalidStorageUri)):
        _entry(**{field: value})


def test_snapshot_entry_rejects_unsafe_paths() -> None:
    with pytest.raises(InvalidStorageUri):
        FileSnapshotEntry(
            relative_path="../outside.txt",
            name="outside.txt",
            size_bytes=1,
            mtime_ns=1,
            media_type=None,
        )


def test_snapshot_entry_derives_a_suffix() -> None:
    def entry(relative_path: str, name: str) -> FileSnapshotEntry:
        return FileSnapshotEntry(
            relative_path=relative_path,
            name=name,
            size_bytes=1,
            mtime_ns=1,
            media_type=None,
        )

    assert entry("a/report.pdf", "report.pdf").suffix == ".pdf"
    assert entry("a/README", "README").suffix == ""
    assert entry("a/.gitignore", ".gitignore").suffix == ""


def test_snapshot_rejects_naive_or_reversed_times() -> None:
    with pytest.raises(InvalidCatalogEntry, match="timezone-aware"):
        FilesystemSnapshot(
            entries=(),
            complete=True,
            errors=(),
            skipped_symlinks=(),
            started_at=datetime(2026, 9, 20, 9, 0),
            finished_at=SEEN_AT,
        )
    with pytest.raises(InvalidCatalogEntry, match="finish before"):
        FilesystemSnapshot(
            entries=(),
            complete=True,
            errors=(),
            skipped_symlinks=(),
            started_at=SEEN_AT,
            finished_at=SEEN_AT.replace(hour=8),
        )


def test_scan_result_carries_every_counter_the_cli_reports() -> None:
    result = CatalogScanResult(
        root_id="archive-main",
        scan_id=uuid4(),
        seen=10,
        created=1,
        updated=2,
        unchanged=6,
        restored=1,
        marked_missing=0,
        scan_complete=True,
        errors=0,
    )

    assert result.created + result.updated + result.unchanged + result.restored == result.seen
    assert result.scan_complete
