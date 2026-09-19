"""Knowledge search service: plain user text → full-text hits + metadata hits (ADR-0012).

Two signals, never blended into a fake single confidence:

```text
content hits   real text matches from per-root FTS5 indexes, each with a source span
metadata hits  file name / path matches from the host catalog, marked metadata-only
```

Ranks from different index databases are not comparable, so cross-root results are merged
with Reciprocal Rank Fusion over positions.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from assistant.domain.catalog import CatalogRoot
from assistant.domain.errors import InvalidVaultManifest, VaultNotInitialized
from assistant.domain.knowledge import (
    KnowledgeSearchHit,
    KnowledgeSearchResult,
    MergedKnowledgeHit,
)
from assistant.domain.storage import StorageKind
from assistant.ports.catalog_repository import CatalogRepository
from assistant.ports.knowledge_index import KnowledgeIndexFactory
from assistant.ports.vault_manifest_store import VaultManifestStore

RRF_K = 60
"""Reciprocal Rank Fusion constant, the usual small-corpus default."""

MAX_SEARCH_LIMIT = 100
CANDIDATE_MULTIPLIER = 2
"""Per-root candidates fetched before fusion, so the merged top-N has enough to choose from."""


def reciprocal_rank_fusion(
    hits_per_root: Sequence[Sequence[KnowledgeSearchHit]],
    *,
    limit: int,
    k: int = RRF_K,
) -> list[MergedKnowledgeHit]:
    """Fuse per-root rankings by `sum(1 / (k + position))`, ties broken deterministically."""
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    scores: dict[UUID, tuple[float, KnowledgeSearchHit]] = {}
    for hits in hits_per_root:
        for position, hit in enumerate(hits, start=1):
            previous = scores.get(hit.chunk_id)
            score = 1.0 / (k + position) + (previous[0] if previous is not None else 0.0)
            scores[hit.chunk_id] = (score, hit)
    ordered = sorted(
        scores.values(),
        key=lambda item: (-item[0], str(item[1].logical_uri), item[1].ordinal),
    )
    return [MergedKnowledgeHit(hit=hit, score=score) for score, hit in ordered[:limit]]


class KnowledgeSearchService:
    """Searches every reachable root's index plus the host catalog."""

    def __init__(
        self,
        catalog: CatalogRepository,
        indexes: KnowledgeIndexFactory,
        manifests: VaultManifestStore,
        *,
        rrf_k: int = RRF_K,
        candidate_multiplier: int = CANDIDATE_MULTIPLIER,
    ) -> None:
        self._catalog = catalog
        self._indexes = indexes
        self._manifests = manifests
        self._rrf_k = rrf_k
        self._candidate_multiplier = candidate_multiplier

    async def search(
        self, query: str, *, root_id: str | None = None, limit: int = 10
    ) -> KnowledgeSearchResult:
        """Search content and metadata, reporting which roots could not be searched.

        Raises:
            ValueError: the query is blank, or the limit is outside 1..100.
        """
        if not query.strip():
            raise ValueError("query must not be blank")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
        roots = await self._target_roots(root_id)
        rankings: list[list[KnowledgeSearchHit]] = []
        offline: list[str] = []
        mismatches: list[str] = []
        for root in roots:
            path = Path(root.last_known_path)
            if not path.is_dir():
                offline.append(root.root.root_id)
                continue
            if not await self._mount_holds_this_root(root, path, mismatches):
                continue
            index = await self._indexes.open(root, create=False)
            if index is None:
                continue
            rankings.append(
                await index.search(
                    query, limit=limit * self._candidate_multiplier
                )
            )
        metadata_hits = await self._catalog.search_metadata(
            query, limit=limit, root_id=root_id, include_missing=False
        )
        return KnowledgeSearchResult(
            query=query,
            content_hits=tuple(
                reciprocal_rank_fusion(rankings, limit=limit, k=self._rrf_k)
            ),
            metadata_hits=tuple(metadata_hits),
            offline_roots=tuple(offline),
            identity_mismatches=tuple(mismatches),
        )

    async def _target_roots(self, root_id: str | None) -> list[CatalogRoot]:
        if root_id is None:
            return await self._catalog.list_roots()
        root = await self._catalog.get_root(root_id)
        return [] if root is None else [root]

    async def _mount_holds_this_root(
        self, root: CatalogRoot, path: Path, mismatches: list[str]
    ) -> bool:
        """Verify a vault mount still holds this vault before opening its index."""
        if root.root.kind is not StorageKind.VAULT:
            return True
        try:
            manifest = await self._manifests.read(path)
        except (VaultNotInitialized, InvalidVaultManifest):
            mismatches.append(root.root.root_id)
            return False
        if manifest.vault_id != root.root.root_id:
            mismatches.append(root.root.root_id)
            return False
        return True


__all__ = [
    "CANDIDATE_MULTIPLIER",
    "MAX_SEARCH_LIMIT",
    "RRF_K",
    "KnowledgeSearchService",
    "reciprocal_rank_fusion",
]

