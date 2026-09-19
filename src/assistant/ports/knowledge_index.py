"""KnowledgeIndex port: a per-root, rebuildable full-text index (ADR-0012).

The index is derived data in its own database file, never part of the runtime authority.
Everything that changes it is a whole-document replacement inside one transaction, so a
document's metadata, its chunks and its FTS rows can never disagree.

Raw SQL and raw FTS `MATCH` syntax are deliberately not exposed: the port takes plain user
text and returns ranked hits.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from assistant.domain.catalog import CatalogRoot
from assistant.domain.knowledge import (
    ExtractedChunk,
    KnowledgeDocumentState,
    KnowledgeSearchHit,
)


class KnowledgeIndex(Protocol):
    """One storage root's knowledge index."""

    async def get_document_state(self, entry_id: UUID) -> KnowledgeDocumentState | None:
        """Return the index state for one catalog entry, or `None`."""
        ...

    async def list_document_states(
        self, *, limit: int | None = None
    ) -> list[KnowledgeDocumentState]:
        """List every indexed document state, ordered by relative path."""
        ...

    async def replace_document(
        self, *, state: KnowledgeDocumentState, chunks: Sequence[ExtractedChunk]
    ) -> None:
        """Replace a document's state and chunks atomically.

        Always removes the previous chunks and their FTS rows first, so a failed or changed
        document can never keep serving stale text.
        """
        ...

    async def mark_error(
        self, *, state: KnowledgeDocumentState, error: str
    ) -> None:
        """Record a failed attempt and drop any previously indexed text for the entry."""
        ...

    async def mark_unsupported(self, *, state: KnowledgeDocumentState) -> None:
        """Record that no extractor handles this entry, dropping any previous text."""
        ...

    async def remove_document(self, entry_id: UUID) -> None:
        """Remove a document and its chunks entirely (for example when it went missing)."""
        ...

    async def search(self, query: str, *, limit: int) -> list[KnowledgeSearchHit]:
        """Search this index for plain user text, best match first."""
        ...


class KnowledgeIndexFactory(Protocol):
    """Opens (and, when asked, creates) the index database for a storage root."""

    async def open(self, root: CatalogRoot, *, create: bool = False) -> KnowledgeIndex | None:
        """Return the root's index, or `None` when `create` is false and none exists.

        Raises:
            KnowledgeIndexMismatch: the database is bound to another root or storage kind.
            KnowledgeIndexNeedsRebuild: the database uses an unsupported schema version.
            KnowledgeIndexCorrupt: the database cannot be read.
            Fts5Unavailable: this SQLite build has no FTS5 module.
            TrigramTokenizerUnavailable: FTS5 exists but the trigram tokenizer does not.
        """
        ...


__all__ = ["KnowledgeIndex", "KnowledgeIndexFactory"]

