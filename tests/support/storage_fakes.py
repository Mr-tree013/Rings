"""Fakes for storage-catalog tests: a scanner, a manifest store and a catalog repository.

They record what the application asked for, so service-level tests can assert the wiring
without touching a real filesystem or database. Real-filesystem and real-SQLite behaviour
is covered by the integration tests instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from assistant.domain.catalog import (
    CatalogEntry,
    CatalogRoot,
    CatalogScanResult,
    FilesystemSnapshot,
)
from assistant.domain.errors import VaultNotInitialized
from assistant.domain.storage import StorageKind, StorageRoot, StorageUri
from assistant.domain.vault import VaultManifest


@dataclass
class FakeFilesystemScanner:
    """Returns a scripted snapshot and remembers which paths were scanned."""

    snapshot: FilesystemSnapshot
    scanned_paths: list[Path] = field(default_factory=list)

    async def scan(self, path: Path) -> FilesystemSnapshot:
        self.scanned_paths.append(path)
        return self.snapshot


@dataclass
class FakeVaultManifestStore:
    """Returns a scripted manifest by vault path, and records initialisation attempts."""

    manifests: dict[Path, VaultManifest] = field(default_factory=dict)
    initialized: list[tuple[Path, str, str]] = field(default_factory=list)
    read_paths: list[Path] = field(default_factory=list)

    async def read(self, root: Path) -> VaultManifest:
        self.read_paths.append(root)
        manifest = self.manifests.get(root)
        if manifest is None:
            raise VaultNotInitialized(f"{root} has no manifest")
        return manifest

    async def initialize(self, root: Path, *, vault_id: str, label: str) -> VaultManifest:
        self.initialized.append((root, vault_id, label))
        manifest = VaultManifest(
            vault_id=vault_id, label=label, created_at=datetime(2026, 9, 20, tzinfo=UTC)
        )
        self.manifests[root] = manifest
        return manifest


@dataclass
class FakeCatalogRepository:
    """Records `apply_snapshot` calls and answers reads from a scripted store."""

    entries: dict[tuple[str, str], CatalogEntry] = field(default_factory=dict)
    roots: dict[str, CatalogRoot] = field(default_factory=dict)
    applied: list[tuple[StorageRoot, str, FilesystemSnapshot, UUID, datetime]] = field(
        default_factory=list
    )

    async def apply_snapshot(
        self,
        *,
        root: StorageRoot,
        physical_path: str,
        snapshot: FilesystemSnapshot,
        scan_id: UUID,
        scanned_at: datetime,
    ) -> CatalogScanResult:
        self.applied.append((root, physical_path, snapshot, scan_id, scanned_at))
        return CatalogScanResult(
            root_id=root.root_id,
            scan_id=scan_id,
            seen=len(snapshot.entries),
            created=len(snapshot.entries),
            updated=0,
            unchanged=0,
            restored=0,
            marked_missing=0,
            scan_complete=snapshot.complete,
            errors=len(snapshot.errors),
        )

    async def get_root(self, root_id: str) -> CatalogRoot | None:
        return self.roots.get(root_id)

    async def get_by_uri(self, uri: StorageUri) -> CatalogEntry | None:
        return await self.get_by_location(
            storage_kind=uri.kind, root_id=uri.root_id, relative_path=uri.relative_path
        )

    async def get_by_location(
        self, *, storage_kind: StorageKind, root_id: str, relative_path: str
    ) -> CatalogEntry | None:
        entry = self.entries.get((root_id, relative_path))
        if entry is None or entry.storage_kind is not storage_kind:
            return None
        return entry

    async def list_by_root(
        self, root_id: str, *, include_missing: bool = False, limit: int | None = None
    ) -> list[CatalogEntry]:
        entries = [
            entry
            for (entry_root_id, _), entry in sorted(self.entries.items())
            if entry_root_id == root_id and (include_missing or entry.is_present)
        ]
        return entries if limit is None else entries[:limit]

__all__ = [
    "FakeCatalogRepository",
    "FakeFilesystemScanner",
    "FakeVaultManifestStore",
]
