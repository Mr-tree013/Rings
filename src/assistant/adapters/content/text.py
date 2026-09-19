"""Plain-text extraction with a strict suffix whitelist (ADR-0012).

Only known text formats are read: "it looks like text" is not a reason to open a file. The
content is decoded as UTF-8 (with or without BOM) and never guessed at, so a
`UnicodeDecodeError` becomes an extraction error for that one file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from assistant.adapters.content.safe_file import (
    catalogued_file_matches_metadata,
    open_catalogued_file,
)
from assistant.domain.catalog import CatalogEntry
from assistant.domain.chunking import chunk_lines
from assistant.domain.errors import ContentExtractionError, ContentTooLarge
from assistant.domain.knowledge import ExtractedDocument, ExtractorInfo

TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".log",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".py",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".java",
        ".rs",
        ".go",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".htm",
        ".css",
        ".sql",
        ".sh",
        ".bash",
    }
)

MAX_TEXT_FILE_BYTES = 16 * 1024 * 1024
HASH_BLOCK_BYTES = 1024 * 1024


class TextContentExtractor:
    """Extracts UTF-8 text files."""

    def __init__(self, *, max_bytes: int = MAX_TEXT_FILE_BYTES) -> None:
        self._max_bytes = max_bytes

    @property
    def info(self) -> ExtractorInfo:
        return ExtractorInfo(name="text", version=1)

    def supports(self, entry: CatalogEntry) -> bool:
        return entry.suffix.lower() in TEXT_SUFFIXES

    async def extract(self, root_path: Path, entry: CatalogEntry) -> ExtractedDocument:
        return await asyncio.to_thread(self._extract_sync, Path(root_path), entry)

    async def matches_catalog_metadata(self, root_path: Path, entry: CatalogEntry) -> bool:
        return await asyncio.to_thread(
            catalogued_file_matches_metadata,
            Path(root_path),
            entry.relative_path,
            expected_size=entry.size_bytes,
            expected_mtime_ns=entry.mtime_ns,
        )

    def _extract_sync(self, root_path: Path, entry: CatalogEntry) -> ExtractedDocument:
        with open_catalogued_file(
            root_path,
            entry.relative_path,
            expected_size=entry.size_bytes,
            expected_mtime_ns=entry.mtime_ns,
        ) as handle:
            size_bytes = os.fstat(handle.fileno()).st_size
            if size_bytes > self._max_bytes:
                raise ContentTooLarge(
                    f"{entry.relative_path} is {size_bytes} bytes, over the "
                    f"{self._max_bytes}-byte text limit"
                )
            digest = hashlib.sha256()
            payload = bytearray()
            while block := handle.read(HASH_BLOCK_BYTES):
                digest.update(block)
                payload.extend(block)
        try:
            text = bytes(payload).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ContentExtractionError(
                f"{entry.relative_path} is not valid UTF-8 "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return ExtractedDocument(
            content_sha256=digest.hexdigest(),
            extractor=self.info,
            chunks=tuple(chunk_lines(normalized)),
        )


__all__ = ["HASH_BLOCK_BYTES", "MAX_TEXT_FILE_BYTES", "TEXT_SUFFIXES", "TextContentExtractor"]
