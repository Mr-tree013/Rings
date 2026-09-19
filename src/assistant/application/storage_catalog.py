"""Storage catalog service: physical path → stable identity → catalog (ADR-0011).

The service is the only place that decides what a directory *is*: a vault names itself
through `.pa/vault.toml`, a local folder is named by its caller. Nothing here ever
guesses an identity from a directory name, and nothing here creates a vault implicitly.

Everything blocking happens behind ports: the scanner offloads traversal to a worker
thread and the catalog repository offloads SQL, so this module never imports `os`,
`sqlite3`, the store or the adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from assistant.domain.catalog import CatalogScanResult
from assistant.domain.storage import StorageKind, StorageRoot
from assistant.ports.catalog_repository import CatalogRepository
from assistant.ports.clock import Clock
from assistant.ports.filesystem_scanner import FilesystemScanner
from assistant.ports.vault_manifest_store import VaultManifestStore


class StorageCatalogService:
    """Scans storage roots and applies the result to the metadata catalog."""

    def __init__(
        self,
        scanner: FilesystemScanner,
        manifests: VaultManifestStore,
        catalog: CatalogRepository,
        clock: Clock,
        *,
        new_scan_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._scanner = scanner
        self._manifests = manifests
        self._catalog = catalog
        self._clock = clock
        self._new_scan_id = new_scan_id

    async def scan_vault(self, path: Path) -> CatalogScanResult:
        """Scan an archive vault, taking its identity from `.pa/vault.toml`.

        Raises:
            VaultNotInitialized: the directory is not an initialised vault. A vault is
                never created implicitly, no matter how much a drive looks like one.
            InvalidVaultManifest: the manifest cannot be trusted.
            StorageRootConflict: this vault id is already known as a different kind.
        """
        manifest = await self._manifests.read(path)
        root = StorageRoot(
            root_id=manifest.vault_id, kind=StorageKind.VAULT, label=manifest.label
        )
        return await self._scan(root, path)

    async def scan_local(
        self, *, root_id: str, label: str, path: Path
    ) -> CatalogScanResult:
        """Scan a local folder under an explicitly supplied identity.

        `root_id` is never derived from the directory name: the caller names the root,
        and the physical path is free to move afterwards.
        """
        root = StorageRoot(root_id=root_id, kind=StorageKind.LOCAL, label=label)
        return await self._scan(root, path)

    async def _scan(self, root: StorageRoot, path: Path) -> CatalogScanResult:
        snapshot = await self._scanner.scan(path)
        return await self._catalog.apply_snapshot(
            root=root,
            physical_path=str(path.expanduser()),
            snapshot=snapshot,
            scan_id=self._new_scan_id(),
            scanned_at=self._clock.now(),
        )


__all__ = ["StorageCatalogService"]

