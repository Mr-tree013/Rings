"""Unit tests for knowledge domain invariants (ADR-0012)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.domain.errors import InvalidKnowledgeDocument, InvalidSourceSpan
from assistant.domain.knowledge import (
    ExtractedChunk,
    ExtractedDocument,
    ExtractorInfo,
    KnowledgeDocumentState,
    KnowledgeIndexStatus,
    SourceSpan,
    SourceSpanKind,
)
from assistant.domain.storage import StorageKind, StorageUri

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
SHA = "b" * 64


def test_line_spans_are_inclusive_and_positive() -> None:
    span = SourceSpan.lines(18, 31)

    assert span.kind is SourceSpanKind.LINE
    assert span.describe() == "lines 18-31"
    assert SourceSpan.lines(1, 1).line_start == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (2, 1), (-1, 3)],
)
def test_invalid_line_spans_are_rejected(start: int, end: int) -> None:
    with pytest.raises(InvalidSourceSpan):
        SourceSpan.lines(start, end)


def test_page_spans_carry_only_a_page_number() -> None:
    span = SourceSpan.page(3)

    assert span.kind is SourceSpanKind.PAGE
    assert span.describe() == "page 3"
    with pytest.raises(InvalidSourceSpan):
        SourceSpan.page(0)
    with pytest.raises(InvalidSourceSpan):
        SourceSpan(kind=SourceSpanKind.PAGE, page_number=2, line_start=1, line_end=2)
    with pytest.raises(InvalidSourceSpan):
        SourceSpan(kind=SourceSpanKind.LINE, line_start=1, line_end=2, page_number=2)
    with pytest.raises(InvalidSourceSpan):
        SourceSpan(kind=SourceSpanKind.LINE)


def test_chunks_need_an_ordinal_and_content() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="ordinal"):
        ExtractedChunk(ordinal=-1, content="text", source_span=SourceSpan.lines(1, 1))
    with pytest.raises(InvalidKnowledgeDocument, match="content"):
        ExtractedChunk(ordinal=0, content="   \n", source_span=SourceSpan.lines(1, 1))


def test_extractor_identity_is_validated() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="name"):
        ExtractorInfo(name="  ", version=1)
    with pytest.raises(InvalidKnowledgeDocument, match="version"):
        ExtractorInfo(name="text", version=0)


@pytest.mark.parametrize("sha", ["A" * 64, "b" * 63, "not-a-hash"])
def test_content_hashes_must_be_lowercase_sha256(sha: str) -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="sha256"):
        ExtractedDocument(
            content_sha256=sha, extractor=ExtractorInfo("text", 1), chunks=()
        )


def test_chunk_ordinals_must_be_contiguous() -> None:
    span = SourceSpan.lines(1, 1)
    with pytest.raises(InvalidKnowledgeDocument, match="contiguous"):
        ExtractedDocument(
            content_sha256=SHA,
            extractor=ExtractorInfo("text", 1),
            chunks=(ExtractedChunk(1, "text", span),),
        )


def test_status_vocabulary_is_exactly_four_states() -> None:
    assert {status.value for status in KnowledgeIndexStatus} == {
        "indexed",
        "empty",
        "error",
        "unsupported",
    }


def _state(**overrides: object) -> KnowledgeDocumentState:
    values: dict[str, object] = {
        "entry_id": uuid4(),
        "logical_uri": StorageUri(
            kind=StorageKind.LOCAL, root_id="university", relative_path="notes/a.md"
        ),
        "relative_path": "notes/a.md",
        "size_bytes": 10,
        "mtime_ns": 1000,
        "status": KnowledgeIndexStatus.INDEXED,
        "last_attempted_at": NOW,
        "content_sha256": SHA,
        "extractor": ExtractorInfo("text", 1),
        "indexed_at": NOW,
    }
    values.update(overrides)
    return KnowledgeDocumentState(**values)  # type: ignore[arg-type]


def test_indexed_state_requires_provenance() -> None:
    assert _state().content_sha256 == SHA
    with pytest.raises(InvalidKnowledgeDocument, match="content hash"):
        _state(content_sha256=None)
    with pytest.raises(InvalidKnowledgeDocument, match="extractor"):
        _state(extractor=None)
    with pytest.raises(InvalidKnowledgeDocument, match="indexed_at"):
        _state(indexed_at=None)


def test_error_state_requires_a_message() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="last_error"):
        _state(
            status=KnowledgeIndexStatus.ERROR,
            content_sha256=None,
            extractor=None,
            indexed_at=None,
        )


def test_unsupported_state_carries_no_hash() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="hash"):
        _state(status=KnowledgeIndexStatus.UNSUPPORTED)


def test_state_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="timezone-aware"):
        _state(last_attempted_at=datetime(2026, 9, 20, 14, 0))


def test_state_uri_must_describe_the_same_document() -> None:
    with pytest.raises(InvalidKnowledgeDocument, match="same document"):
        _state(relative_path="other.md")

