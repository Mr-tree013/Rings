"""PDF text extraction with `pypdf`, behind explicit limits (ADR-0012).

No OCR, no password cracking, no rendering: pages that carry no text layer stay empty, and a
whole PDF with no text at all is reported as EMPTY by the indexer. pypdf exceptions never
leave this module — they are translated into `ContentExtractionError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import BinaryIO

import pypdf

from assistant.adapters.content.safe_file import (
    catalogued_file_matches_metadata,
    open_catalogued_file,
)
from assistant.adapters.content.text import HASH_BLOCK_BYTES
from assistant.domain.catalog import CatalogEntry
from assistant.domain.chunking import chunk_pages
from assistant.domain.errors import ContentExtractionError, ContentTooLarge
from assistant.domain.knowledge import ExtractedDocument, ExtractorInfo

PDF_SUFFIXES = frozenset({".pdf"})
MAX_PDF_FILE_BYTES = 64 * 1024 * 1024


class PdfContentExtractor:
    """Extracts the text layer of PDF files page by page."""

    def __init__(self, *, max_bytes: int = MAX_PDF_FILE_BYTES) -> None:
        self._max_bytes = max_bytes

    @property
    def info(self) -> ExtractorInfo:
        return ExtractorInfo(name="pdf-pypdf", version=1)

    def supports(self, entry: CatalogEntry) -> bool:
        return entry.suffix.lower() in PDF_SUFFIXES

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
                    f"{self._max_bytes}-byte PDF limit"
                )
            digest = hashlib.sha256()
            while block := handle.read(HASH_BLOCK_BYTES):
                digest.update(block)
            handle.seek(0)
            pages = self._read_pages(handle, entry)
        return ExtractedDocument(
            content_sha256=digest.hexdigest(),
            extractor=self.info,
            chunks=tuple(chunk_pages(pages)),
        )

    def _read_pages(self, handle: BinaryIO, entry: CatalogEntry) -> list[tuple[int, str]]:
        try:
            reader = pypdf.PdfReader(handle)
            if reader.is_encrypted:
                raise ContentExtractionError(
                    f"{entry.relative_path} is an encrypted PDF; no password is used"
                )
            pages: list[tuple[int, str]] = []
            for number, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    raise ContentExtractionError(
                        f"{entry.relative_path} page {number} could not be extracted: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if text.strip():
                    pages.append((number, text))
            return pages
        except ContentExtractionError:
            raise
        except Exception as exc:
            raise ContentExtractionError(
                f"pypdf could not read {entry.relative_path}: {type(exc).__name__}: {exc}"
            ) from exc


__all__ = ["MAX_PDF_FILE_BYTES", "PDF_SUFFIXES", "PdfContentExtractor"]
