"""ContentExtractor port: turn a catalogued file into searchable text (ADR-0012).

Extraction reads user files, so it is blocking I/O behind an async API: adapters perform the
whole read/hash/parse inside one `asyncio.to_thread`, never one thread hop per chunk. The
application never opens a user file itself, and never sees a third-party parser exception —
adapters translate those into project errors (`ContentExtractionError`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from assistant.domain.catalog import CatalogEntry
from assistant.domain.knowledge import ExtractedDocument, ExtractorInfo


class ContentExtractor(Protocol):
    """Reads one supported file kind and returns text with source spans."""

    @property
    def info(self) -> ExtractorInfo:
        """Stable name and version of this extractor.

        A version bump makes previously indexed documents due for reprocessing even when
        size and mtime are unchanged.
        """
        ...

    def supports(self, entry: CatalogEntry) -> bool:
        """Whether this extractor handles `entry`, purely from catalog metadata."""
        ...

    async def extract(self, root_path: Path, entry: CatalogEntry) -> ExtractedDocument:
        """Extract `entry` from `root_path`.

        Raises:
            UnsafeFilePath: the path is a symlink, a non-regular file, or escapes the root.
            FileChangedDuringExtraction: the file no longer matches the catalog metadata.
            ContentTooLarge: the file exceeds this extractor's size limit.
            ContentExtractionError: the format is supported but could not be read.
        """
        ...

    async def matches_catalog_metadata(self, root_path: Path, entry: CatalogEntry) -> bool:
        """Cheap, content-free check that the file still matches the catalog metadata.

        The indexer uses this before skipping a document as "unchanged": the catalog can be
        stale, so a stat-only revalidation is what keeps a modified-but-not-rescanned file
        from silently serving old text. Implementations must not read file contents.
        """
        ...


class ContentExtractorRegistry(Protocol):
    """Chooses an extractor for a catalog entry, or nothing when unsupported."""

    def extractor_for(self, entry: CatalogEntry) -> ContentExtractor | None:
        """Return the extractor that handles `entry`, or `None` if unsupported."""
        ...


__all__ = ["ContentExtractor", "ContentExtractorRegistry"]
