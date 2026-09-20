"""Bounded evidence context: budget, dedup, ordering and privacy (ADR-0019)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from assistant.application.grounded_context import (
    MAX_EVIDENCE_CHARS_PER_CHUNK,
    MAX_EVIDENCE_CHARS_TOTAL,
    MAX_EVIDENCE_LIMIT,
    GroundedContextBuilder,
)
from assistant.domain.catalog import (
    CatalogEntry,
    CatalogPresence,
)
from assistant.domain.knowledge import (
    KnowledgeContextHit,
    KnowledgeContextSearchResult,
    SourceSpan,
)
from assistant.domain.storage import StorageKind, StorageUri

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class StubSearch:
    """A search service whose answer is scripted, so budgets are testable in isolation."""

    result: KnowledgeContextSearchResult
    calls: list[tuple[str, str | None, int]]

    def __init__(self, result: KnowledgeContextSearchResult) -> None:
        self.result = result
        self.calls = []

    async def search_context(
        self, query: str, *, root_id: str | None = None, limit: int = 8
    ) -> KnowledgeContextSearchResult:
        self.calls.append((query, root_id, limit))
        return self.result


def _hit(
    *,
    root_id: str = "archive-main",
    chunk_id: UUID | None = None,
    content: str = "The registration deadline is October 23.",
    span: SourceSpan | None = None,
    score: float = 1.0,
    relative_path: str = "Courses/SE/notice.md",
) -> KnowledgeContextHit:
    return KnowledgeContextHit(
        root_id=root_id,
        entry_id=uuid4(),
        chunk_id=chunk_id or uuid4(),
        ordinal=0,
        logical_uri=StorageUri.parse(f"vault://{root_id}/{relative_path}"),
        source_span=span or SourceSpan.lines(18, 31),
        content=content,
        score=score,
    )


def _entry(root_id: str = "archive-main") -> CatalogEntry:
    return CatalogEntry(
        id=uuid4(),
        root_id=root_id,
        storage_kind=StorageKind.VAULT,
        relative_path="Courses/SE/notice.md",
        name="notice.md",
        suffix=".md",
        size_bytes=120,
        mtime_ns=1,
        media_type="text/markdown",
        presence=CatalogPresence.PRESENT,
        first_seen_at=NOW,
        last_seen_at=NOW,
        metadata_updated_at=NOW,
    )


def _result(
    hits: tuple[KnowledgeContextHit, ...] = (),
    *,
    metadata: tuple[CatalogEntry, ...] = (),
    offline: tuple[str, ...] = (),
) -> KnowledgeContextSearchResult:
    return KnowledgeContextSearchResult(
        query="q",
        content_hits=hits,
        metadata_hits=metadata,
        offline_roots=offline,
        identity_mismatches=(),
    )


async def test_the_builder_searches_with_the_question_itself() -> None:
    """Retrieval stays deterministic: no model-chosen query, no expansion."""
    search = StubSearch(_result((_hit(),)))

    await GroundedContextBuilder(search).build(  # type: ignore[arg-type]
        "  What is the SE lab deadline?  ", root_id="archive-main", limit=3
    )

    assert search.calls == [("What is the SE lab deadline?", "archive-main", 3)]


async def test_evidence_ids_are_assigned_in_retrieval_order() -> None:
    first, second, third = _hit(), _hit(), _hit()
    search = StubSearch(_result((first, second, third)))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    assert [str(item.id) for item in context.evidence] == ["S1", "S2", "S3"]
    assert [item.chunk_id for item in context.evidence] == [
        first.chunk_id,
        second.chunk_id,
        third.chunk_id,
    ]


async def test_duplicate_chunks_are_deduplicated_keeping_the_first_rank() -> None:
    chunk_id = uuid4()
    best = _hit(chunk_id=chunk_id, content="first appearance wins")
    duplicate = _hit(chunk_id=chunk_id, content="second appearance")
    other_root_same_chunk = _hit(root_id="university", chunk_id=chunk_id)
    search = StubSearch(_result((best, duplicate, other_root_same_chunk)))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    assert len(context.evidence) == 2  # (archive-main, chunk) and (university, chunk)
    assert context.evidence[0].content == "first appearance wins"


async def test_per_chunk_truncation_is_deterministic_and_flagged() -> None:
    long_content = "x" * (MAX_EVIDENCE_CHARS_PER_CHUNK + 500)
    search = StubSearch(_result((_hit(content=long_content),)))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    (evidence,) = context.evidence
    assert len(evidence.content) == MAX_EVIDENCE_CHARS_PER_CHUNK
    assert evidence.content == long_content[:MAX_EVIDENCE_CHARS_PER_CHUNK]
    assert evidence.content_truncated is True
    assert context.context_truncated is True


async def test_the_total_budget_stops_adding_evidence() -> None:
    per_chunk = MAX_EVIDENCE_CHARS_PER_CHUNK
    needed = MAX_EVIDENCE_CHARS_TOTAL // per_chunk + 2
    hits = tuple(_hit(content=f"{index:04d}" + "y" * (per_chunk - 4)) for index in range(needed))
    search = StubSearch(_result(hits))

    context = await GroundedContextBuilder(search).build("q", limit=20)  # type: ignore[arg-type]

    total = sum(len(item.content) for item in context.evidence)
    assert total <= MAX_EVIDENCE_CHARS_TOTAL
    assert len(context.evidence) == MAX_EVIDENCE_CHARS_TOTAL // per_chunk
    assert context.context_truncated is True  # and says so honestly


async def test_a_small_result_is_not_marked_truncated() -> None:
    search = StubSearch(_result((_hit(), _hit())))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    assert context.context_truncated is False
    assert all(item.content_truncated is False for item in context.evidence)


async def test_the_character_budget_counts_unicode_code_points() -> None:
    content = "中文🙂" * (MAX_EVIDENCE_CHARS_PER_CHUNK // 3 + 10)
    search = StubSearch(_result((_hit(content=content),)))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    (evidence,) = context.evidence
    assert len(evidence.content) == MAX_EVIDENCE_CHARS_PER_CHUNK
    assert evidence.content.encode("utf-8").decode("utf-8") == evidence.content  # not split


async def test_metadata_and_offline_roots_are_reported_but_not_evidence() -> None:
    search = StubSearch(
        _result((_hit(),), metadata=(_entry(),), offline=("archive-main",))
    )

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    assert len(context.evidence) == 1  # a file name is not evidence
    assert context.metadata_matches[0].relative_path == "Courses/SE/notice.md"
    assert context.offline_roots == ("archive-main",)


async def test_the_model_payload_is_canonical_json_with_no_physical_paths() -> None:
    hit = _hit()
    search = StubSearch(_result((hit,)))
    context = await GroundedContextBuilder(search).build("What is the deadline?")  # type: ignore[arg-type]

    payload = context.to_payload()
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert rendered == json.dumps(
        json.loads(rendered), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert set(payload) == {"question", "evidence"}
    (entry,) = payload["evidence"]  # type: ignore[misc]
    assert set(entry) == {
        "source_id",
        "logical_uri",
        "source_span",
        "content",
        "content_truncated",
    }
    assert entry["logical_uri"] == f"vault://{hit.root_id}/Courses/SE/notice.md"
    assert entry["source_span"] == {"kind": "line", "line_start": 18, "line_end": 31}
    assert "/tmp" not in rendered


async def test_a_page_span_renders_as_a_page_in_the_payload() -> None:
    search = StubSearch(_result((_hit(span=SourceSpan.page(4), relative_path="report.pdf"),)))

    context = await GroundedContextBuilder(search).build("q")  # type: ignore[arg-type]

    (entry,) = context.to_payload()["evidence"]  # type: ignore[misc]
    assert entry["source_span"] == {"kind": "page", "page_number": 4}


async def test_the_question_and_limit_are_validated() -> None:
    builder = GroundedContextBuilder(StubSearch(_result()))  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await builder.build("   ")
    with pytest.raises(ValueError):
        await builder.build("q", limit=0)
    with pytest.raises(ValueError):
        await builder.build("q", limit=MAX_EVIDENCE_LIMIT + 1)
