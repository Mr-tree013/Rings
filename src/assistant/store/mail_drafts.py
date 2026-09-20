"""SQLite implementation of the durable reply drafts (ADR-0009, ADR-0022).

Every public method is an async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

Two behaviours are the reason this store exists rather than a generic upsert:

- **a draft and its sources are one transaction.** A body whose provenance was lost halfway would
  be a claim nobody can check;
- **an edit is a compare-and-set.** `UPDATE … WHERE id = ? AND version = ?` is the whole
  concurrency story: a second writer's stale version matches no row, and the caller is told
  rather than silently overwriting the first writer's work.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from typing import NoReturn
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    MailDraftNotFound,
    StaleMailDraftUpdate,
)
from assistant.domain.mail_draft import (
    MailDraft,
    MailDraftId,
    MailDraftOrigin,
    MailDraftSource,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_DRAFT_FIELDS = (
    "id, account_id, thread_id, reply_to_message_id, to_addresses_json, subject, body_text, "
    "needs_user_input_json, origin, version, generation_input_fingerprint, prompt_version, "
    "created_at, updated_at"
)

_SOURCE_FIELDS = "draft_id, ordinal, root_id, entry_id, chunk_id, logical_uri, source_span_json"


class SqliteMailDraftRepository:
    """Durable reply drafts, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_draft(
        self, draft: MailDraft, sources: Sequence[MailDraftSource] = ()
    ) -> MailDraft:
        return await asyncio.to_thread(self._add_draft_sync, draft, tuple(sources))

    async def get_draft(self, draft_id: MailDraftId) -> MailDraft | None:
        return await asyncio.to_thread(self._get_draft_sync, draft_id)

    async def list_drafts(self, *, limit: int | None = 20) -> list[MailDraft]:
        return await asyncio.to_thread(self._list_drafts_sync, limit)

    async def list_sources(self, draft_id: MailDraftId) -> list[MailDraftSource]:
        return await asyncio.to_thread(self._list_sources_sync, draft_id)

    async def count_sources(self, draft_id: MailDraftId) -> int:
        return await asyncio.to_thread(self._count_sources_sync, draft_id)

    async def update_draft(self, draft: MailDraft, *, expected_version: int) -> MailDraft:
        return await asyncio.to_thread(self._update_draft_sync, draft, expected_version)

    async def resolve_draft_id(self, reference: str) -> MailDraftId:
        text = reference.strip().lower()
        if not text:
            raise MailDraftNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        drafts = await asyncio.to_thread(self._list_draft_ids_sync)
        if candidate is not None:
            if candidate not in drafts:
                raise MailDraftNotFound(candidate)
            return candidate
        matching = [draft_id for draft_id in drafts if str(draft_id).startswith(text)]
        if not matching:
            raise MailDraftNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    async def count_drafts(self) -> int:
        return await asyncio.to_thread(self._count_drafts_sync)

    # ------------------------------------------------------------ blocking internals

    def _add_draft_sync(
        self, draft: MailDraft, sources: tuple[MailDraftSource, ...]
    ) -> MailDraft:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO mail_drafts ({_DRAFT_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _draft_parameters(draft),
                )
                for source in sources:
                    connection.execute(
                        f"INSERT INTO mail_draft_sources ({_SOURCE_FIELDS}) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(draft.id),
                            source.ordinal,
                            source.root_id,
                            str(source.entry_id),
                            str(source.chunk_id),
                            str(source.logical_uri),
                            json.dumps(
                                source.to_payload()["source_span"],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                row = connection.execute(
                    f"SELECT {_DRAFT_FIELDS} FROM mail_drafts WHERE id = ?", (str(draft.id),)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a mail draft: {exc}") from exc
        if row is None:  # pragma: no cover - the insert guarantees a row
            raise CommitmentStoreError(f"mail draft {draft.id} vanished after being stored")
        return _row_to_draft(row)

    def _get_draft_sync(self, draft_id: MailDraftId) -> MailDraft | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_DRAFT_FIELDS} FROM mail_drafts WHERE id = ?", (str(draft_id),)
            ).fetchone()
        return None if row is None else _row_to_draft(row)

    def _list_drafts_sync(self, limit: int | None) -> list[MailDraft]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = f"SELECT {_DRAFT_FIELDS} FROM mail_drafts ORDER BY updated_at DESC, id"
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_draft(row) for row in rows]

    def _list_draft_ids_sync(self) -> list[MailDraftId]:
        with self._database.connect() as connection:
            rows = connection.execute("SELECT id FROM mail_drafts ORDER BY id").fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _list_sources_sync(self, draft_id: MailDraftId) -> list[MailDraftSource]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_SOURCE_FIELDS} FROM mail_draft_sources WHERE draft_id = ? "
                "ORDER BY ordinal",
                (str(draft_id),),
            ).fetchall()
        return [_row_to_source(row) for row in rows]

    def _count_sources_sync(self, draft_id: MailDraftId) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM mail_draft_sources WHERE draft_id = ?",
                (str(draft_id),),
            ).fetchone()
        return int(row["total"])

    def _update_draft_sync(self, draft: MailDraft, expected_version: int) -> MailDraft:
        if expected_version < 1:
            raise ValueError("expected_version must be at least 1")
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE mail_drafts SET subject = ?, body_text = ?, "
                    "needs_user_input_json = ?, origin = ?, version = ?, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        draft.subject,
                        draft.body_text,
                        json.dumps(list(draft.needs_user_input), ensure_ascii=False),
                        str(draft.origin),
                        draft.version,
                        to_utc_iso(draft.updated_at),
                        str(draft.id),
                        expected_version,
                    ),
                )
                if cursor.rowcount == 0:
                    raise _stale_or_missing(connection, draft.id, expected_version)
                row = connection.execute(
                    f"SELECT {_DRAFT_FIELDS} FROM mail_drafts WHERE id = ?", (str(draft.id),)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not update a mail draft: {exc}") from exc
        if row is None:  # pragma: no cover - the update guarantees a row
            raise CommitmentStoreError(f"mail draft {draft.id} vanished after being updated")
        return _row_to_draft(row)

    def _count_drafts_sync(self) -> int:
        with self._database.connect() as connection:
            row = connection.execute("SELECT count(*) AS total FROM mail_drafts").fetchone()
        return int(row["total"])


def _stale_or_missing(
    connection: sqlite3.Connection, draft_id: MailDraftId, expected_version: int
) -> NoReturn:
    """Explain a failed compare-and-set: no such draft, or a newer version exists."""
    row = connection.execute(
        "SELECT version FROM mail_drafts WHERE id = ?", (str(draft_id),)
    ).fetchone()
    if row is None:
        raise MailDraftNotFound(draft_id)
    actual = int(row["version"])
    raise StaleMailDraftUpdate(draft_id, expected_version, actual)


def _draft_parameters(draft: MailDraft) -> tuple[object, ...]:
    return (
        str(draft.id),
        draft.account_id,
        None if draft.thread_id is None else str(draft.thread_id),
        str(draft.reply_to_message_id),
        json.dumps(list(draft.to_addresses), ensure_ascii=False),
        draft.subject,
        draft.body_text,
        json.dumps(list(draft.needs_user_input), ensure_ascii=False),
        str(draft.origin),
        draft.version,
        draft.generation_input_fingerprint,
        draft.prompt_version,
        to_utc_iso(draft.created_at),
        to_utc_iso(draft.updated_at),
    )


def _row_to_draft(row: sqlite3.Row) -> MailDraft:
    try:
        thread_id = row["thread_id"]
        return MailDraft(
            id=UUID(str(row["id"])),
            account_id=str(row["account_id"]),
            thread_id=None if thread_id is None else UUID(str(thread_id)),
            reply_to_message_id=UUID(str(row["reply_to_message_id"])),
            to_addresses=_parse_json_list(row["to_addresses_json"], "recipients"),
            subject=str(row["subject"]),
            body_text=str(row["body_text"]),
            needs_user_input=_parse_json_list(row["needs_user_input_json"], "open questions"),
            origin=MailDraftOrigin(str(row["origin"])),
            version=int(row["version"]),
            generation_input_fingerprint=str(row["generation_input_fingerprint"]),
            prompt_version=int(row["prompt_version"]),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(f"stored mail draft is not readable: {exc}") from exc


def _row_to_source(row: sqlite3.Row) -> MailDraftSource:
    try:
        decoded = json.loads(str(row["source_span_json"]))
    except ValueError as exc:  # pragma: no cover - written by this store
        raise CommitmentStoreError(f"stored draft source span is not JSON: {exc}") from exc
    return MailDraftSource.from_payload(
        draft_id=UUID(str(row["draft_id"])),
        ordinal=int(row["ordinal"]),
        root_id=str(row["root_id"]),
        payload={
            "entry_id": str(row["entry_id"]),
            "chunk_id": str(row["chunk_id"]),
            "logical_uri": str(row["logical_uri"]),
            "source_span": decoded,
        },
    )


def _parse_json_list(raw: object, what: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(raw))
    except ValueError as exc:
        raise CommitmentStoreError(f"stored draft {what} is not JSON: {exc}") from exc
    if not isinstance(decoded, list):
        raise CommitmentStoreError(f"stored draft {what} is not a JSON array")
    return tuple(str(item) for item in decoded)


__all__ = ["SqliteMailDraftRepository"]
