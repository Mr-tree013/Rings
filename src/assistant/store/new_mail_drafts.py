"""SQLite implementation of the new-mail draft repository (ADR-0037 §17)."""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    NewMailDraftNotFound,
    StaleMailDraftUpdate,
)
from assistant.domain.mail_draft import MailDraftOrigin
from assistant.domain.new_mail_draft import NewMailDraft, NewMailDraftId
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

DRAFT_FIELDS = (
    "id, account_id, to_address, subject, body_text, origin, version, created_at, updated_at"
)


class SqliteNewMailDraftRepository:
    """New-mail drafts, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_draft(self, draft: NewMailDraft) -> NewMailDraft:
        return await asyncio.to_thread(self._add_sync, draft)

    async def get_draft(self, draft_id: NewMailDraftId) -> NewMailDraft | None:
        return await asyncio.to_thread(self._get_sync, draft_id)

    async def list_drafts(self, *, limit: int | None = 20) -> list[NewMailDraft]:
        return await asyncio.to_thread(self._list_sync, limit)

    async def update_draft(
        self, draft: NewMailDraft, *, expected_version: int
    ) -> NewMailDraft:
        return await asyncio.to_thread(self._update_sync, draft, expected_version)

    async def resolve_draft_id(self, reference: str) -> NewMailDraftId:
        return await asyncio.to_thread(self._resolve_sync, reference)

    # ------------------------------------------------------------------ blocking internals

    def _add_sync(self, draft: NewMailDraft) -> NewMailDraft:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO new_mail_drafts ({DRAFT_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _values(draft),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not store a new mail draft: {exc}"
            ) from exc
        return draft

    def _get_sync(self, draft_id: NewMailDraftId) -> NewMailDraft | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {DRAFT_FIELDS} FROM new_mail_drafts WHERE id = ?", (str(draft_id),)
            ).fetchone()
        return None if row is None else row_to_draft(row)

    def _list_sync(self, limit: int | None) -> list[NewMailDraft]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = (
            f"SELECT {DRAFT_FIELDS} FROM new_mail_drafts ORDER BY updated_at DESC, id"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [row_to_draft(row) for row in rows]

    def _update_sync(
        self, draft: NewMailDraft, expected_version: int
    ) -> NewMailDraft:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                "SELECT version FROM new_mail_drafts WHERE id = ?", (str(draft.id),)
            ).fetchone()
            if row is None:
                raise NewMailDraftNotFound(draft.id)
            if int(row["version"]) != expected_version:
                raise StaleMailDraftUpdate(draft.id, expected_version, int(row["version"]))
            connection.execute(
                "UPDATE new_mail_drafts SET account_id = ?, to_address = ?, subject = ?, "
                "body_text = ?, origin = ?, version = ?, updated_at = ? WHERE id = ?",
                (
                    draft.account_id,
                    draft.to_address,
                    draft.subject,
                    draft.body_text,
                    draft.origin.value,
                    draft.version,
                    to_utc_iso(draft.updated_at),
                    str(draft.id),
                ),
            )
        return draft

    def _resolve_sync(self, reference: str) -> NewMailDraftId:
        cleaned = reference.strip().lower()
        if not cleaned:
            raise NewMailDraftNotFound(reference)
        with self._database.connect() as connection:
            rows = connection.execute("SELECT id FROM new_mail_drafts").fetchall()
        matches = [str(row["id"]) for row in rows if str(row["id"]).startswith(cleaned)]
        if not matches:
            raise NewMailDraftNotFound(reference)
        if len(matches) > 1:
            raise AmbiguousId(reference, len(matches))
        return UUID(matches[0])


def _values(draft: NewMailDraft) -> tuple[object, ...]:
    return (
        str(draft.id),
        draft.account_id,
        draft.to_address,
        draft.subject,
        draft.body_text,
        draft.origin.value,
        draft.version,
        to_utc_iso(draft.created_at),
        to_utc_iso(draft.updated_at),
    )


def row_to_draft(row: sqlite3.Row | tuple[object, ...]) -> NewMailDraft:
    """Rebuild one new-mail draft from its row."""
    return NewMailDraft(
        id=UUID(str(row[0])),
        account_id=str(row[1]),
        to_address=str(row[2]),
        subject=str(row[3]),
        body_text=str(row[4]),
        origin=MailDraftOrigin(str(row[5])),
        version=int(str(row[6])),
        created_at=from_utc_iso(str(row[7])),
        updated_at=from_utc_iso(str(row[8])),
    )


__all__ = ["DRAFT_FIELDS", "SqliteNewMailDraftRepository", "row_to_draft"]
