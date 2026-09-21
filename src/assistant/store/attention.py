"""SQLite implementation of the attention repository (ADR-0042).

Every blocking `sqlite3` call lives in a private `_*_sync` method and is reached through
`asyncio.to_thread`; the connection is created and closed inside the worker thread that runs the
SQL, exactly as ADR-0009 requires. The unique index on live dedupe keys is the idempotency
guarantee, so a projector that runs twice writes one row rather than two.
"""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.attention import (
    AttentionItem,
    AttentionItemId,
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
    AttentionStatus,
)
from assistant.domain.errors import AttentionItemNotFound
from assistant.store.db import Database, transaction
from assistant.store.errors import StoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

ATTENTION_FIELDS = (
    "id, kind, source_type, source_id, dedupe_key, source_fingerprint, generation, status, "
    "severity, title, summary, created_at, updated_at, acknowledged_at, dismissed_at, resolved_at"
)

_SEVERITY_ORDER = (
    "CASE severity WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at, id"
)


class AttentionStoreError(StoreError):
    """A stored attention row could not be written."""


class SqliteAttentionRepository:
    """Attention items, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_item(self, item: AttentionItem) -> AttentionItem:
        return await asyncio.to_thread(self._add_sync, item)

    async def update_item(self, item: AttentionItem) -> AttentionItem:
        return await asyncio.to_thread(self._update_sync, item)

    async def get_item(self, item_id: AttentionItemId) -> AttentionItem | None:
        return await asyncio.to_thread(self._get_sync, item_id)

    async def find_live_by_dedupe_key(self, dedupe_key: str) -> AttentionItem | None:
        return await asyncio.to_thread(self._find_live_sync, dedupe_key)

    async def list_items(
        self,
        *,
        statuses: tuple[AttentionStatus, ...] | None = None,
        limit: int = 100,
    ) -> list[AttentionItem]:
        return await asyncio.to_thread(self._list_sync, statuses, limit)

    # ------------------------------------------------------------------ blocking internals

    def _add_sync(self, item: AttentionItem) -> AttentionItem:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO attention_items ({ATTENTION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _values(item),
                )
        except sqlite3.IntegrityError as exc:
            raise AttentionStoreError(
                f"could not store attention item {item.dedupe_key!r}: {exc}"
            ) from exc
        return item

    def _update_sync(self, item: AttentionItem) -> AttentionItem:
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE attention_items SET kind = ?, source_type = ?, source_id = ?, "
                    "dedupe_key = ?, source_fingerprint = ?, generation = ?, status = ?, "
                    "severity = ?, title = ?, summary = ?, created_at = ?, updated_at = ?, "
                    "acknowledged_at = ?, dismissed_at = ?, resolved_at = ? WHERE id = ?",
                    (*_values(item)[1:], str(item.id)),
                )
                if cursor.rowcount != 1:
                    raise AttentionItemNotFound(item.id)
        except sqlite3.IntegrityError as exc:
            raise AttentionStoreError(
                f"could not update attention item {item.id}: {exc}"
            ) from exc
        return item

    def _get_sync(self, item_id: AttentionItemId) -> AttentionItem | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {ATTENTION_FIELDS} FROM attention_items WHERE id = ?",
                (str(item_id),),
            ).fetchone()
        return None if row is None else row_to_attention(row)

    def _find_live_sync(self, dedupe_key: str) -> AttentionItem | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {ATTENTION_FIELDS} FROM attention_items "
                "WHERE dedupe_key = ? AND status <> 'resolved' ORDER BY generation DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        return None if row is None else row_to_attention(row)

    def _list_sync(
        self, statuses: tuple[AttentionStatus, ...] | None, limit: int
    ) -> list[AttentionItem]:
        query = f"SELECT {ATTENTION_FIELDS} FROM attention_items"
        parameters: tuple[object, ...] = ()
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(status.value for status in statuses)
        query += f" ORDER BY {_SEVERITY_ORDER} LIMIT ?"
        parameters = (*parameters, max(1, limit))
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [row_to_attention(row) for row in rows]


def _values(item: AttentionItem) -> tuple[object, ...]:
    return (
        str(item.id),
        item.kind.value,
        item.source_type.value,
        item.source_id,
        item.dedupe_key,
        item.fingerprint,
        item.generation,
        item.status.value,
        item.severity.value,
        item.title,
        item.summary,
        to_utc_iso(item.created_at),
        to_utc_iso(item.updated_at),
        None if item.acknowledged_at is None else to_utc_iso(item.acknowledged_at),
        None if item.dismissed_at is None else to_utc_iso(item.dismissed_at),
        None if item.resolved_at is None else to_utc_iso(item.resolved_at),
    )


def row_to_attention(row: sqlite3.Row | tuple[object, ...]) -> AttentionItem:
    """Rebuild one attention item from its row."""
    return AttentionItem(
        id=UUID(str(row[0])),
        kind=AttentionKind(str(row[1])),
        source_type=AttentionSourceType(str(row[2])),
        source_id=str(row[3]),
        dedupe_key=str(row[4]),
        fingerprint=str(row[5]),
        generation=int(str(row[6])),
        status=AttentionStatus(str(row[7])),
        severity=AttentionSeverity(str(row[8])),
        title=str(row[9]),
        summary=None if row[10] is None else str(row[10]),
        created_at=from_utc_iso(str(row[11])),
        updated_at=from_utc_iso(str(row[12])),
        acknowledged_at=None if row[13] is None else from_utc_iso(str(row[13])),
        dismissed_at=None if row[14] is None else from_utc_iso(str(row[14])),
        resolved_at=None if row[15] is None else from_utc_iso(str(row[15])),
    )


__all__ = [
    "ATTENTION_FIELDS",
    "AttentionStoreError",
    "SqliteAttentionRepository",
    "row_to_attention",
]
