"""CatalogRepository port: durable metadata catalog for storage roots (ADR-0011).

The catalog answers "what do I have, and where did it live?" — never "what is in it".
There is deliberately no content search, no move and no delete: this phase stores
filesystem metadata only.

`apply_snapshot` is the only writer. It applies one whole snapshot inside one transaction,
so a partial scan can never half-update the catalog.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from assistant.domain.catalog import (
    CatalogEntry,
    CatalogRoot,
    CatalogScanResult,
    FilesystemSnapshot,
)
from assistant.domain.storage import StorageKind, StorageRoot, StorageUri


class CatalogRepository(Protocol):
    """Durable catalog storage."""

    async def apply_snapshot(
        self,
        *,
        root: StorageRoot,
        physical_path: str,
        snapshot: FilesystemSnapshot,
        scan_id: UUID,
        scanned_at: datetime,
    ) -> CatalogScanResult:
        """Register/refresh `root` and apply one snapshot to its entries.

        Raises:
            StorageRootConflict: `root.root_id` already exists with a different kind.
        """
        ...

    async def get_root(self, root_id: str) -> CatalogRoot | None:
        """Return the root and its runtime metadata, or `None`."""
        ...

    async def get_by_uri(self, uri: StorageUri) -> CatalogEntry | None:
        """Return the catalog record for a logical URI, or `None`."""
        ...

    async def get_by_location(
        self, *, storage_kind: StorageKind, root_id: str, relative_path: str
    ) -> CatalogEntry | None:
        """Return the catalog record for a location, or `None`."""
        ...

    async def list_by_root(
        self, root_id: str, *, include_missing: bool = False, limit: int | None = None
    ) -> list[CatalogEntry]:
        """List a root's entries ordered by relative path.

        Missing entries are excluded unless `include_missing` is set.
        """
        ...


__all__ = ["CatalogRepository"]

