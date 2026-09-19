"""SQLite implementation of the `CatalogRepository` port (ADR-0009, ADR-0011).

Structure: async public methods delegate to blocking `_*_sync` helpers through
`asyncio.to_thread`, and each helper opens its own connection inside that worker thread.

`apply_snapshot` applies a whole snapshot — root registration, entry upserts and, only for
a complete snapshot, missing detection — inside one `BEGIN IMMEDIATE` transaction. A scan
therefore either lands completely or not at all, and an incomplete scan can never mark
anything missing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.catalog import (
    CatalogEntry,
    CatalogPresence,
    CatalogRoot,
    CatalogScanResult,
    FileSnapshotEntry,
    FilesystemSnapshot,
)
from assistant.domain.errors import StorageRootConflict
from assistant.domain.storage import StorageKind, StorageRoot, StorageUri
from assistant.store.db import Database, transaction
from assistant.store.errors import CatalogStoreError
from assistant.store.search_text import like_pattern
from assistant.store.serialization import from_utc_iso, to_utc_iso

_ENTRY_FIELDS = (
    "id, root_id, relative_path, name, suffix, size_bytes, mtime_ns, media_type, "
    "presence, first_seen_at, last_seen_at, metadata_updated_at"
)

# Entries carry their storage kind from the root row: kind is a property of the root, and
# it is that join that keeps a catalog entry's logical URI reconstructible without any
# physical path.
_ENTRY_SELECT = (
    "e.id, e.root_id, r.kind AS storage_kind, e.relative_path, e.name, e.suffix, "
    "e.size_bytes, e.mtime_ns, e.media_type, e.presence, e.first_seen_at, e.last_seen_at, "
    "e.metadata_updated_at"
)
_ENTRY_FROM = "FROM catalog_entries e JOIN storage_roots r ON r.root_id = e.root_id"

_ROOT_FIELDS = (
    "root_id, kind, label, last_known_path, first_seen_at, last_seen_at, last_scanned_at"
)

_SELECT_ROOT_SQL = f"SELECT {_ROOT_FIELDS} FROM storage_roots WHERE root_id = ?"

_SELECT_ROOTS_SQL = f"SELECT {_ROOT_FIELDS} FROM storage_roots ORDER BY root_id"

_INSERT_ROOT_SQL = """
INSERT INTO storage_roots (
    root_id, kind, label, last_known_path, first_seen_at, last_seen_at, last_scanned_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_ROOT_SQL = """
UPDATE storage_roots
   SET label = ?, last_known_path = ?, last_seen_at = ?,
       last_scanned_at = COALESCE(?, last_scanned_at)
 WHERE root_id = ?
"""

_SELECT_ENTRIES_SQL = f"SELECT {_ENTRY_SELECT} {_ENTRY_FROM} WHERE e.root_id = ?"

_SELECT_ENTRY_SQL = (
    f"SELECT {_ENTRY_SELECT} {_ENTRY_FROM} WHERE e.root_id = ? AND e.relative_path = ?"
)

_INSERT_ENTRY_SQL = f"""
INSERT INTO catalog_entries ({_ENTRY_FIELDS}, last_seen_scan_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_ENTRY_METADATA_SQL = """
UPDATE catalog_entries
   SET name = ?, suffix = ?, size_bytes = ?, mtime_ns = ?, media_type = ?,
       last_seen_at = ?, metadata_updated_at = ?, last_seen_scan_id = ?, presence = ?
 WHERE id = ?
"""

_TOUCH_ENTRY_SQL = """
UPDATE catalog_entries
   SET last_seen_at = ?, last_seen_scan_id = ?, presence = ?
 WHERE id = ?
"""

_MARK_MISSING_SQL = """
UPDATE catalog_entries
   SET presence = ?, metadata_updated_at = ?
 WHERE root_id = ? AND presence = ? AND last_seen_scan_id != ?
"""


class SqliteCatalogRepository:
    """Durable metadata catalog backed by SQLite."""

    def __init__(
        self, database: Database, *, new_entry_id: Callable[[], UUID] = uuid4
    ) -> None:
        self._database = database
        self._new_entry_id = new_entry_id

    async def apply_snapshot(
        self,
        *,
        root: StorageRoot,
        physical_path: str,
        snapshot: FilesystemSnapshot,
        scan_id: UUID,
        scanned_at: datetime,
    ) -> CatalogScanResult:
        """Register/refresh `root` and apply one snapshot in a single transaction."""
        return await asyncio.to_thread(
            self._apply_snapshot_sync, root, physical_path, snapshot, scan_id, scanned_at
        )

    async def get_root(self, root_id: str) -> CatalogRoot | None:
        return await asyncio.to_thread(self._get_root_sync, root_id)

    async def get_by_uri(self, uri: StorageUri) -> CatalogEntry | None:
        return await asyncio.to_thread(
            self._get_by_location_sync, uri.kind, uri.root_id, uri.relative_path
        )

    async def get_by_location(
        self, *, storage_kind: StorageKind, root_id: str, relative_path: str
    ) -> CatalogEntry | None:
        return await asyncio.to_thread(
            self._get_by_location_sync, storage_kind, root_id, relative_path
        )

    async def list_by_root(
        self, root_id: str, *, include_missing: bool = False, limit: int | None = None
    ) -> list[CatalogEntry]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(
            self._list_by_root_sync, root_id, include_missing, limit
        )

    async def list_roots(self) -> list[CatalogRoot]:
        """List every known storage root, ordered by root id."""
        return await asyncio.to_thread(self._list_roots_sync)

    async def search_metadata(
        self,
        query: str,
        *,
        limit: int,
        root_id: str | None = None,
        include_missing: bool = False,
    ) -> list[CatalogEntry]:
        """Search file names and relative paths literally (no wildcards, no contents)."""
        if not query.strip():
            raise ValueError("query must not be blank")
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        return await asyncio.to_thread(
            self._search_metadata_sync, query, limit, root_id, include_missing
        )

    # ------------------------------------------------------------ blocking internals

    def _apply_snapshot_sync(
        self,
        root: StorageRoot,
        physical_path: str,
        snapshot: FilesystemSnapshot,
        scan_id: UUID,
        scanned_at: datetime,
    ) -> CatalogScanResult:
        scanned_at_iso = to_utc_iso(scanned_at)
        created = updated = unchanged = restored = marked_missing = 0
        try:
            with self._database.connect() as connection, transaction(connection):
                self._upsert_root(
                    connection, root, physical_path, scanned_at_iso, snapshot.complete
                )
                existing = {
                    str(row["relative_path"]): row
                    for row in connection.execute(_SELECT_ENTRIES_SQL, (root.root_id,))
                }
                for entry in snapshot.entries:
                    previous = existing.get(entry.relative_path)
                    if previous is None:
                        self._insert_entry(
                            connection, root, entry, scan_id, scanned_at_iso
                        )
                        created += 1
                        continue
                    was_missing = (
                        str(previous["presence"]) == CatalogPresence.MISSING.value
                    )
                    changed = _metadata_changed(previous, entry)
                    if was_missing:
                        restored += 1
                    elif changed:
                        updated += 1
                    else:
                        unchanged += 1
                    if changed:
                        connection.execute(
                            _UPDATE_ENTRY_METADATA_SQL,
                            (
                                entry.name,
                                entry.suffix,
                                entry.size_bytes,
                                entry.mtime_ns,
                                entry.media_type,
                                scanned_at_iso,
                                scanned_at_iso,
                                str(scan_id),
                                CatalogPresence.PRESENT.value,
                                str(previous["id"]),
                            ),
                        )
                    else:
                        connection.execute(
                            _TOUCH_ENTRY_SQL,
                            (
                                scanned_at_iso,
                                str(scan_id),
                                CatalogPresence.PRESENT.value,
                                str(previous["id"]),
                            ),
                        )
                if snapshot.complete:
                    cursor = connection.execute(
                        _MARK_MISSING_SQL,
                        (
                            CatalogPresence.MISSING.value,
                            scanned_at_iso,
                            root.root_id,
                            CatalogPresence.PRESENT.value,
                            str(scan_id),
                        ),
                    )
                    marked_missing = cursor.rowcount
        except sqlite3.Error as exc:
            raise CatalogStoreError(
                f"could not apply catalog snapshot for {root.root_id!r}: {exc}"
            ) from exc
        return CatalogScanResult(
            root_id=root.root_id,
            scan_id=scan_id,
            seen=len(snapshot.entries),
            created=created,
            updated=updated,
            unchanged=unchanged,
            restored=restored,
            marked_missing=marked_missing,
            scan_complete=snapshot.complete,
            errors=len(snapshot.errors),
        )

    def _upsert_root(
        self,
        connection: sqlite3.Connection,
        root: StorageRoot,
        physical_path: str,
        scanned_at_iso: str,
        complete: bool,
    ) -> None:
        row = connection.execute(_SELECT_ROOT_SQL, (root.root_id,)).fetchone()
        if row is None:
            connection.execute(
                _INSERT_ROOT_SQL,
                (
                    root.root_id,
                    root.kind.value,
                    root.label,
                    physical_path,
                    scanned_at_iso,
                    scanned_at_iso,
                    scanned_at_iso if complete else None,
                ),
            )
            return
        existing_kind = str(row["kind"])
        if existing_kind != root.kind.value:
            raise StorageRootConflict(
                f"storage root {root.root_id!r} is already registered as {existing_kind}; "
                f"it cannot become {root.kind.value}"
            )
        connection.execute(
            _UPDATE_ROOT_SQL,
            (
                root.label,
                physical_path,
                scanned_at_iso,
                scanned_at_iso if complete else None,
                root.root_id,
            ),
        )

    def _insert_entry(
        self,
        connection: sqlite3.Connection,
        root: StorageRoot,
        entry: FileSnapshotEntry,
        scan_id: UUID,
        scanned_at_iso: str,
    ) -> None:
        connection.execute(
            _INSERT_ENTRY_SQL,
            (
                str(self._new_entry_id()),
                root.root_id,
                entry.relative_path,
                entry.name,
                entry.suffix,
                entry.size_bytes,
                entry.mtime_ns,
                entry.media_type,
                CatalogPresence.PRESENT.value,
                scanned_at_iso,
                scanned_at_iso,
                scanned_at_iso,
                str(scan_id),
            ),
        )

    def _get_root_sync(self, root_id: str) -> CatalogRoot | None:
        with self._database.connect() as connection:
            row = connection.execute(_SELECT_ROOT_SQL, (root_id,)).fetchone()
        return None if row is None else _row_to_root(row)

    def _list_roots_sync(self) -> list[CatalogRoot]:
        with self._database.connect() as connection:
            rows = connection.execute(_SELECT_ROOTS_SQL).fetchall()
        return [_row_to_root(row) for row in rows]

    def _search_metadata_sync(
        self, query: str, limit: int, root_id: str | None, include_missing: bool
    ) -> list[CatalogEntry]:
        pattern = like_pattern(query)
        statement = (
            f"SELECT {_ENTRY_SELECT} {_ENTRY_FROM} "
            "WHERE (e.name LIKE ? ESCAPE '\\' OR e.relative_path LIKE ? ESCAPE '\\')"
        )
        parameters: list[object] = [pattern, pattern]
        if root_id is not None:
            statement += " AND e.root_id = ?"
            parameters.append(root_id)
        if not include_missing:
            statement += " AND e.presence = ?"
            parameters.append(CatalogPresence.PRESENT.value)
        statement += " ORDER BY e.relative_path LIMIT ?"
        parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_entry(row) for row in rows]

    def _get_by_location_sync(
        self, storage_kind: StorageKind, root_id: str, relative_path: str
    ) -> CatalogEntry | None:
        with self._database.connect() as connection:
            row = connection.execute(
                _SELECT_ENTRY_SQL, (root_id, relative_path)
            ).fetchone()
        if row is None:
            return None
        entry = _row_to_entry(row)
        if entry.storage_kind is not storage_kind:
            # The same root_id cannot change kind (enforced on write); a mismatch here
            # means the caller asked for something that is simply not in the catalog.
            return None
        return entry

    def _list_by_root_sync(
        self, root_id: str, include_missing: bool, limit: int | None
    ) -> list[CatalogEntry]:
        statement = f"SELECT {_ENTRY_SELECT} {_ENTRY_FROM} WHERE e.root_id = ?"
        parameters: list[object] = [root_id]
        if not include_missing:
            statement += " AND e.presence = ?"
            parameters.append(CatalogPresence.PRESENT.value)
        statement += " ORDER BY e.relative_path"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_entry(row) for row in rows]


def _metadata_changed(previous: sqlite3.Row, entry: FileSnapshotEntry) -> bool:
    return (
        int(previous["size_bytes"]) != entry.size_bytes
        or int(previous["mtime_ns"]) != entry.mtime_ns
        or str(previous["name"]) != entry.name
        or str(previous["suffix"]) != entry.suffix
        or previous["media_type"] != entry.media_type
    )


def _row_to_root(row: sqlite3.Row) -> CatalogRoot:
    return CatalogRoot(
        root=StorageRoot(
            root_id=str(row["root_id"]),
            kind=StorageKind(str(row["kind"])),
            label=str(row["label"]),
        ),
        last_known_path=str(row["last_known_path"]),
        first_seen_at=from_utc_iso(str(row["first_seen_at"])),
        last_seen_at=from_utc_iso(str(row["last_seen_at"])),
        last_scanned_at=(
            None if row["last_scanned_at"] is None else from_utc_iso(str(row["last_scanned_at"]))
        ),
    )


def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
    try:
        return CatalogEntry(
            id=UUID(str(row["id"])),
            root_id=str(row["root_id"]),
            storage_kind=StorageKind(str(row["storage_kind"])),
            relative_path=str(row["relative_path"]),
            name=str(row["name"]),
            suffix=str(row["suffix"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            media_type=None if row["media_type"] is None else str(row["media_type"]),
            presence=CatalogPresence(str(row["presence"])),
            first_seen_at=from_utc_iso(str(row["first_seen_at"])),
            last_seen_at=from_utc_iso(str(row["last_seen_at"])),
            metadata_updated_at=from_utc_iso(str(row["metadata_updated_at"])),
        )
    except (ValueError, sqlite3.Error) as exc:
        raise CatalogStoreError(f"stored catalog entry is not readable: {exc}") from exc


__all__ = ["SqliteCatalogRepository"]
