"""SQLite-backed, per-root full-text knowledge index (ADR-0009, ADR-0012).

Each storage root owns one index database, bound to its `root_id` and `storage_kind`:

```text
vault://archive-main  ->  <vault>/.pa/index.sqlite3
local://university    ->  ~/.cache/growing-assistant/knowledge/university/index.sqlite3
```

The index is derived data: it can be rebuilt from the files at any time, it never lives in
the runtime authority, and it is opened with `journal_mode=DELETE` because it may sit on a
removable drive where leaving `-wal`/`-shm` files behind after an unplug is worse than the
concurrency benefit WAL would give.

Every mutation replaces a whole document inside one transaction (chunks, FTS rows and the
document row), so a document can never keep serving text that no longer matches the file.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from assistant.domain.catalog import CatalogRoot
from assistant.domain.errors import (
    DomainError,
    Fts5Unavailable,
    KnowledgeIndexCorrupt,
    KnowledgeIndexMismatch,
    KnowledgeIndexNeedsRebuild,
    StorageRootIdentityMismatch,
    TrigramTokenizerUnavailable,
)
from assistant.domain.knowledge import (
    ExtractedChunk,
    ExtractorInfo,
    KnowledgeDocumentState,
    KnowledgeIndexStatus,
    KnowledgeSearchHit,
    SourceSpan,
)
from assistant.domain.storage import StorageKind, StorageUri
from assistant.store.db import JOURNAL_MODE_DELETE, Database, transaction
from assistant.store.search_text import FTS_MIN_QUERY_CHARS, excerpt, fts5_phrase, like_pattern
from assistant.store.serialization import from_utc_iso, to_utc_iso

INDEX_SCHEMA_VERSION = 1
MAX_STORED_ERROR_CHARS = 2000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    entry_id          TEXT PRIMARY KEY,
    relative_path     TEXT NOT NULL,
    logical_uri       TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    content_sha256    TEXT NULL,
    extractor_name    TEXT NULL,
    extractor_version INTEGER NULL,
    status            TEXT NOT NULL,
    indexed_at        TEXT NULL,
    last_attempted_at TEXT NOT NULL,
    last_error        TEXT NULL,
    CONSTRAINT knowledge_documents_status_is_known
        CHECK (status IN ('indexed', 'empty', 'error', 'unsupported')),
    CONSTRAINT knowledge_documents_indexed_has_provenance
        CHECK (status NOT IN ('indexed', 'empty')
               OR (content_sha256 IS NOT NULL AND indexed_at IS NOT NULL
                   AND extractor_name IS NOT NULL AND extractor_version IS NOT NULL)),
    CONSTRAINT knowledge_documents_error_has_message
        CHECK (status <> 'error'
               OR (last_error IS NOT NULL AND length(trim(last_error)) > 0)),
    CONSTRAINT knowledge_documents_size_not_negative CHECK (size_bytes >= 0),
    CONSTRAINT knowledge_documents_mtime_not_negative CHECK (mtime_ns >= 0)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          TEXT PRIMARY KEY,
    entry_id    TEXT NOT NULL REFERENCES knowledge_documents (entry_id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    page_number INTEGER NULL,
    line_start  INTEGER NULL,
    line_end    INTEGER NULL,
    content     TEXT NOT NULL,
    UNIQUE (entry_id, ordinal),
    CONSTRAINT knowledge_chunks_ordinal_not_negative CHECK (ordinal >= 0),
    CONSTRAINT knowledge_chunks_source_span_is_exclusive
        CHECK ((page_number IS NOT NULL AND line_start IS NULL AND line_end IS NULL)
            OR (page_number IS NULL AND line_start IS NOT NULL AND line_end IS NOT NULL)),
    CONSTRAINT knowledge_chunks_page_positive
        CHECK (page_number IS NULL OR page_number >= 1),
    CONSTRAINT knowledge_chunks_lines_positive
        CHECK (line_start IS NULL OR (line_start >= 1 AND line_end >= line_start)),
    CONSTRAINT knowledge_chunks_content_not_blank CHECK (length(trim(content)) > 0)
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
    content,
    chunk_id UNINDEXED,
    tokenize = 'trigram'
);
"""

_DOCUMENT_COLUMNS = (
    "entry_id, relative_path, logical_uri, size_bytes, mtime_ns, content_sha256, "
    "extractor_name, extractor_version, status, indexed_at, last_attempted_at, last_error"
)

_HIT_COLUMNS = (
    "c.id AS chunk_id, c.entry_id AS entry_id, c.ordinal AS ordinal, "
    "c.page_number AS page_number, c.line_start AS line_start, c.line_end AS line_end, "
    "c.content AS content, d.relative_path AS relative_path, d.logical_uri AS logical_uri"
)

_UPSERT_DOCUMENT_SQL = f"""
INSERT INTO knowledge_documents ({_DOCUMENT_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (entry_id) DO UPDATE SET
    relative_path = excluded.relative_path,
    logical_uri = excluded.logical_uri,
    size_bytes = excluded.size_bytes,
    mtime_ns = excluded.mtime_ns,
    content_sha256 = excluded.content_sha256,
    extractor_name = excluded.extractor_name,
    extractor_version = excluded.extractor_version,
    status = excluded.status,
    indexed_at = excluded.indexed_at,
    last_attempted_at = excluded.last_attempted_at,
    last_error = excluded.last_error
"""

_MATCH_SQL = f"""
SELECT {_HIT_COLUMNS}, bm25(knowledge_chunks_fts) AS rank
  FROM knowledge_chunks_fts
  JOIN knowledge_chunks c ON c.id = knowledge_chunks_fts.chunk_id
  JOIN knowledge_documents d ON d.entry_id = c.entry_id
 WHERE knowledge_chunks_fts MATCH ?
   AND d.status = 'indexed'
 ORDER BY rank
 LIMIT ?
"""

_LIKE_SQL = f"""
SELECT {_HIT_COLUMNS}
  FROM knowledge_chunks c
  JOIN knowledge_documents d ON d.entry_id = c.entry_id
 WHERE d.status = 'indexed'
   AND c.content LIKE ? ESCAPE '\\'
 ORDER BY d.relative_path, c.ordinal
 LIMIT ?
"""


class IndexLocationResolver(Protocol):
    """Resolves the index database path for a storage root."""

    def location_for(self, root: CatalogRoot) -> Path:
        """Return where this root's knowledge index lives."""
        ...


def check_fts5_support(connection: sqlite3.Connection) -> None:
    """Prove this SQLite build supports FTS5 with the trigram tokenizer.

    Raises:
        Fts5Unavailable: the FTS5 module is missing.
        TrigramTokenizerUnavailable: FTS5 exists but is built without `trigram`.
    """
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.fts5_capability_probe USING fts5("
            "content, tokenize = 'trigram')"
        )
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "no such module" in message:
            raise Fts5Unavailable(f"SQLite has no FTS5 module: {exc}") from exc
        if "tokenizer" in message or "trigram" in message:
            raise TrigramTokenizerUnavailable(
                f"SQLite FTS5 has no trigram tokenizer: {exc}"
            ) from exc
        raise Fts5Unavailable(f"FTS5 capability probe failed: {exc}") from exc


@dataclass(frozen=True, slots=True)
class SearchCapabilities:
    """What this SQLite build can do for knowledge search."""

    fts5: bool
    trigram: bool
    fts5_version: str | None
    detail: str | None


def sqlite_search_capabilities() -> SearchCapabilities:
    """Probe FTS5 and the trigram tokenizer on a throwaway in-memory database.

    Used by `pw doctor`: it touches no user file and creates no database on disk.
    """
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(content)"
            )
        except sqlite3.OperationalError as exc:
            return SearchCapabilities(
                fts5=False, trigram=False, fts5_version=None, detail=str(exc)
            )
        version = None
        try:
            row = connection.execute("SELECT sqlite_version()").fetchone()
            version = None if row is None else str(row[0])
        except sqlite3.Error:  # pragma: no cover - defensive
            version = None
        try:
            check_fts5_support(connection)
        except TrigramTokenizerUnavailable as exc:
            return SearchCapabilities(
                fts5=True, trigram=False, fts5_version=version, detail=str(exc)
            )
        except Fts5Unavailable as exc:  # pragma: no cover - unreachable after the probe above
            return SearchCapabilities(
                fts5=False, trigram=False, fts5_version=version, detail=str(exc)
            )
        return SearchCapabilities(
            fts5=True, trigram=True, fts5_version=version, detail=None
        )
    finally:
        connection.close()


class SqliteKnowledgeIndexFactory:
    """Opens a root's knowledge index, creating and binding it on first use."""

    def __init__(
        self,
        locations: IndexLocationResolver,
        *,
        new_chunk_id: Callable[[], UUID] = uuid4,
        journal_mode: str = JOURNAL_MODE_DELETE,
    ) -> None:
        self._locations = locations
        self._new_chunk_id = new_chunk_id
        self._journal_mode = journal_mode

    async def open(
        self, root: CatalogRoot, *, create: bool = False
    ) -> _SqliteKnowledgeIndex | None:
        return await asyncio.to_thread(self._open_sync, root, create)

    def _open_sync(self, root: CatalogRoot, create: bool) -> _SqliteKnowledgeIndex | None:
        path = self._locations.location_for(root)
        if not create and not path.is_file():
            return None
        if create:
            _prepare_index_location(root, path)
        database = Database.at(
            path, create_parent=create, journal_mode=self._journal_mode
        )
        try:
            if create:
                with database.connect() as connection:
                    check_fts5_support(connection)
                    connection.executescript(_SCHEMA_SQL)
                    _ensure_binding(connection, root)
            else:
                _verify_binding(database, root)
        except sqlite3.DatabaseError as exc:
            raise KnowledgeIndexCorrupt(f"{path} is not a readable index: {exc}") from exc
        return _SqliteKnowledgeIndex(database, root, new_chunk_id=self._new_chunk_id)


class _SqliteKnowledgeIndex:
    """One root's index: blocking work happens in worker threads with private connections."""

    def __init__(
        self,
        database: Database,
        root: CatalogRoot,
        *,
        new_chunk_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._database = database
        self._root = root
        self._new_chunk_id = new_chunk_id

    @property
    def root_id(self) -> str:
        """The root this index is bound to."""
        return self._root.root.root_id

    async def get_document_state(self, entry_id: UUID) -> KnowledgeDocumentState | None:
        return await asyncio.to_thread(self._get_document_state_sync, entry_id)

    async def list_document_states(
        self, *, limit: int | None = None
    ) -> list[KnowledgeDocumentState]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_document_states_sync, limit)

    async def replace_document(
        self, *, state: KnowledgeDocumentState, chunks: Sequence[ExtractedChunk]
    ) -> None:
        await asyncio.to_thread(self._replace_document_sync, state, tuple(chunks))

    async def mark_error(self, *, state: KnowledgeDocumentState, error: str) -> None:
        await asyncio.to_thread(self._mark_error_sync, state, error)

    async def mark_unsupported(self, *, state: KnowledgeDocumentState) -> None:
        await asyncio.to_thread(self._mark_unsupported_sync, state)

    async def remove_document(self, entry_id: UUID) -> None:
        await asyncio.to_thread(self._remove_document_sync, entry_id)

    async def search(self, query: str, *, limit: int) -> list[KnowledgeSearchHit]:
        if not query.strip():
            raise ValueError("query must not be blank")
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        return await asyncio.to_thread(self._search_sync, query, limit)

    # ------------------------------------------------------------ blocking internals

    def _get_document_state_sync(self, entry_id: UUID) -> KnowledgeDocumentState | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM knowledge_documents WHERE entry_id = ?",
                (str(entry_id),),
            ).fetchone()
        return None if row is None else _row_to_state(row)

    def _list_document_states_sync(self, limit: int | None) -> list[KnowledgeDocumentState]:
        statement = (
            f"SELECT {_DOCUMENT_COLUMNS} FROM knowledge_documents ORDER BY relative_path"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_state(row) for row in rows]

    def _replace_document_sync(
        self, state: KnowledgeDocumentState, chunks: tuple[ExtractedChunk, ...]
    ) -> None:
        if state.status is KnowledgeIndexStatus.INDEXED and not chunks:
            raise ValueError("an INDEXED document needs at least one chunk")
        if state.status is KnowledgeIndexStatus.EMPTY and chunks:
            raise ValueError("an EMPTY document must not carry chunks")
        if state.status not in {KnowledgeIndexStatus.INDEXED, KnowledgeIndexStatus.EMPTY}:
            raise ValueError(
                f"replace_document handles indexed/empty states, not {state.status}"
            )
        with self._database.connect() as connection, transaction(connection):
            _delete_chunks(connection, state.entry_id)
            _write_document(connection, state)
            for chunk in chunks:
                chunk_id = str(self._new_chunk_id())
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        id, entry_id, ordinal, page_number, line_start, line_end, content
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        str(state.entry_id),
                        chunk.ordinal,
                        chunk.source_span.page_number,
                        chunk.source_span.line_start,
                        chunk.source_span.line_end,
                        chunk.content,
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks_fts (content, chunk_id) VALUES (?, ?)",
                    (chunk.content, chunk_id),
                )

    def _mark_error_sync(self, state: KnowledgeDocumentState, error: str) -> None:
        final = replace(
            state,
            status=KnowledgeIndexStatus.ERROR,
            last_error=_truncate_error(error),
            content_sha256=None,
            extractor=None,
            indexed_at=None,
        )
        self._replace_state_only(final)

    def _mark_unsupported_sync(self, state: KnowledgeDocumentState) -> None:
        final = replace(
            state,
            status=KnowledgeIndexStatus.UNSUPPORTED,
            content_sha256=None,
            extractor=None,
            indexed_at=None,
            last_error=None,
        )
        self._replace_state_only(final)

    def _replace_state_only(self, state: KnowledgeDocumentState) -> None:
        """Write a document state and drop any previously searchable text."""
        with self._database.connect() as connection, transaction(connection):
            _delete_chunks(connection, state.entry_id)
            _write_document(connection, state)

    def _remove_document_sync(self, entry_id: UUID) -> None:
        with self._database.connect() as connection, transaction(connection):
            _delete_chunks(connection, entry_id)
            connection.execute(
                "DELETE FROM knowledge_documents WHERE entry_id = ?", (str(entry_id),)
            )

    def _search_sync(self, query: str, limit: int) -> list[KnowledgeSearchHit]:
        with self._database.connect() as connection:
            if len(query) < FTS_MIN_QUERY_CHARS:
                return _like_hits(connection, query, limit)
            try:
                rows = connection.execute(_MATCH_SQL, (fts5_phrase(query), limit)).fetchall()
            except sqlite3.OperationalError:
                # A query the tokenizer cannot represent is not a user error.
                return _like_hits(connection, query, limit)
        return [_row_to_hit(row, float(row["rank"]), query) for row in rows]


def _like_hits(
    connection: sqlite3.Connection, query: str, limit: int
) -> list[KnowledgeSearchHit]:
    rows = connection.execute(_LIKE_SQL, (like_pattern(query), limit)).fetchall()
    return [
        _row_to_hit(row, float(position), query) for position, row in enumerate(rows)
    ]


def _prepare_index_location(root: CatalogRoot, path: Path) -> None:
    """Create the index location, only where creating it is legitimate."""
    if root.root.kind is StorageKind.VAULT:
        internal = path.parent
        if internal.name != ".pa" or not internal.is_dir():
            raise StorageRootIdentityMismatch(
                f"{root.root.root_id!r} has no initialised .pa directory at {path.parent}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def _ensure_binding(connection: sqlite3.Connection, root: CatalogRoot) -> None:
    values = _read_meta(connection)
    if not values:
        with transaction(connection):
            for key, value in (
                ("schema_version", str(INDEX_SCHEMA_VERSION)),
                ("root_id", root.root.root_id),
                ("storage_kind", root.root.kind.value),
            ):
                connection.execute(
                    "INSERT INTO knowledge_index_meta (key, value) VALUES (?, ?)", (key, value)
                )
        return
    _validate_binding(values, root)


def _verify_binding(database: Database, root: CatalogRoot) -> None:
    with database.connect() as connection:
        values = _read_meta(connection)
    if not values:
        raise KnowledgeIndexCorrupt("index database has no binding metadata")
    _validate_binding(values, root)


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM knowledge_index_meta").fetchall()
    except sqlite3.OperationalError as exc:
        raise KnowledgeIndexCorrupt(f"index database has no metadata table: {exc}") from exc
    return {str(row["key"]): str(row["value"]) for row in rows}


def _validate_binding(values: dict[str, str], root: CatalogRoot) -> None:
    version = values.get("schema_version")
    if version != str(INDEX_SCHEMA_VERSION):
        raise KnowledgeIndexNeedsRebuild(
            f"index schema_version {version!r} is not {INDEX_SCHEMA_VERSION}"
        )
    if values.get("root_id") != root.root.root_id:
        raise KnowledgeIndexMismatch(
            f"index is bound to root {values.get('root_id')!r}, "
            f"not {root.root.root_id!r}"
        )
    if values.get("storage_kind") != root.root.kind.value:
        raise KnowledgeIndexMismatch(
            f"index is bound to kind {values.get('storage_kind')!r}, "
            f"not {root.root.kind.value!r}"
        )


def _delete_chunks(connection: sqlite3.Connection, entry_id: UUID) -> None:
    connection.execute(
        "DELETE FROM knowledge_chunks_fts WHERE chunk_id IN "
        "(SELECT id FROM knowledge_chunks WHERE entry_id = ?)",
        (str(entry_id),),
    )
    connection.execute("DELETE FROM knowledge_chunks WHERE entry_id = ?", (str(entry_id),))


def _write_document(connection: sqlite3.Connection, state: KnowledgeDocumentState) -> None:
    connection.execute(
        _UPSERT_DOCUMENT_SQL,
        (
            str(state.entry_id),
            state.relative_path,
            str(state.logical_uri),
            state.size_bytes,
            state.mtime_ns,
            state.content_sha256,
            None if state.extractor is None else state.extractor.name,
            None if state.extractor is None else state.extractor.version,
            state.status.value,
            None if state.indexed_at is None else to_utc_iso(state.indexed_at),
            to_utc_iso(state.last_attempted_at),
            state.last_error,
        ),
    )


def _row_to_state(row: sqlite3.Row) -> KnowledgeDocumentState:
    try:
        extractor_name = row["extractor_name"]
        extractor_version = row["extractor_version"]
        return KnowledgeDocumentState(
            entry_id=UUID(str(row["entry_id"])),
            logical_uri=StorageUri.parse(str(row["logical_uri"])),
            relative_path=str(row["relative_path"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            status=KnowledgeIndexStatus(str(row["status"])),
            last_attempted_at=from_utc_iso(str(row["last_attempted_at"])),
            content_sha256=None if row["content_sha256"] is None else str(row["content_sha256"]),
            extractor=(
                None
                if extractor_name is None or extractor_version is None
                else ExtractorInfo(name=str(extractor_name), version=int(extractor_version))
            ),
            indexed_at=None if row["indexed_at"] is None else from_utc_iso(str(row["indexed_at"])),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
        )
    except (ValueError, DomainError) as exc:
        raise KnowledgeIndexCorrupt(f"stored knowledge state is not readable: {exc}") from exc


def _row_to_hit(row: sqlite3.Row, rank: float, query: str) -> KnowledgeSearchHit:
    try:
        logical_uri = StorageUri.parse(str(row["logical_uri"]))
        return KnowledgeSearchHit(
            entry_id=UUID(str(row["entry_id"])),
            chunk_id=UUID(str(row["chunk_id"])),
            ordinal=int(row["ordinal"]),
            logical_uri=logical_uri,
            name=logical_uri.name,
            snippet=excerpt(str(row["content"]), query),
            source_span=_span_from_row(row),
            rank=rank,
            content=str(row["content"]),
        )
    except (ValueError, DomainError) as exc:
        raise KnowledgeIndexCorrupt(f"stored knowledge chunk is not readable: {exc}") from exc


def _span_from_row(row: sqlite3.Row) -> SourceSpan:
    if row["page_number"] is not None:
        return SourceSpan.page(int(row["page_number"]))
    return SourceSpan.lines(int(row["line_start"]), int(row["line_end"]))


def _truncate_error(error: str) -> str:
    collapsed = " ".join(error.split())
    if len(collapsed) <= MAX_STORED_ERROR_CHARS:
        return collapsed
    return collapsed[: MAX_STORED_ERROR_CHARS - 3] + "..."


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "MAX_STORED_ERROR_CHARS",
    "IndexLocationResolver",
    "SearchCapabilities",
    "SqliteKnowledgeIndexFactory",
    "check_fts5_support",
    "sqlite_search_capabilities",
]
