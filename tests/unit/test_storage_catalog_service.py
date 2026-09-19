"""Unit tests for the storage catalog service wiring (fakes, no filesystem or database)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import FileSnapshotEntry, FilesystemSnapshot, ScanError
from assistant.domain.errors import VaultNotInitialized
from assistant.domain.storage import StorageKind
from assistant.domain.vault import VaultManifest
from tests.support.fakes import FakeClock
from tests.support.storage_fakes import (
    FakeCatalogRepository,
    FakeFilesystemScanner,
    FakeVaultManifestStore,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
VAULT_PATH = Path("/mnt/e/archive")


class ScanIdFactory:
    """Deterministic scan ids: 1, 2, 3, ..."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


def _snapshot(*paths: str, complete: bool = True) -> FilesystemSnapshot:
    entries = tuple(
        FileSnapshotEntry(
            relative_path=path,
            name=path.rsplit("/", 1)[-1],
            size_bytes=10,
            mtime_ns=1_000,
            media_type=None,
        )
        for path in paths
    )
    errors = () if complete else (ScanError(relative_path=".", error_type="OSError", message="x"),)
    return FilesystemSnapshot(
        entries=entries,
        complete=complete,
        errors=errors,
        skipped_symlinks=(),
        started_at=NOW,
        finished_at=NOW,
    )


def _service(
    snapshot: FilesystemSnapshot,
    *,
    manifests: FakeVaultManifestStore | None = None,
) -> tuple[
    StorageCatalogService,
    FakeFilesystemScanner,
    FakeVaultManifestStore,
    FakeCatalogRepository,
]:
    scanner = FakeFilesystemScanner(snapshot=snapshot)
    manifest_store = manifests if manifests is not None else FakeVaultManifestStore()
    catalog = FakeCatalogRepository()
    service = StorageCatalogService(
        scanner, manifest_store, catalog, FakeClock(start=NOW), new_scan_id=ScanIdFactory()
    )
    return service, scanner, manifest_store, catalog


async def test_scan_vault_uses_the_manifest_identity() -> None:
    manifests = FakeVaultManifestStore(
        manifests={
            VAULT_PATH: VaultManifest(
                vault_id="archive-main", label="Personal Archive", created_at=NOW
            )
        }
    )
    service, scanner, _, catalog = _service(_snapshot("Courses/OS/report.pdf"), manifests=manifests)

    result = await service.scan_vault(VAULT_PATH)

    root, physical_path, snapshot, scan_id, scanned_at = catalog.applied[0]
    assert scanner.scanned_paths == [VAULT_PATH]
    assert root.root_id == "archive-main"
    assert root.kind is StorageKind.VAULT
    assert root.label == "Personal Archive"
    assert physical_path == str(VAULT_PATH)
    assert snapshot is not None
    assert scan_id == UUID(int=1)
    assert scanned_at == NOW
    assert result.root_id == "archive-main"
    assert result.seen == 1


async def test_scan_vault_never_initialises_a_vault_implicitly() -> None:
    service, scanner, manifests, catalog = _service(_snapshot("a.txt"))

    with pytest.raises(VaultNotInitialized):
        await service.scan_vault(VAULT_PATH)

    assert manifests.initialized == []
    assert manifests.read_paths == [VAULT_PATH]
    assert scanner.scanned_paths == []
    assert catalog.applied == []


async def test_scan_local_uses_the_caller_supplied_identity() -> None:
    service, _, _, catalog = _service(_snapshot("Lab1/report.pdf"))

    await service.scan_local(
        root_id="university", label="University folder", path=Path("~/Documents/Whatever")
    )

    root, physical_path, _, _, _ = catalog.applied[0]
    assert root.root_id == "university"
    assert root.kind is StorageKind.LOCAL
    assert root.label == "University folder"
    assert physical_path == str(Path("~/Documents/Whatever").expanduser())
    assert physical_path != "Whatever"


async def test_incomplete_snapshots_are_passed_through_unchanged() -> None:
    incomplete = _snapshot("a.txt", complete=False)
    service, _, _, catalog = _service(incomplete)

    result = await service.scan_local(root_id="university", label="U", path=Path("/tmp/u"))

    _, _, snapshot, _, _ = catalog.applied[0]
    assert snapshot is incomplete
    assert result.scan_complete is False
    assert result.marked_missing == 0
    assert result.errors == 1
