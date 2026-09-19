"""Integration tests for the per-root SQLite knowledge index (ADR-0012).

Real SQLite files with real FTS5: ranking, trigram matching, LIKE fallback, escaping,
source spans and whole-document replacement are exactly the behaviours a mock would fake.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.domain.catalog import CatalogRoot
from assistant.domain.errors import (
    KnowledgeIndexCorrupt,
    KnowledgeIndexMismatch,
    KnowledgeIndexNeedsRebuild,
)
from assistant.domain.knowledge import (
    ExtractedChunk,
    ExtractorInfo,
    KnowledgeDocumentState,
    KnowledgeIndexStatus,
    SourceSpan,
)
from assistant.domain.storage import StorageKind, StorageRoot
from assistant.store.knowledge_index import (
    INDEX_SCHEMA_VERSION,
    SqliteKnowledgeIndexFactory,
    sqlite_search_capabilities,
)

NOW = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
TEXT_EXTRACTOR = ExtractorInfo(name="text", version=1)
SHA = "a" * 64


class ChunkIdFactory:
    """Deterministic chunk ids: 1, 2, 3, ..."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


class FixedIndexLocation:
    """Resolver that always returns the same path, for binding tests."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def location_for(self, root: CatalogRoot) -> Path:
        return self._path


def _root(
    root_id: str = "university",
    *,
    kind: StorageKind = StorageKind.LOCAL,
    path: str = "/tmp/nowhere",
) -> CatalogRoot:
    return CatalogRoot(
        root=StorageRoot(root_id=root_id, kind=kind, label="Root"),
        last_known_path=path,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_scanned_at=NOW,
    )


def _state(
    entry_id: UUID,
    *,
    status: KnowledgeIndexStatus = KnowledgeIndexStatus.INDEXED,
    root_id: str = "university",
    kind: StorageKind = StorageKind.LOCAL,
    relative_path: str = "notes/a.md",
    size_bytes: int = 10,
    mtime_ns: int = 1000,
    last_error: str | None = None,
) -> KnowledgeDocumentState:
    from assistant.domain.storage import StorageUri

    if status in {KnowledgeIndexStatus.INDEXED, KnowledgeIndexStatus.EMPTY}:
        return KnowledgeDocumentState(
            entry_id=entry_id,
            logical_uri=StorageUri(kind=kind, root_id=root_id, relative_path=relative_path),
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            status=status,
            last_attempted_at=NOW,
            content_sha256=SHA,
            extractor=TEXT_EXTRACTOR,
            indexed_at=NOW,
        )
    if status is KnowledgeIndexStatus.ERROR:
        return KnowledgeDocumentState(
            entry_id=entry_id,
            logical_uri=StorageUri(kind=kind, root_id=root_id, relative_path=relative_path),
            relative_path=relative_path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            status=status,
            last_attempted_at=NOW,
            last_error=last_error or "RuntimeError: boom",
        )
    return KnowledgeDocumentState(
        entry_id=entry_id,
        logical_uri=StorageUri(kind=kind, root_id=root_id, relative_path=relative_path),
        relative_path=relative_path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        status=KnowledgeIndexStatus.UNSUPPORTED,
        last_attempted_at=NOW,
    )


def _chunk(ordinal: int, content: str, *, line_start: int = 1, line_end: int = 1) -> ExtractedChunk:
    return ExtractedChunk(
        ordinal=ordinal, content=content, source_span=SourceSpan.lines(line_start, line_end)
    )


@pytest.fixture
def factory(tmp_path: Path) -> SqliteKnowledgeIndexFactory:
    return SqliteKnowledgeIndexFactory(
        KnowledgeIndexLocator(cache_root=tmp_path / "cache"),
        new_chunk_id=ChunkIdFactory(),
    )


def test_this_environment_supports_fts5_trigram() -> None:
    capabilities = sqlite_search_capabilities()

    assert capabilities.fts5
    assert capabilities.trigram


async def test_first_open_creates_schema_and_binding(
    factory: SqliteKnowledgeIndexFactory, tmp_path: Path
) -> None:
    index = await factory.open(_root(), create=True)

    assert index is not None
    path = tmp_path / "cache" / "university" / "index.sqlite3"
    with sqlite3.connect(path) as connection:
        meta = dict(connection.execute("SELECT key, value FROM knowledge_index_meta"))
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert meta["schema_version"] == str(INDEX_SCHEMA_VERSION)
    assert meta["root_id"] == "university"
    assert meta["storage_kind"] == "local"
    assert {"knowledge_documents", "knowledge_chunks", "knowledge_chunks_fts"} <= tables
    assert journal_mode == "delete"
    assert not path.with_name("index.sqlite3-wal").exists()


async def test_open_without_create_returns_none_when_missing(
    factory: SqliteKnowledgeIndexFactory
) -> None:
    assert await factory.open(_root(), create=False) is None


async def test_index_is_bound_to_one_root_and_kind(
    factory: SqliteKnowledgeIndexFactory, tmp_path: Path
) -> None:
    """One index database serves exactly one (root_id, kind) pair."""
    internal = tmp_path / "vault" / ".pa"
    internal.mkdir(parents=True)
    path = internal / "index.sqlite3"
    await SqliteKnowledgeIndexFactory(FixedIndexLocation(path)).open(
        _root("university"), create=True
    )
    other_factory = SqliteKnowledgeIndexFactory(FixedIndexLocation(path))

    with pytest.raises(KnowledgeIndexMismatch, match="bound to root"):
        await other_factory.open(
            _root("archive-main", kind=StorageKind.VAULT, path=str(tmp_path / "vault")),
            create=True,
        )
    with pytest.raises(KnowledgeIndexMismatch, match="bound to kind"):
        await other_factory.open(
            _root("university", kind=StorageKind.VAULT, path=str(tmp_path / "vault")),
            create=True,
        )


async def test_unsupported_schema_version_asks_for_a_rebuild(
    factory: SqliteKnowledgeIndexFactory, tmp_path: Path
) -> None:
    await factory.open(_root(), create=True)
    path = tmp_path / "cache" / "university" / "index.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE knowledge_index_meta SET value = '99' WHERE key = 'schema_version'"
        )

    with pytest.raises(KnowledgeIndexNeedsRebuild, match="schema_version"):
        await factory.open(_root(), create=True)


async def test_a_foreign_sqlite_file_is_reported_as_corrupt(
    factory: SqliteKnowledgeIndexFactory, tmp_path: Path
) -> None:
    path = tmp_path / "cache" / "university" / "index.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE something_else (x)")

    with pytest.raises(KnowledgeIndexCorrupt):
        await factory.open(_root(), create=False)


async def test_document_and_chunk_round_trip(factory: SqliteKnowledgeIndexFactory) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    state = _state(entry_id)

    await index.replace_document(state=state, chunks=[_chunk(0, "hello deadline")])

    stored = await index.get_document_state(entry_id)
    states = await index.list_document_states()
    assert stored == state
    assert [item.entry_id for item in states] == [entry_id]
    assert await index.get_document_state(uuid4()) is None


async def test_search_returns_text_with_source_spans(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    body = "\n".join(f"line {number}" for number in range(1, 21))
    await index.replace_document(
        state=_state(entry_id, relative_path="notes/deadlines.md"),
        chunks=[
            _chunk(0, "line 1\nline 2", line_start=1, line_end=2),
            _chunk(1, "line 20 important deadline", line_start=20, line_end=20),
        ],
    )

    hits = await index.search("important deadline", limit=10)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.entry_id == entry_id
    assert hit.source_span.line_start == 20
    assert hit.source_span.line_end == 20
    assert "important deadline" in hit.snippet
    assert len(hit.snippet) <= 310
    assert str(hit.logical_uri) == "local://university/notes/deadlines.md"
    assert hit.name == "deadlines.md"
    assert isinstance(hit.rank, float)
    assert body.count("line 20") == 1


async def test_search_matches_chinese_substrings(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    await index.replace_document(
        state=_state(uuid4(), relative_path="notes/高数.md"),
        chunks=[_chunk(0, "高等数学期末考试重点", line_start=1, line_end=1)],
    )

    long_query = await index.search("高等数", limit=5)
    short_query = await index.search("数学", limit=5)

    assert len(long_query) == 1
    assert len(short_query) == 1
    assert "高等数学期末考试重点" in short_query[0].snippet


async def test_search_treats_wildcards_and_fts_operators_as_text(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    await index.replace_document(
        state=_state(uuid4(), relative_path="notes/report.md"),
        chunks=[
            _chunk(0, "progress is 100% done", line_start=1, line_end=1),
            _chunk(1, "a:b OR NEAR(x) is literal text", line_start=2, line_end=2),
            _chunk(2, "plain words only", line_start=3, line_end=3),
        ],
    )

    percent = await index.search("100%", limit=5)
    underscore = await index.search("_", limit=5)
    operators = await index.search("a:b OR NEAR(x)", limit=5)
    quoted = await index.search('"quoted"', limit=5)

    assert [hit.source_span.line_start for hit in percent] == [1]
    assert underscore == []
    assert [hit.source_span.line_start for hit in operators] == [2]
    assert quoted == []


async def test_replacing_a_document_removes_its_old_text(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    await index.replace_document(
        state=_state(entry_id, size_bytes=10),
        chunks=[_chunk(0, "old secret content", line_start=1, line_end=1)],
    )

    await index.replace_document(
        state=_state(entry_id, size_bytes=20, mtime_ns=2000),
        chunks=[_chunk(0, "new content", line_start=1, line_end=1)],
    )

    assert await index.search("old secret", limit=5) == []
    assert len(await index.search("new content", limit=5)) == 1


async def test_marking_error_removes_searchable_text(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    await index.replace_document(
        state=_state(entry_id), chunks=[_chunk(0, "stale text", line_start=1, line_end=1)]
    )

    await index.mark_error(
        state=_state(entry_id, status=KnowledgeIndexStatus.ERROR, last_error="boom"),
        error="RuntimeError: boom",
    )

    state = await index.get_document_state(entry_id)
    assert state is not None
    assert state.status is KnowledgeIndexStatus.ERROR
    assert state.last_error == "RuntimeError: boom"
    assert state.content_sha256 is None
    assert await index.search("stale text", limit=5) == []


async def test_marking_unsupported_stores_no_hash_and_no_chunks(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    await index.replace_document(
        state=_state(entry_id), chunks=[_chunk(0, "previous text", line_start=1, line_end=1)]
    )

    await index.mark_unsupported(state=_state(entry_id, status=KnowledgeIndexStatus.UNSUPPORTED))

    state = await index.get_document_state(entry_id)
    assert state is not None
    assert state.status is KnowledgeIndexStatus.UNSUPPORTED
    assert state.content_sha256 is None
    assert state.indexed_at is None
    assert await index.search("previous text", limit=5) == []


async def test_removing_a_document_removes_its_text(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    await index.replace_document(
        state=_state(entry_id), chunks=[_chunk(0, "gone soon", line_start=1, line_end=1)]
    )

    await index.remove_document(entry_id)

    assert await index.get_document_state(entry_id) is None
    assert await index.search("gone soon", limit=5) == []


async def test_empty_documents_have_no_chunks(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()

    await index.replace_document(
        state=_state(entry_id, status=KnowledgeIndexStatus.EMPTY), chunks=[]
    )

    state = await index.get_document_state(entry_id)
    assert state is not None
    assert state.status is KnowledgeIndexStatus.EMPTY
    assert state.content_sha256 == SHA


async def test_search_limit_and_deterministic_order(
    factory: SqliteKnowledgeIndexFactory,
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    for number in range(3):
        await index.replace_document(
            state=_state(uuid4(), relative_path=f"notes/file-{number}.md"),
            chunks=[_chunk(0, f"deadline number {number}", line_start=1, line_end=1)],
        )

    first = await index.search("deadline", limit=2)
    second = await index.search("deadline", limit=2)

    assert len(first) == 2
    assert [str(hit.logical_uri) for hit in first] == [str(hit.logical_uri) for hit in second]


async def test_search_validates_its_arguments(factory: SqliteKnowledgeIndexFactory) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None

    with pytest.raises(ValueError, match="query"):
        await index.search("   ", limit=5)
    with pytest.raises(ValueError, match="limit"):
        await index.search("deadline", limit=0)


async def test_database_constraints_protect_chunk_integrity(
    factory: SqliteKnowledgeIndexFactory, tmp_path: Path
) -> None:
    index = await factory.open(_root(), create=True)
    assert index is not None
    entry_id = uuid4()
    await index.replace_document(
        state=_state(entry_id), chunks=[_chunk(0, "text", line_start=1, line_end=1)]
    )
    path = tmp_path / "cache" / "university" / "index.sqlite3"

    def raw_insert(
        entry: str,
        ordinal: int,
        page: object,
        line_start: object,
        line_end: object,
    ) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO knowledge_chunks (id, entry_id, ordinal, page_number, "
                "line_start, line_end, content) VALUES (?, ?, ?, ?, ?, ?, 'x')",
                (str(uuid4()), entry, ordinal, page, line_start, line_end),
            )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        raw_insert(str(entry_id), 0, None, 1, 1)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        raw_insert(str(uuid4()), 1, None, 1, 1)
    with pytest.raises(sqlite3.IntegrityError, match="source_span_is_exclusive"):
        raw_insert(str(entry_id), 2, 3, 1, 1)
