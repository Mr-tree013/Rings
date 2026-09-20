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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from assistant.domain.catalog import CatalogRoot
from assistant.domain.errors import InvalidVaultManifest, VaultNotInitialized
from assistant.domain.knowledge import (
    KnowledgeContextHit,
    KnowledgeContextSearchResult,
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

KEYWORD_MIN_CHARS = 3
"""Shortest token worth searching: the index's trigram tokenizer cannot match less."""

MAX_KEYWORD_TOKENS = 8
"""How many tokens the deterministic fallback may search, in question order."""

_KEYWORD_STOPWORDS = frozenset(
    (
        "about", "after", "again", "against", "all", "also", "and", "any", "are", "because",
        "been", "before", "being", "between", "both", "but", "can", "could", "did", "does",
        "doing", "done", "during", "each", "few", "for", "from", "further", "had", "has",
        "have", "having", "how", "into", "its", "itself", "just", "like", "made", "make",
        "many", "more", "most", "much", "must", "not", "now", "off", "once", "only", "other",
        "our", "out", "over", "own", "same", "should", "some", "such", "than", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "those", "through",
        "under", "until", "very", "was", "were", "what", "when", "where", "which", "while",
        "who", "whom", "why", "will", "with", "would", "you", "your", "yours",
    )
)
"""Function words a question carries but a document rarely repeats meaningfully."""


def derive_keyword_tokens(query: str) -> tuple[str, ...]:
    """Derive deterministic search tokens from plain text.

    Retrieval has to survive a natural-language question. The index matches phrases, so the
    whole sentence is tried first; when that finds nothing, these tokens are searched
    individually. The derivation is local, fixed and model-free: lowercase, split on anything
    that is not a letter or digit, drop stopwords and anything shorter than the index's trigram
    minimum, deduplicate, and keep the question's own order.
    """
    words = re.findall(r"[^\W_]+", query.lower())
    tokens: list[str] = []
    for word in words:
        if len(word) < KEYWORD_MIN_CHARS or word in _KEYWORD_STOPWORDS:
            continue
        if word not in tokens:
            tokens.append(word)
        if len(tokens) == MAX_KEYWORD_TOKENS:
            break
    return tuple(tokens)


@dataclass(frozen=True, slots=True)
class _CollectedRankings:
    """One pass over the target roots: their rankings, plus what could not be searched."""

    rankings: tuple[tuple[str, list[KnowledgeSearchHit]], ...]
    offline_roots: tuple[str, ...]
    identity_mismatches: tuple[str, ...]


def _validate_query(query: str, limit: int) -> None:
    if not query.strip():
        raise ValueError("query must not be blank")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")


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
        _validate_query(query, limit)
        collected = await self._collect((query,), root_id=root_id, limit=limit)
        metadata_hits = await self._catalog.search_metadata(
            query, limit=limit, root_id=root_id, include_missing=False
        )
        return KnowledgeSearchResult(
            query=query,
            content_hits=tuple(
                reciprocal_rank_fusion(
                    [hits for _, hits in collected.rankings], limit=limit, k=self._rrf_k
                )
            ),
            metadata_hits=tuple(metadata_hits),
            offline_roots=collected.offline_roots,
            identity_mismatches=collected.identity_mismatches,
        )

    async def search_context(
        self, query: str, *, root_id: str | None = None, limit: int = 8
    ) -> KnowledgeContextSearchResult:
        """Search content, returning each fused hit's full indexed text and its root.

        This is the read path a source-grounded answer uses: it needs the chunk the index
        actually holds, not a snippet, and it needs to say which root the chunk came from.
        Metadata-only matches are returned separately because they are not evidence.

        A natural-language question is tried verbatim first; if that phrase matches nothing, the
        same question is searched as its own keywords. Both steps are local and deterministic —
        no model writes, expands or rewrites a query.

        Raises:
            ValueError: the query is blank, or the limit is outside 1..100.
        """
        _validate_query(query, limit)
        cleaned = query.strip()
        collected = await self._collect((cleaned,), root_id=root_id, limit=limit)
        derived: tuple[str, ...] = (cleaned,)
        if not any(hits for _, hits in collected.rankings):
            tokens = derive_keyword_tokens(cleaned)
            if tokens:
                fallback = await self._collect(tokens, root_id=root_id, limit=limit)
                if any(hits for _, hits in fallback.rankings):
                    collected = fallback
                    derived = (cleaned, *tokens)
        root_by_chunk: dict[UUID, str] = {}
        for root_id_value, hits in collected.rankings:
            for hit in hits:
                root_by_chunk.setdefault(hit.chunk_id, root_id_value)
        merged = reciprocal_rank_fusion(
            [hits for _, hits in collected.rankings], limit=limit, k=self._rrf_k
        )
        content_hits = tuple(
            KnowledgeContextHit(
                root_id=root_by_chunk[item.hit.chunk_id],
                entry_id=item.hit.entry_id,
                chunk_id=item.hit.chunk_id,
                ordinal=item.hit.ordinal,
                logical_uri=item.hit.logical_uri,
                source_span=item.hit.source_span,
                content=item.hit.content,
                score=item.score,
            )
            for item in merged
        )
        metadata_hits = await self._catalog.search_metadata(
            query, limit=limit, root_id=root_id, include_missing=False
        )
        return KnowledgeContextSearchResult(
            query=query,
            derived_queries=derived,
            content_hits=content_hits,
            metadata_hits=tuple(metadata_hits),
            offline_roots=collected.offline_roots,
            identity_mismatches=collected.identity_mismatches,
        )

    async def _collect(
        self, queries: Sequence[str], *, root_id: str | None, limit: int
    ) -> _CollectedRankings:
        """Walk the target roots once, keeping each query's ranking and the problems found."""
        roots = await self._target_roots(root_id)
        rankings: list[tuple[str, list[KnowledgeSearchHit]]] = []
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
            for query in queries:
                hits = await index.search(query, limit=limit * self._candidate_multiplier)
                if hits:
                    rankings.append((root.root.root_id, hits))
        return _CollectedRankings(
            rankings=tuple(rankings),
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
