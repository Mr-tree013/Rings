"""Knowledge index domain: extracted text, source spans and search results (ADR-0012).

The knowledge index is *derived* data: it holds text extracted from catalogued files plus a
strong SHA-256 of the original bytes, and it can always be rebuilt. Original files remain the
authority, and every piece of indexed text must be traceable back to a page or a line range
in the file it came from.

This module is pure: no I/O, no `os`, no `sqlite3`, no `pypdf`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from assistant.domain.catalog import CatalogEntry
from assistant.domain.errors import InvalidKnowledgeDocument, InvalidSourceSpan
from assistant.domain.storage import StorageUri

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_SNIPPET_CHARS = 300


class SourceSpanKind(StrEnum):
    """How a chunk maps back to the original file."""

    LINE = "line"
    PAGE = "page"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A page number, or an inclusive 1-based line range — never both, never neither."""

    kind: SourceSpanKind
    line_start: int | None = None
    line_end: int | None = None
    page_number: int | None = None

    def __post_init__(self) -> None:
        if self.kind is SourceSpanKind.LINE:
            if self.page_number is not None:
                raise InvalidSourceSpan("a line span must not carry a page number")
            if self.line_start is None or self.line_end is None:
                raise InvalidSourceSpan("a line span needs both line_start and line_end")
            if self.line_start < 1:
                raise InvalidSourceSpan("line_start must be at least 1")
            if self.line_end < self.line_start:
                raise InvalidSourceSpan("line_end must not precede line_start")
        else:
            if self.page_number is None:
                raise InvalidSourceSpan("a page span needs a page number")
            if self.page_number < 1:
                raise InvalidSourceSpan("page_number must be at least 1")
            if self.line_start is not None or self.line_end is not None:
                raise InvalidSourceSpan("a page span must not carry line numbers")

    @classmethod
    def lines(cls, start: int, end: int) -> SourceSpan:
        """Build an inclusive line span."""
        return cls(kind=SourceSpanKind.LINE, line_start=start, line_end=end)

    @classmethod
    def page(cls, number: int) -> SourceSpan:
        """Build a page span."""
        return cls(kind=SourceSpanKind.PAGE, page_number=number)

    def describe(self) -> str:
        """Human-readable location, e.g. `page 3` or `lines 18-31`."""
        if self.kind is SourceSpanKind.PAGE:
            return f"page {self.page_number}"
        return f"lines {self.line_start}-{self.line_end}"


@dataclass(frozen=True, slots=True)
class ExtractedChunk:
    """One searchable piece of a document, with its location in the original file."""

    ordinal: int
    content: str
    source_span: SourceSpan

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise InvalidKnowledgeDocument("chunk ordinal must not be negative")
        if not self.content.strip():
            raise InvalidKnowledgeDocument("chunk content must not be blank")


@dataclass(frozen=True, slots=True)
class ExtractorInfo:
    """Identity of the extractor that produced a document's text."""

    name: str
    version: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidKnowledgeDocument("extractor name must not be blank")
        if self.version < 1:
            raise InvalidKnowledgeDocument("extractor version must be at least 1")


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """The result of reading one supported file."""

    content_sha256: str
    extractor: ExtractorInfo
    chunks: tuple[ExtractedChunk, ...]

    def __post_init__(self) -> None:
        if not SHA256_PATTERN.match(self.content_sha256):
            raise InvalidKnowledgeDocument(
                "content_sha256 must be 64 lowercase hex characters"
            )
        ordinals = [chunk.ordinal for chunk in self.chunks]
        if ordinals != list(range(len(self.chunks))):
            raise InvalidKnowledgeDocument("chunk ordinals must be contiguous from 0")


class KnowledgeIndexStatus(StrEnum):
    """Outcome of the last indexing attempt for one catalog entry."""

    INDEXED = "indexed"
    EMPTY = "empty"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentState:
    """What the index knows about one catalog entry."""

    entry_id: UUID
    logical_uri: StorageUri
    relative_path: str
    size_bytes: int
    mtime_ns: int
    status: KnowledgeIndexStatus
    last_attempted_at: datetime
    content_sha256: str | None = None
    extractor: ExtractorInfo | None = None
    indexed_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if self.logical_uri.relative_path != self.relative_path:
            raise InvalidKnowledgeDocument(
                "logical URI and relative path must describe the same document"
            )
        if self.size_bytes < 0 or self.mtime_ns < 0:
            raise InvalidKnowledgeDocument("size and mtime must not be negative")
        for value, field_name in (
            (self.last_attempted_at, "last_attempted_at"),
            (self.indexed_at, "indexed_at"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise InvalidKnowledgeDocument(f"{field_name} must be timezone-aware")
        if self.status in {KnowledgeIndexStatus.INDEXED, KnowledgeIndexStatus.EMPTY}:
            if self.content_sha256 is None or not SHA256_PATTERN.match(self.content_sha256):
                raise InvalidKnowledgeDocument(f"{self.status} requires a content hash")
            if self.extractor is None:
                raise InvalidKnowledgeDocument(f"{self.status} requires an extractor")
            if self.indexed_at is None:
                raise InvalidKnowledgeDocument(f"{self.status} requires indexed_at")
            if self.last_error is not None:
                raise InvalidKnowledgeDocument(f"{self.status} must not carry last_error")
        elif self.status is KnowledgeIndexStatus.ERROR:
            if self.last_error is None or not self.last_error.strip():
                raise InvalidKnowledgeDocument("an ERROR state must carry last_error")
        else:
            if self.content_sha256 is not None:
                raise InvalidKnowledgeDocument("an UNSUPPORTED state must not carry a hash")
            if self.indexed_at is not None:
                raise InvalidKnowledgeDocument("an UNSUPPORTED state must not be indexed")


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    """One content hit inside one root's index.

    `snippet` is the short excerpt the search CLI shows; `content` is the indexed chunk itself,
    which is what a source-grounded answer must quote from. Both come from the same row: the
    index never has to re-read the original file to explain itself.
    """

    entry_id: UUID
    chunk_id: UUID
    ordinal: int
    logical_uri: StorageUri
    name: str
    snippet: str
    source_span: SourceSpan
    rank: float
    content: str = ""


@dataclass(frozen=True, slots=True)
class MergedKnowledgeHit:
    """A content hit with its cross-root fusion score."""

    hit: KnowledgeSearchHit
    score: float


@dataclass(frozen=True, slots=True)
class KnowledgeContextHit:
    """A content hit with the root it came from and its full indexed text."""

    root_id: str
    entry_id: UUID
    chunk_id: UUID
    ordinal: int
    logical_uri: StorageUri
    source_span: SourceSpan
    content: str
    score: float

    def __post_init__(self) -> None:
        if not self.root_id.strip():
            raise InvalidKnowledgeDocument("a context hit needs a root id")
        if not self.content.strip():
            raise InvalidKnowledgeDocument("a context hit needs indexed content")


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """Everything one search produced, clearly separated by confidence."""

    query: str
    content_hits: tuple[MergedKnowledgeHit, ...]
    metadata_hits: tuple[CatalogEntry, ...]
    offline_roots: tuple[str, ...]
    identity_mismatches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeContextSearchResult:
    """A search whose content hits carry their indexed text, for answer grounding.

    `metadata_hits` are file-name matches with no readable text; they are useful as a hint and
    are never evidence for what a document says. `offline_roots` names roots whose content could
    not be searched at all.
    """

    query: str
    content_hits: tuple[KnowledgeContextHit, ...]
    metadata_hits: tuple[CatalogEntry, ...]
    offline_roots: tuple[str, ...]
    identity_mismatches: tuple[str, ...]
    derived_queries: tuple[str, ...] = ()
    """The queries actually issued, in order: the question itself, then any keyword fallback."""


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRunResult:
    """Counters for one `index_root` run."""

    root_id: str
    seen: int
    indexed: int
    empty: int
    unsupported: int
    skipped_unchanged: int
    errors: int
    started_at: datetime
    finished_at: datetime


__all__ = [
    "DEFAULT_SNIPPET_CHARS",
    "SHA256_PATTERN",
    "ExtractedChunk",
    "ExtractedDocument",
    "ExtractorInfo",
    "KnowledgeContextHit",
    "KnowledgeContextSearchResult",
    "KnowledgeDocumentState",
    "KnowledgeIndexRunResult",
    "KnowledgeIndexStatus",
    "KnowledgeSearchHit",
    "KnowledgeSearchResult",
    "MergedKnowledgeHit",
    "SourceSpan",
    "SourceSpanKind",
]
