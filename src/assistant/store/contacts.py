"""SQLite implementation of the contact repository (ADR-0037 §9)."""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.contact import Contact, ContactId, ContactStatus
from assistant.domain.errors import ContactNotFound
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

CONTACT_FIELDS = (
    "id, display_name, email_address, email_key, status, contact_fingerprint, "
    "created_at, updated_at, retired_at"
)


class SqliteContactRepository:
    """Contacts, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_contact(self, contact: Contact) -> Contact:
        return await asyncio.to_thread(self._add_sync, contact)

    async def update_contact(self, contact: Contact) -> Contact:
        return await asyncio.to_thread(self._update_sync, contact)

    async def get_contact(self, contact_id: ContactId) -> Contact | None:
        return await asyncio.to_thread(self._get_sync, contact_id)

    async def find_active_by_fingerprint(self, fingerprint: str) -> Contact | None:
        return await asyncio.to_thread(self._find_active_sync, fingerprint)

    async def list_contacts(
        self, *, status: ContactStatus | None = ContactStatus.ACTIVE
    ) -> list[Contact]:
        return await asyncio.to_thread(self._list_sync, status)

    # ------------------------------------------------------------------ blocking internals

    def _add_sync(self, contact: Contact) -> Contact:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO contacts ({CONTACT_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _values(contact),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not store a contact: {exc}"
            ) from exc
        return contact

    def _update_sync(self, contact: Contact) -> Contact:
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE contacts SET display_name = ?, email_address = ?, email_key = ?, "
                    "status = ?, contact_fingerprint = ?, updated_at = ?, retired_at = ? "
                    "WHERE id = ?",
                    (
                        contact.display_name,
                        contact.email_address,
                        contact.email_key,
                        contact.status.value,
                        contact.fingerprint,
                        to_utc_iso(contact.updated_at),
                        None if contact.retired_at is None else to_utc_iso(contact.retired_at),
                        str(contact.id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ContactNotFound(contact.id)
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not update a contact: {exc}"
            ) from exc
        return contact

    def _get_sync(self, contact_id: ContactId) -> Contact | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {CONTACT_FIELDS} FROM contacts WHERE id = ?", (str(contact_id),)
            ).fetchone()
        return None if row is None else row_to_contact(row)

    def _find_active_sync(self, fingerprint: str) -> Contact | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {CONTACT_FIELDS} FROM contacts "
                "WHERE contact_fingerprint = ? AND status = ? ORDER BY created_at LIMIT 1",
                (fingerprint, ContactStatus.ACTIVE.value),
            ).fetchone()
        return None if row is None else row_to_contact(row)

    def _list_sync(self, status: ContactStatus | None) -> list[Contact]:
        query = f"SELECT {CONTACT_FIELDS} FROM contacts ORDER BY created_at, rowid"
        parameters: tuple[object, ...] = ()
        if status is not None:
            query = (
                f"SELECT {CONTACT_FIELDS} FROM contacts WHERE status = ? "
                "ORDER BY created_at, rowid"
            )
            parameters = (status.value,)
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [row_to_contact(row) for row in rows]


def _values(contact: Contact) -> tuple[object, ...]:
    return (
        str(contact.id),
        contact.display_name,
        contact.email_address,
        contact.email_key,
        contact.status.value,
        contact.fingerprint,
        to_utc_iso(contact.created_at),
        to_utc_iso(contact.updated_at),
        None if contact.retired_at is None else to_utc_iso(contact.retired_at),
    )


def row_to_contact(row: sqlite3.Row | tuple[object, ...]) -> Contact:
    """Rebuild one contact from its row."""
    return Contact(
        id=UUID(str(row[0])),
        display_name=str(row[1]),
        email_address=str(row[2]),
        status=ContactStatus(str(row[4])),
        created_at=from_utc_iso(str(row[6])),
        updated_at=from_utc_iso(str(row[7])),
        retired_at=None if row[8] is None else from_utc_iso(str(row[8])),
    )


__all__ = ["CONTACT_FIELDS", "SqliteContactRepository", "row_to_contact"]
