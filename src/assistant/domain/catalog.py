"""Catalog value objects: what a metadata scan saw, and what the catalog keeps (ADR-0011).

The catalog is *derived* metadata. The files themselves are the authority, so the whole
catalog can be rebuilt by scanning again; nothing here stores file contents, hashes,
summaries or embeddings.

`MISSING` has one precise meaning: a *complete* scan of a root did not see this path any
more. An unplugged drive is not `MISSING` — that is a runtime availability question, not a
catalog state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from assistant.domain.errors import InvalidCatalogEntry
from assistant.domain.storage import (
    StorageKind,
    StorageRoot,
    StorageUri,
    validate_relative_path,
)

CatalogEntryId = UUID
"""Stable identity of a catalog record: `(root_id, relative_path)` for its whole life."""


class CatalogPresence(StrEnum):
    """Whether a complete scan still sees this path.

    `MISSING` is not `OFFLINE`: a removable vault that is not plugged in keeps all its
    entries `PRESENT`, because no complete scan has disproved them.
    """

    PRESENT = "present"
    MISSING = "missing"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCatalogEntry(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FileSnapshotEntry:
    """One file seen by a metadata scan: filesystem facts only, never content."""

    relative_path: str
    name: str
    size_bytes: int
    mtime_ns: int
    media_type: str | None

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if not self.name.strip():
            raise InvalidCatalogEntry("snapshot entry name must not be blank")
        if self.size_bytes < 0:
            raise InvalidCatalogEntry("snapshot entry size must not be negative")
        if self.mtime_ns < 0:
            raise InvalidCatalogEntry("snapshot entry mtime_ns must not be negative")

    @property
    def suffix(self) -> str:
        """The final suffix of the file name, or an empty string."""
        name = self.name
        dot = name.rfind(".")
        return name[dot:] if dot > 0 else ""


@dataclass(frozen=True, slots=True)
class ScanError:
    """A bounded, storable description of one filesystem failure."""

    relative_path: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class FilesystemSnapshot:
    """Everything one metadata scan produced."""

    entries: tuple[FileSnapshotEntry, ...]
    complete: bool
    errors: tuple[ScanError, ...]
    skipped_symlinks: tuple[str, ...]
    started_at: datetime
    finished_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise InvalidCatalogEntry("a snapshot cannot finish before it started")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """The catalog's record of one path under one storage root."""

    id: CatalogEntryId
    root_id: str
    storage_kind: StorageKind
    relative_path: str
    name: str
    suffix: str
    size_bytes: int
    mtime_ns: int
    media_type: str | None
    presence: CatalogPresence
    first_seen_at: datetime
    last_seen_at: datetime
    metadata_updated_at: datetime

    def __post_init__(self) -> None:
        validate_relative_path(self.relative_path)
        if not self.name.strip():
            raise InvalidCatalogEntry("catalog entry name must not be blank")
        if self.size_bytes < 0 or self.mtime_ns < 0:
            raise InvalidCatalogEntry("catalog entry size and mtime must not be negative")
        if self.suffix and not self.suffix.startswith("."):
            raise InvalidCatalogEntry("catalog entry suffix must be empty or start with '.'")
        _require_aware(self.first_seen_at, "first_seen_at")
        _require_aware(self.last_seen_at, "last_seen_at")
        _require_aware(self.metadata_updated_at, "metadata_updated_at")

    @property
    def logical_uri(self) -> StorageUri:
        """The stable reference to this document, independent of any mount point."""
        return StorageUri(
            kind=self.storage_kind, root_id=self.root_id, relative_path=self.relative_path
        )

    @property
    def is_present(self) -> bool:
        """Whether the last complete scan saw this path."""
        return self.presence is CatalogPresence.PRESENT


@dataclass(frozen=True, slots=True)
class CatalogRoot:
    """A storage root plus the runtime metadata the catalog keeps about it."""

    root: StorageRoot
    last_known_path: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_scanned_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogScanResult:
    """What applying one snapshot did to the catalog.

    Every entry lands in exactly one of `created`, `restored`, `updated` or `unchanged`
    (in that order of precedence), so the four counters add up to `seen`.
    """

    root_id: str
    scan_id: UUID
    seen: int
    created: int
    updated: int
    unchanged: int
    restored: int
    marked_missing: int
    scan_complete: bool
    errors: int


__all__ = [
    "CatalogEntry",
    "CatalogEntryId",
    "CatalogPresence",
    "CatalogRoot",
    "CatalogScanResult",
    "FileSnapshotEntry",
    "FilesystemSnapshot",
    "ScanError",
]

