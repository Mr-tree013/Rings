"""Knowledge index service: catalogued files → extracted text → per-root index (ADR-0012).

The service trusts the catalog for *what exists* and proves the filesystem again before
reading: the root must be online, a vault must still be the vault we catalogued, and the
extractors revalidate size, mtime and symlink safety. One unreadable file becomes one ERROR
document; it never aborts the root.

Nothing here reads files or SQL directly — extraction and indexing both happen behind ports
that offload blocking work to worker threads.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from assistant.application.event_failures import format_event_failure
from assistant.domain.catalog import CatalogEntry, CatalogRoot
from assistant.domain.errors import (
    KnowledgeIndexCorrupt,
    StorageRootIdentityMismatch,
    StorageRootOffline,
    UnknownCatalogEntry,
    UnknownStorageRoot,
)
from assistant.domain.knowledge import (
    KnowledgeDocumentState,
    KnowledgeIndexRunResult,
    KnowledgeIndexStatus,
)
from assistant.domain.storage import StorageKind
from assistant.ports.catalog_repository import CatalogRepository
from assistant.ports.clock import Clock
from assistant.ports.content_extractor import ContentExtractor, ContentExtractorRegistry
from assistant.ports.knowledge_index import KnowledgeIndex, KnowledgeIndexFactory
from assistant.ports.vault_manifest_store import VaultManifestStore


class KnowledgeIndexer:
    """Builds and maintains the derived full-text index of storage roots."""

    def __init__(
        self,
        catalog: CatalogRepository,
        extractors: ContentExtractorRegistry,
        indexes: KnowledgeIndexFactory,
        manifests: VaultManifestStore,
        clock: Clock,
    ) -> None:
        self._catalog = catalog
        self._extractors = extractors
        self._indexes = indexes
        self._manifests = manifests
        self._clock = clock

    async def index_root(self, root_id: str, *, force: bool = False) -> KnowledgeIndexRunResult:
        """Index every PRESENT entry of one root.

        Entries the catalog reports as MISSING have their derived text removed, so search can
        never return content for a file that is gone.

        Raises:
            UnknownStorageRoot: the catalog has no such root.
            StorageRootOffline: the root's physical location is not present.
            StorageRootIdentityMismatch: a vault mount no longer holds this vault.
        """
        started_at = self._clock.now()
        root = await self._require_online_root(root_id)
        index = await self._open_index(root)
        entries = await self._catalog.list_by_root(root_id, include_missing=True)
        counters = {
            "indexed": 0,
            "empty": 0,
            "unsupported": 0,
            "skipped_unchanged": 0,
            "errors": 0,
        }
        seen = 0
        for entry in entries:
            if not entry.is_present:
                await index.remove_document(entry.id)
                continue
            seen += 1
            counters[await self._index_one(index, root, entry, force=force)] += 1
        return KnowledgeIndexRunResult(
            root_id=root_id,
            seen=seen,
            indexed=counters["indexed"],
            empty=counters["empty"],
            unsupported=counters["unsupported"],
            skipped_unchanged=counters["skipped_unchanged"],
            errors=counters["errors"],
            started_at=started_at,
            finished_at=self._clock.now(),
        )

    async def index_entry(
        self, root_id: str, entry_id: UUID, *, force: bool = False
    ) -> KnowledgeDocumentState | None:
        """Index one catalogued entry.

        Returns the resulting state, or `None` when the entry is no longer present in the
        catalog (its derived text is removed in that case).
        """
        root = await self._require_online_root(root_id)
        index = await self._open_index(root)
        entries = await self._catalog.list_by_root(root_id, include_missing=True)
        entry = next((item for item in entries if item.id == entry_id), None)
        if entry is None:
            raise UnknownCatalogEntry(f"{entry_id} is not catalogued under {root_id!r}")
        if not entry.is_present:
            await index.remove_document(entry_id)
            return None
        await self._index_one(index, root, entry, force=force)
        return await index.get_document_state(entry_id)

    async def _index_one(
        self, index: KnowledgeIndex, root: CatalogRoot, entry: CatalogEntry, *, force: bool
    ) -> str:
        """Attempt one entry and return the counter key it belongs to."""
        extractor = self._extractors.extractor_for(entry)
        existing = await index.get_document_state(entry.id)
        if not force and existing is not None and _is_current(existing, entry, extractor):
            # Skipping is only safe when the file still matches the catalog: a modified file
            # that has not been rescanned must be re-read (and will fail loudly) rather than
            # silently keep serving old text.
            unchanged_on_disk = extractor is None or await extractor.matches_catalog_metadata(
                Path(root.last_known_path), entry
            )
            if unchanged_on_disk and existing.status is KnowledgeIndexStatus.UNSUPPORTED:
                return "unsupported"
            if unchanged_on_disk:
                return "skipped_unchanged"
        attempted_at = self._clock.now()
        template = KnowledgeDocumentState(
            entry_id=entry.id,
            logical_uri=entry.logical_uri,
            relative_path=entry.relative_path,
            size_bytes=entry.size_bytes,
            mtime_ns=entry.mtime_ns,
            status=KnowledgeIndexStatus.UNSUPPORTED,
            last_attempted_at=attempted_at,
        )
        if extractor is None:
            await index.mark_unsupported(state=template)
            return "unsupported"
        try:
            document = await extractor.extract(Path(root.last_known_path), entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            summary = format_event_failure(exc)
            await index.mark_error(
                state=replace(template, status=KnowledgeIndexStatus.ERROR, last_error=summary),
                error=summary,
            )
            return "errors"
        if document.chunks:
            await index.replace_document(
                state=replace(
                    template,
                    status=KnowledgeIndexStatus.INDEXED,
                    content_sha256=document.content_sha256,
                    extractor=document.extractor,
                    indexed_at=attempted_at,
                ),
                chunks=document.chunks,
            )
            return "indexed"
        await index.replace_document(
            state=replace(
                template,
                status=KnowledgeIndexStatus.EMPTY,
                content_sha256=document.content_sha256,
                extractor=document.extractor,
                indexed_at=attempted_at,
            ),
            chunks=(),
        )
        return "empty"

    async def _require_online_root(self, root_id: str) -> CatalogRoot:
        root = await self._catalog.get_root(root_id)
        if root is None:
            raise UnknownStorageRoot(f"no storage root {root_id!r} in the catalog")
        path = Path(root.last_known_path)
        if not path.is_dir():
            raise StorageRootOffline(
                f"storage root {root_id!r} is not present at {root.last_known_path}"
            )
        if root.root.kind is StorageKind.VAULT:
            manifest = await self._manifests.read(path)
            if manifest.vault_id != root.root.root_id:
                raise StorageRootIdentityMismatch(
                    f"{path} holds vault {manifest.vault_id!r}, "
                    f"not {root.root.root_id!r}"
                )
        return root

    async def _open_index(self, root: CatalogRoot) -> KnowledgeIndex:
        index = await self._indexes.open(root, create=True)
        if index is None:  # pragma: no cover - create=True never returns None
            raise KnowledgeIndexCorrupt(
                f"no knowledge index could be opened for {root.root.root_id!r}"
            )
        return index


def _is_current(
    existing: KnowledgeDocumentState,
    entry: CatalogEntry,
    extractor: ContentExtractor | None,
) -> bool:
    """Whether an existing index state still describes this file and extractor policy."""
    if existing.size_bytes != entry.size_bytes or existing.mtime_ns != entry.mtime_ns:
        return False
    if existing.status is KnowledgeIndexStatus.UNSUPPORTED:
        return extractor is None
    if existing.status not in {KnowledgeIndexStatus.INDEXED, KnowledgeIndexStatus.EMPTY}:
        return False
    if extractor is None:
        return False
    return existing.extractor == extractor.info


__all__ = ["KnowledgeIndexer"]
