"""Periodic reconciliation of configured storage roots (ADR-0013).

```text
configured root → metadata scan → catalog update → knowledge index update
```

Periodic reconciliation is the correctness mechanism: every change is eventually noticed by
re-scanning, so no filesystem notification is required (and a future watcher may only make
detection faster). The loop is explicitly *not* business processing: catalog and knowledge
maintenance are materialised views, so no `InboundEvent` is created here.

Two policies matter more than the plumbing:

- an **incomplete** metadata scan never triggers knowledge reindexing, so a temporary
  permission failure cannot wipe searchable content;
- a single root's offline / identity / index problem never stops the other roots.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from assistant.application.event_failures import format_event_failure
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import CatalogScanResult
from assistant.domain.config import AssistantConfig, ConfiguredStorageRoot
from assistant.domain.errors import (
    ConfiguredRootNotFound,
    InvalidVaultManifest,
    KnowledgeIndexCorrupt,
    KnowledgeIndexMismatch,
    KnowledgeIndexNeedsRebuild,
    StorageRootConflict,
    StorageRootIdentityMismatch,
    StorageRootOffline,
    UnsafeStorageRoot,
    VaultNotInitialized,
)
from assistant.domain.knowledge import KnowledgeIndexRunResult
from assistant.domain.storage import StorageKind
from assistant.ports.clock import Clock
from assistant.ports.interval_waiter import IntervalWaiter
from assistant.ports.vault_manifest_store import VaultManifestStore

LOGGER = logging.getLogger("assistant.index_sync")


class RootSyncStatus(StrEnum):
    """Outcome of reconciling one configured root."""

    SYNCED = "synced"
    OFFLINE = "offline"
    INCOMPLETE = "incomplete"
    IDENTITY_MISMATCH = "identity_mismatch"
    INDEX_ERROR = "index_error"
    SCAN_ERROR = "scan_error"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class RootSyncResult:
    """What happened to one root, with enough detail to explain it."""

    root_id: str
    kind: StorageKind
    status: RootSyncStatus
    catalog_result: CatalogScanResult | None = None
    knowledge_result: KnowledgeIndexRunResult | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IndexSyncResult:
    """One full reconciliation pass over the configured roots."""

    started_at: datetime
    finished_at: datetime
    roots: tuple[RootSyncResult, ...]

    @property
    def configured(self) -> int:
        return len(self.roots)

    @property
    def synced(self) -> int:
        return sum(1 for root in self.roots if root.status is RootSyncStatus.SYNCED)

    @property
    def offline(self) -> int:
        return sum(1 for root in self.roots if root.status is RootSyncStatus.OFFLINE)

    @property
    def incomplete(self) -> int:
        return sum(1 for root in self.roots if root.status is RootSyncStatus.INCOMPLETE)

    @property
    def failed(self) -> int:
        return sum(
            1
            for root in self.roots
            if root.status
            in {
                RootSyncStatus.IDENTITY_MISMATCH,
                RootSyncStatus.INDEX_ERROR,
                RootSyncStatus.SCAN_ERROR,
            }
        )

    def summary(self) -> str:
        """One-line summary for logs."""
        return (
            f"index sync completed: configured={self.configured} synced={self.synced} "
            f"offline={self.offline} incomplete={self.incomplete} failed={self.failed}"
        )


class IndexSyncService:
    """Reconciles configured roots: catalog first, knowledge index second."""

    name = "index-sync"

    def __init__(
        self,
        config: AssistantConfig,
        catalog_service: StorageCatalogService,
        indexer: KnowledgeIndexer,
        manifests: VaultManifestStore,
        clock: Clock,
        waiter: IntervalWaiter,
        *,
        enable_logging: bool = True,
    ) -> None:
        self._config = config
        self._catalog_service = catalog_service
        self._indexer = indexer
        self._manifests = manifests
        self._clock = clock
        self._waiter = waiter
        self._enable_logging = enable_logging
        self._lock = asyncio.Lock()

    @property
    def config(self) -> AssistantConfig:
        """The configuration this service was built with (loaded once, by design)."""
        return self._config

    async def sync_once(
        self, root_id: str | None = None, *, force_index: bool = False
    ) -> IndexSyncResult:
        """Reconcile every enabled root, or exactly one explicitly named root.

        Raises:
            ConfiguredRootNotFound: `root_id` is not configured.
            StoreError / other infrastructure errors: host-database failures are not
                disguised as root failures; the daemon supervisor handles them.
        """
        async with self._lock:
            roots = self._select_roots(root_id)
            started_at = self._clock.now()
            results = [
                await self._sync_root(root, force_index=force_index) for root in roots
            ]
            result = IndexSyncResult(
                started_at=started_at, finished_at=self._clock.now(), roots=tuple(results)
            )
        if self._enable_logging:
            LOGGER.info("%s", result.summary())
            for root in result.roots:
                if root.status in {
                    RootSyncStatus.IDENTITY_MISMATCH,
                    RootSyncStatus.INDEX_ERROR,
                    RootSyncStatus.SCAN_ERROR,
                }:
                    LOGGER.warning(
                        "root %s (%s): %s: %s",
                        root.root_id,
                        root.kind.value,
                        root.status.value,
                        root.error,
                    )
                elif root.status is RootSyncStatus.INCOMPLETE:
                    LOGGER.warning(
                        "root %s scan incomplete; knowledge indexing skipped",
                        root.root_id,
                    )
        return result

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Reconcile on startup (when configured) and then once per interval."""
        indexing = self._config.indexing
        if indexing.run_on_startup:
            await self.sync_once()
        while not stop_event.is_set():
            await self._waiter.wait(indexing.interval_seconds, stop_event)
            if stop_event.is_set():
                return
            await self.sync_once()

    def _select_roots(self, root_id: str | None) -> list[ConfiguredStorageRoot]:
        if root_id is not None:
            root = self._config.find(root_id)
            if root is None:
                raise ConfiguredRootNotFound(f"no configured storage root {root_id!r}")
            return [root]
        return list(self._config.roots)

    async def _sync_root(
        self, root: ConfiguredStorageRoot, *, force_index: bool
    ) -> RootSyncResult:
        if not root.enabled:
            return RootSyncResult(
                root_id=root.root_id, kind=root.kind, status=RootSyncStatus.DISABLED
            )
        path = Path(root.path)
        if not path.exists():
            return RootSyncResult(
                root_id=root.root_id,
                kind=root.kind,
                status=RootSyncStatus.OFFLINE,
                error=f"{root.path} is not present",
            )
        try:
            catalog_result = await self._scan(root, path)
        except (VaultNotInitialized, InvalidVaultManifest) as exc:
            return self._failed(root, RootSyncStatus.IDENTITY_MISMATCH, exc)
        except StorageRootIdentityMismatch as exc:
            return self._failed(root, RootSyncStatus.IDENTITY_MISMATCH, exc)
        except (UnsafeStorageRoot, StorageRootConflict) as exc:
            return self._failed(root, RootSyncStatus.SCAN_ERROR, exc)
        if not catalog_result.scan_complete:
            return RootSyncResult(
                root_id=root.root_id,
                kind=root.kind,
                status=RootSyncStatus.INCOMPLETE,
                catalog_result=catalog_result,
                error="metadata scan was incomplete; knowledge indexing skipped",
            )
        try:
            knowledge_result = await self._indexer.index_root(
                root.root_id, force=force_index
            )
        except StorageRootOffline as exc:
            return self._failed(root, RootSyncStatus.OFFLINE, exc, catalog_result)
        except StorageRootIdentityMismatch as exc:
            return self._failed(
                root, RootSyncStatus.IDENTITY_MISMATCH, exc, catalog_result
            )
        except (
            KnowledgeIndexMismatch,
            KnowledgeIndexNeedsRebuild,
            KnowledgeIndexCorrupt,
        ) as exc:
            return self._failed(root, RootSyncStatus.INDEX_ERROR, exc, catalog_result)
        return RootSyncResult(
            root_id=root.root_id,
            kind=root.kind,
            status=RootSyncStatus.SYNCED,
            catalog_result=catalog_result,
            knowledge_result=knowledge_result,
        )

    async def _scan(
        self, root: ConfiguredStorageRoot, path: Path
    ) -> CatalogScanResult:
        if path.is_symlink() or not path.is_dir():
            raise UnsafeStorageRoot(
                f"{root.path} is not a plain directory; automatic scanning refuses "
                "root-level symlinks and non-directories"
            )
        if root.kind is StorageKind.VAULT:
            # Verify the vault's own identity *before* scanning: a mismatched mount must not
            # be catalogued, indexed, or even read as if it were the configured root.
            manifest = await self._manifests.read(path)
            if manifest.vault_id != root.root_id:
                raise StorageRootIdentityMismatch(
                    f"{root.path} holds vault {manifest.vault_id!r}, not {root.root_id!r}"
                )
            return await self._catalog_service.scan_vault(path)
        return await self._catalog_service.scan_local(
            root_id=root.root_id, label=root.label, path=path
        )

    def _failed(
        self,
        root: ConfiguredStorageRoot,
        status: RootSyncStatus,
        error: BaseException,
        catalog_result: CatalogScanResult | None = None,
    ) -> RootSyncResult:
        return RootSyncResult(
            root_id=root.root_id,
            kind=root.kind,
            status=status,
            catalog_result=catalog_result,
            error=format_event_failure(error),
        )

__all__ = ["IndexSyncResult", "IndexSyncService", "RootSyncResult", "RootSyncStatus"]
