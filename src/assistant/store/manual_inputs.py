"""SQLite implementation of the ManualInputRepository port (ADR-0009, ADR-0029).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

The rows are write-once and never edited: text a person pasted is evidence of what they pasted.
The bridge row follows the same idempotent pattern as mail and web observations
(`ON CONFLICT DO NOTHING`), so re-running an ingest after a crash repairs a missing link instead of
producing a second one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    ManualInputNotFound,
)
from assistant.domain.inbound_event import EventId
from assistant.domain.manual_input import (
    ManualInput,
    ManualInputId,
    ManualInputSource,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_INPUT_FIELDS = "id, source, text, content_sha256, created_at"


class SqliteManualInputRepository:
    """Durable manual input and its links to the event inbox."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_input(self, manual_input: ManualInput) -> ManualInput:
        return await asyncio.to_thread(self._add_input_sync, manual_input)

    async def get_input(self, input_id: ManualInputId) -> ManualInput | None:
        return await asyncio.to_thread(self._get_input_sync, input_id)

    async def list_inputs(self, *, limit: int | None = 20) -> list[ManualInput]:
        return await asyncio.to_thread(self._list_inputs_sync, limit)

    async def resolve_input_id(self, reference: str) -> ManualInputId:
        inputs = await self.list_inputs(limit=None)
        return _resolve_id(
            reference, [item.id for item in inputs], ManualInputNotFound(reference)
        )

    async def link_event(
        self,
        input_id: ManualInputId,
        inbound_event_id: EventId,
        *,
        linked_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._link_event_sync, input_id, inbound_event_id, linked_at
        )

    async def get_linked_event_id(self, input_id: ManualInputId) -> EventId | None:
        return await asyncio.to_thread(self._get_linked_event_id_sync, input_id)

    async def list_unlinked_inputs(self, *, limit: int) -> list[ManualInput]:
        return await asyncio.to_thread(self._list_unlinked_sync, limit)

    # ------------------------------------------------------------ blocking internals

    def _add_input_sync(self, manual_input: ManualInput) -> ManualInput:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO manual_inputs ({_INPUT_FIELDS}) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(manual_input.id),
                        manual_input.source.value,
                        manual_input.text,
                        manual_input.content_sha256,
                        to_utc_iso(manual_input.created_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store the manual input: {exc}") from exc
        return manual_input

    def _get_input_sync(self, input_id: ManualInputId) -> ManualInput | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_INPUT_FIELDS} FROM manual_inputs WHERE id = ?",
                (str(input_id),),
            ).fetchone()
        return None if row is None else _row_to_input(row)

    def _list_inputs_sync(self, limit: int | None) -> list[ManualInput]:
        statement = f"SELECT {_INPUT_FIELDS} FROM manual_inputs ORDER BY created_at DESC, id DESC"
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_input(row) for row in rows]

    def _link_event_sync(
        self, input_id: ManualInputId, inbound_event_id: EventId, linked_at: datetime
    ) -> None:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO manual_input_event_links "
                "(manual_input_id, inbound_event_id, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT (manual_input_id) DO NOTHING",
                (str(input_id), str(inbound_event_id), to_utc_iso(linked_at)),
            )

    def _get_linked_event_id_sync(self, input_id: ManualInputId) -> EventId | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT inbound_event_id FROM manual_input_event_links "
                "WHERE manual_input_id = ?",
                (str(input_id),),
            ).fetchone()
        return None if row is None else UUID(str(row["inbound_event_id"]))

    def _list_unlinked_sync(self, limit: int) -> list[ManualInput]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_INPUT_FIELDS} FROM manual_inputs AS m "
                "WHERE NOT EXISTS (SELECT 1 FROM manual_input_event_links AS l "
                "WHERE l.manual_input_id = m.id) "
                "ORDER BY m.created_at, m.id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_input(row) for row in rows]


def _resolve_id[T](reference: str, identifiers: list[UUID], missing: Exception) -> UUID:
    """Resolve a full UUID or a unique prefix against a list of identities."""
    text = reference.strip().lower()
    if not text:
        raise missing
    try:
        candidate = UUID(text)
    except ValueError:
        candidate = None
    if candidate is not None:
        if candidate not in identifiers:
            raise missing
        return candidate
    matching = [item for item in identifiers if str(item).startswith(text)]
    if not matching:
        raise missing
    if len(matching) > 1:
        raise AmbiguousId(reference, len(matching))
    return matching[0]


def _row_to_input(row: sqlite3.Row) -> ManualInput:
    return ManualInput(
        id=UUID(str(row["id"])),
        source=ManualInputSource(str(row["source"])),
        text=str(row["text"]),
        content_sha256=str(row["content_sha256"]),
        created_at=from_utc_iso(str(row["created_at"])),
    )


__all__ = ["SqliteManualInputRepository"]
