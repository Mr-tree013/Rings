"""Integration tests for content extraction from real files (ADR-0012).

Real files, real symlinks, a real FIFO and real PDFs: safe access, hashing, decoding and
page/line spans are all behaviours that only a real filesystem can demonstrate.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.adapters.content.pdf import PdfContentExtractor
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.content.safe_file import open_catalogued_file
from assistant.adapters.content.text import TextContentExtractor
from assistant.domain.catalog import CatalogEntry, CatalogPresence
from assistant.domain.errors import (
    ContentExtractionError,
    ContentTooLarge,
    FileChangedDuringExtraction,
    InvalidStorageUri,
    UnsafeFilePath,
)
from assistant.domain.knowledge import SourceSpanKind
from assistant.domain.storage import StorageKind
from tests.support.pdf_fixtures import build_encrypted_pdf, build_text_pdf, malformed_pdf

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _entry_for(root: Path, relative_path: str) -> CatalogEntry:
    path = root / relative_path
    info = path.stat()
    return CatalogEntry(
        id=uuid4(),
        root_id="university",
        storage_kind=StorageKind.LOCAL,
        relative_path=relative_path,
        name=path.name,
        suffix=path.suffix,
        size_bytes=info.st_size,
        mtime_ns=info.st_mtime_ns,
        media_type=None,
        presence=CatalogPresence.PRESENT,
        first_seen_at=NOW,
        last_seen_at=NOW,
        metadata_updated_at=NOW,
    )


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def test_extracts_utf8_text_with_hashes_and_line_spans(tmp_path: Path) -> None:
    body = "第一行 中文\nEnglish line\nemoji 🚀 line\n"
    _write(tmp_path / "notes.md", body.encode("utf-8"))
    entry = _entry_for(tmp_path, "notes.md")
    extractor = TextContentExtractor()

    document = await extractor.extract(tmp_path, entry)

    assert document.content_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert document.extractor.name == "text"
    assert document.extractor.version == 1
    joined = "".join(chunk.content for chunk in document.chunks)
    assert "第一行 中文" in joined
    assert "English line" in joined
    assert "🚀" in joined
    assert document.chunks[0].source_span.kind is SourceSpanKind.LINE
    assert document.chunks[0].source_span.line_start == 1


async def test_reads_utf8_with_bom(tmp_path: Path) -> None:
    _write(tmp_path / "bom.txt", "hello bom\n".encode("utf-8-sig"))
    entry = _entry_for(tmp_path, "bom.txt")

    document = await TextContentExtractor().extract(tmp_path, entry)

    assert document.chunks[0].content.startswith("hello bom")
    assert "\ufeff" not in document.chunks[0].content


async def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    _write(tmp_path / "broken.txt", b"valid start \xff\xfe invalid")
    entry = _entry_for(tmp_path, "broken.txt")

    with pytest.raises(ContentExtractionError, match="UnicodeDecodeError"):
        await TextContentExtractor().extract(tmp_path, entry)


async def test_rejects_oversized_text_without_reading_it(tmp_path: Path) -> None:
    _write(tmp_path / "big.txt", b"x" * 4096)
    entry = _entry_for(tmp_path, "big.txt")

    with pytest.raises(ContentTooLarge, match="text limit"):
        await TextContentExtractor(max_bytes=1024).extract(tmp_path, entry)


async def test_refuses_a_symlinked_file(tmp_path: Path) -> None:
    _write(tmp_path / "outside.txt", b"outside content")
    (tmp_path / "link.txt").symlink_to(tmp_path / "outside.txt")
    entry = replace(
        _entry_for(tmp_path, "outside.txt"),
        relative_path="link.txt",
        name="link.txt",
    )

    with pytest.raises(UnsafeFilePath, match="symlink"):
        await TextContentExtractor().extract(tmp_path, entry)


async def test_refuses_a_symlinked_directory_in_the_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", b"secret")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    entry = _entry_for(tmp_path, "linked/secret.txt")

    with pytest.raises(UnsafeFilePath, match="symlink"):
        await TextContentExtractor().extract(tmp_path, entry)


async def test_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    entry = _entry_for(tmp_path, "pipe.txt")

    with pytest.raises(UnsafeFilePath, match="regular file"):
        await TextContentExtractor().extract(tmp_path, entry)


def test_catalog_entries_cannot_even_describe_a_traversing_path(tmp_path: Path) -> None:
    _write(tmp_path / "notes.txt", b"content")

    with pytest.raises(InvalidStorageUri):
        replace(_entry_for(tmp_path, "notes.txt"), relative_path="../notes.txt")


def test_safe_file_revalidates_traversal_at_read_time(tmp_path: Path) -> None:
    """Even a path that never passed the domain must not be readable."""
    _write(tmp_path / "notes.txt", b"content")
    info = (tmp_path / "notes.txt").stat()

    with (
        pytest.raises(UnsafeFilePath),
        open_catalogued_file(
            tmp_path,
            "../notes.txt",
            expected_size=info.st_size,
            expected_mtime_ns=info.st_mtime_ns,
        ),
    ):
        pytest.fail("a traversing path must never open")


async def test_rejects_a_file_that_changed_after_the_catalog_scan(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes.txt", b"first version\n")
    entry = _entry_for(tmp_path, "notes.txt")
    _write(path, b"second version with different size\n")

    with pytest.raises(FileChangedDuringExtraction):
        await TextContentExtractor().extract(tmp_path, entry)


async def test_rejects_a_file_that_changes_during_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "notes.txt", b"stable content\n")
    entry = _entry_for(tmp_path, "notes.txt")
    real_fstat = os.fstat
    calls = {"count": 0}

    def flaky_fstat(descriptor: int) -> os.stat_result:
        calls["count"] += 1
        result = real_fstat(descriptor)
        if calls["count"] >= 2:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size + 1,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(
        "assistant.adapters.content.safe_file.os.fstat", flaky_fstat, raising=True
    )

    with pytest.raises(FileChangedDuringExtraction):
        await TextContentExtractor().extract(tmp_path, entry)


def test_registry_only_supports_whitelisted_suffixes(tmp_path: Path) -> None:
    _write(tmp_path / "notes.md", b"# notes\n")
    _write(tmp_path / "binary.bin", b"\x00\x01\x02")
    registry = SuffixExtractorRegistry()

    assert registry.extractor_for(_entry_for(tmp_path, "notes.md")) is not None
    assert registry.extractor_for(_entry_for(tmp_path, "binary.bin")) is None


async def test_extracts_pdf_text_page_by_page(tmp_path: Path) -> None:
    data = build_text_pdf(
        ["The important deadline is Friday", "Calculus notes live on page two"]
    )
    _write(tmp_path / "report.pdf", data)
    entry = _entry_for(tmp_path, "report.pdf")

    document = await PdfContentExtractor().extract(tmp_path, entry)

    assert document.content_sha256 == hashlib.sha256(data).hexdigest()
    assert document.extractor.name == "pdf-pypdf"
    spans = {chunk.source_span for chunk in document.chunks}
    assert {span.page_number for span in spans} == {1, 2}
    page_two = next(chunk for chunk in document.chunks if chunk.source_span.page_number == 2)
    assert "page two" in page_two.content
    assert page_two.source_span.line_start is None


async def test_pdf_without_a_text_layer_yields_no_chunks(tmp_path: Path) -> None:
    _write(tmp_path / "scanned.pdf", build_text_pdf(["   "]))
    entry = _entry_for(tmp_path, "scanned.pdf")

    document = await PdfContentExtractor().extract(tmp_path, entry)

    assert document.chunks == ()
    assert len(document.content_sha256) == 64


async def test_malformed_pdf_is_an_extraction_error(tmp_path: Path) -> None:
    _write(tmp_path / "broken.pdf", malformed_pdf())
    entry = _entry_for(tmp_path, "broken.pdf")

    with pytest.raises(ContentExtractionError, match="pypdf could not read"):
        await PdfContentExtractor().extract(tmp_path, entry)


async def test_encrypted_pdf_is_an_extraction_error(tmp_path: Path) -> None:
    _write(tmp_path / "locked.pdf", build_encrypted_pdf(["secret page"]))
    entry = _entry_for(tmp_path, "locked.pdf")

    with pytest.raises(ContentExtractionError, match="encrypted"):
        await PdfContentExtractor().extract(tmp_path, entry)


async def test_oversized_pdf_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path / "report.pdf", build_text_pdf(["page one"]))
    entry = _entry_for(tmp_path, "report.pdf")

    with pytest.raises(ContentTooLarge, match="PDF limit"):
        await PdfContentExtractor(max_bytes=64).extract(tmp_path, entry)
