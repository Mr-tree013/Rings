"""SQLite implementation of the mail repository (ADR-0009, ADR-0020).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

`apply_fetched_batch` is the reason this store exists: one `BEGIN IMMEDIATE` transaction writes
the messages, their locations, their attachments **and** the mailbox cursor. A batch that fails
halfway leaves nothing behind — neither a message without its location nor a cursor that has
already skipped the mail the database never stored.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import AmbiguousId, DomainError, MailMessageNotFound
from assistant.domain.inbound_event import EventId
from assistant.domain.mail import (
    MailAccountId,
    MailAttachmentMetadata,
    MailBodyStatus,
    MailboxSyncMode,
    MailboxSyncState,
    MailMessage,
    MailMessageId,
    MailMessageLocation,
    ReconciliationCandidates,
)
from assistant.ports.mail_repository import BatchApplyResult, FetchedMail
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

MESSAGE_FIELDS = (
    "id, account_id, message_id_header, in_reply_to_header, references_json, subject, "
    "from_address, to_addresses_json, cc_addresses_json, date_header, sent_at, body_text, "
    "body_status, raw_sha256, content_fingerprint, raw_storage_key, size_bytes, parse_warnings, "
    "first_seen_at, last_seen_at"
)

LOCATION_FIELDS = (
    "message_id, account_id, mailbox_name, uidvalidity, uid, first_seen_at, last_seen_at"
)

ATTACHMENT_FIELDS = (
    "id, message_id, ordinal, filename, content_type, content_disposition, size_bytes, sha256"
)

STATE_FIELDS = (
    "account_id, mailbox_name, uidvalidity, last_seen_uid, mode, last_sync_at, "
    "last_reconciled_at, updated_at"
)


class SqliteMailRepository:
    """Durable inbound mail, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_sync_state(
        self, account_id: MailAccountId, mailbox_name: str
    ) -> MailboxSyncState | None:
        return await asyncio.to_thread(self._get_sync_state_sync, account_id, mailbox_name)

    async def apply_fetched_batch(
        self,
        *,
        account_id: MailAccountId,
        mailbox_name: str,
        uidvalidity: int,
        last_seen_uid: int,
        messages: Sequence[FetchedMail],
        mode: MailboxSyncMode,
        at: datetime,
        reconciled: bool = False,
    ) -> BatchApplyResult:
        return await asyncio.to_thread(
            self._apply_fetched_batch_sync,
            account_id,
            mailbox_name,
            uidvalidity,
            last_seen_uid,
            tuple(messages),
            mode,
            at,
            reconciled,
        )

    async def get_message(self, message_id: MailMessageId) -> MailMessage | None:
        return await asyncio.to_thread(self._get_message_sync, message_id)

    async def list_messages(
        self, *, account_id: MailAccountId | None = None, limit: int | None = 20
    ) -> list[MailMessage]:
        return await asyncio.to_thread(self._list_messages_sync, account_id, limit)

    async def list_locations(self, message_id: MailMessageId) -> list[MailMessageLocation]:
        return await asyncio.to_thread(self._list_locations_sync, message_id)

    async def list_attachments(
        self, message_id: MailMessageId
    ) -> list[MailAttachmentMetadata]:
        return await asyncio.to_thread(self._list_attachments_sync, message_id)

    async def list_unlinked_messages(self, *, limit: int = 100) -> list[MailMessage]:
        return await asyncio.to_thread(self._list_unlinked_messages_sync, limit)

    async def link_inbound_event(
        self,
        *,
        mail_message_id: MailMessageId,
        inbound_event_id: EventId,
        linked_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._link_inbound_event_sync, mail_message_id, inbound_event_id, linked_at
        )

    async def get_linked_event_id(self, message_id: MailMessageId) -> EventId | None:
        return await asyncio.to_thread(self._get_linked_event_id_sync, message_id)

    async def count_messages(self, *, account_id: MailAccountId | None = None) -> int:
        return await asyncio.to_thread(self._count_messages_sync, account_id)

    async def count_unlinked_messages(
        self, *, account_id: MailAccountId | None = None
    ) -> int:
        return await asyncio.to_thread(self._count_unlinked_messages_sync, account_id)

    async def find_reconciliation_candidates(
        self,
        *,
        account_id: MailAccountId,
        message_id_header: str | None,
        content_fingerprint: str,
        raw_sha256: str | None,
    ) -> ReconciliationCandidates:
        return await asyncio.to_thread(
            self._find_candidates_sync,
            account_id,
            message_id_header,
            content_fingerprint,
            raw_sha256,
        )

    async def resolve_message_id(self, reference: str) -> MailMessageId:
        text = reference.strip().lower()
        if not text:
            raise MailMessageNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        messages = await self.list_messages(limit=None)
        if candidate is not None:
            if all(message.id != candidate for message in messages):
                raise MailMessageNotFound(candidate)
            return candidate
        matching = [message.id for message in messages if str(message.id).startswith(text)]
        if not matching:
            raise MailMessageNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    # ------------------------------------------------------------ blocking internals

    def _get_sync_state_sync(
        self, account_id: MailAccountId, mailbox_name: str
    ) -> MailboxSyncState | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {STATE_FIELDS} FROM mailbox_sync_state "
                "WHERE account_id = ? AND mailbox_name = ?",
                (account_id, mailbox_name),
            ).fetchone()
        return None if row is None else _row_to_state(row)

    def _apply_fetched_batch_sync(
        self,
        account_id: MailAccountId,
        mailbox_name: str,
        uidvalidity: int,
        last_seen_uid: int,
        messages: tuple[FetchedMail, ...],
        mode: MailboxSyncMode,
        at: datetime,
        reconciled: bool,
    ) -> BatchApplyResult:
        created = matched = existing = 0
        try:
            with self._database.connect() as connection, transaction(connection):
                for fetched in messages:
                    if self._location_exists(connection, fetched.location):
                        existing += 1
                        continue
                    if self._message_exists(connection, fetched.message.id):
                        matched += 1
                        self._touch_message(connection, fetched.message, at=at)
                    else:
                        created += 1
                        self._insert_message(connection, fetched.message)
                        for attachment in fetched.attachments:
                            self._insert_attachment(connection, attachment)
                    self._insert_location(connection, fetched.location)
                self._upsert_sync_state(
                    connection,
                    account_id=account_id,
                    mailbox_name=mailbox_name,
                    uidvalidity=uidvalidity,
                    last_seen_uid=last_seen_uid,
                    mode=mode,
                    at=at,
                    reconciled=reconciled,
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a mail batch: {exc}") from exc
        return BatchApplyResult(
            created_messages=created,
            matched_messages=matched,
            existing_locations=existing,
        )

    def _location_exists(
        self, connection: sqlite3.Connection, location: MailMessageLocation
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM mail_message_locations "
            "WHERE account_id = ? AND mailbox_name = ? AND uidvalidity = ? AND uid = ?",
            location.identity,
        ).fetchone()
        return row is not None

    def _message_exists(self, connection: sqlite3.Connection, message_id: MailMessageId) -> bool:
        row = connection.execute(
            "SELECT 1 FROM mail_messages WHERE id = ?", (str(message_id),)
        ).fetchone()
        return row is not None

    def _insert_message(self, connection: sqlite3.Connection, message: MailMessage) -> None:
        connection.execute(
            f"INSERT INTO mail_messages ({MESSAGE_FIELDS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _message_parameters(message),
        )

    def _touch_message(
        self, connection: sqlite3.Connection, message: MailMessage, *, at: datetime
    ) -> None:
        connection.execute(
            "UPDATE mail_messages SET last_seen_at = ? WHERE id = ?",
            (to_utc_iso(at), str(message.id)),
        )

    def _insert_location(
        self, connection: sqlite3.Connection, location: MailMessageLocation
    ) -> None:
        connection.execute(
            f"INSERT INTO mail_message_locations ({LOCATION_FIELDS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(location.message_id),
                location.account_id,
                location.mailbox_name,
                location.uidvalidity,
                location.uid,
                to_utc_iso(location.first_seen_at),
                to_utc_iso(location.last_seen_at),
            ),
        )

    def _insert_attachment(
        self, connection: sqlite3.Connection, attachment: MailAttachmentMetadata
    ) -> None:
        connection.execute(
            f"INSERT INTO mail_attachments ({ATTACHMENT_FIELDS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(attachment.id),
                str(attachment.message_id),
                attachment.ordinal,
                attachment.filename,
                attachment.content_type,
                attachment.content_disposition,
                attachment.size_bytes,
                attachment.sha256,
            ),
        )

    def _upsert_sync_state(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: MailAccountId,
        mailbox_name: str,
        uidvalidity: int,
        last_seen_uid: int,
        mode: MailboxSyncMode,
        at: datetime,
        reconciled: bool,
    ) -> None:
        connection.execute(
            f"INSERT INTO mailbox_sync_state ({STATE_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (account_id, mailbox_name) DO UPDATE SET "
            "uidvalidity = excluded.uidvalidity, "
            "last_seen_uid = excluded.last_seen_uid, "
            "mode = excluded.mode, "
            "last_sync_at = excluded.last_sync_at, "
            "last_reconciled_at = COALESCE(excluded.last_reconciled_at, "
            "mailbox_sync_state.last_reconciled_at), "
            "updated_at = excluded.updated_at",
            (
                account_id,
                mailbox_name,
                uidvalidity,
                last_seen_uid,
                str(mode),
                to_utc_iso(at),
                to_utc_iso(at) if reconciled else None,
                to_utc_iso(at),
            ),
        )

    def _get_message_sync(self, message_id: MailMessageId) -> MailMessage | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {MESSAGE_FIELDS} FROM mail_messages WHERE id = ?", (str(message_id),)
            ).fetchone()
        return None if row is None else row_to_message(row)

    def _list_messages_sync(
        self, account_id: MailAccountId | None, limit: int | None
    ) -> list[MailMessage]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = (
            f"SELECT {MESSAGE_FIELDS} FROM mail_messages"
            # SQLite has no portable NULLS LAST, so the NULL case is spelled out explicitly.
        )
        parameters: list[object] = []
        if account_id is not None:
            statement += " WHERE account_id = ?"
            parameters.append(account_id)
        statement += " ORDER BY (sent_at IS NULL), sent_at DESC, first_seen_at DESC, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [row_to_message(row) for row in rows]

    def _list_locations_sync(self, message_id: MailMessageId) -> list[MailMessageLocation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {LOCATION_FIELDS} FROM mail_message_locations WHERE message_id = ? "
                "ORDER BY uidvalidity, uid",
                (str(message_id),),
            ).fetchall()
        return [_row_to_location(row) for row in rows]

    def _list_attachments_sync(
        self, message_id: MailMessageId
    ) -> list[MailAttachmentMetadata]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ATTACHMENT_FIELDS} FROM mail_attachments WHERE message_id = ? "
                "ORDER BY ordinal",
                (str(message_id),),
            ).fetchall()
        return [_row_to_attachment(row) for row in rows]

    def _list_unlinked_messages_sync(self, limit: int) -> list[MailMessage]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {MESSAGE_FIELDS} FROM mail_messages AS m "
                "WHERE NOT EXISTS (SELECT 1 FROM mail_event_links AS l "
                "WHERE l.mail_message_id = m.id) "
                "ORDER BY m.first_seen_at, m.id LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_message(row) for row in rows]

    def _link_inbound_event_sync(
        self,
        mail_message_id: MailMessageId,
        inbound_event_id: EventId,
        linked_at: datetime,
    ) -> None:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO mail_event_links (mail_message_id, inbound_event_id, linked_at) "
                "VALUES (?, ?, ?) ON CONFLICT (mail_message_id) DO NOTHING",
                (str(mail_message_id), str(inbound_event_id), to_utc_iso(linked_at)),
            )

    def _get_linked_event_id_sync(self, message_id: MailMessageId) -> EventId | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT inbound_event_id FROM mail_event_links WHERE mail_message_id = ?",
                (str(message_id),),
            ).fetchone()
        return None if row is None else UUID(str(row["inbound_event_id"]))

    def _count_messages_sync(self, account_id: MailAccountId | None) -> int:
        statement = "SELECT count(*) AS total FROM mail_messages"
        parameters: tuple[object, ...] = ()
        if account_id is not None:
            statement += " WHERE account_id = ?"
            parameters = (account_id,)
        with self._database.connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        return int(row["total"])

    def _count_unlinked_messages_sync(self, account_id: MailAccountId | None) -> int:
        statement = (
            "SELECT count(*) AS total FROM mail_messages AS m "
            "WHERE NOT EXISTS (SELECT 1 FROM mail_event_links AS l "
            "WHERE l.mail_message_id = m.id)"
        )
        parameters: tuple[object, ...] = ()
        if account_id is not None:
            statement += " AND m.account_id = ?"
            parameters = (account_id,)
        with self._database.connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        return int(row["total"])

    def _find_candidates_sync(
        self,
        account_id: MailAccountId,
        message_id_header: str | None,
        content_fingerprint: str,
        raw_sha256: str | None,
    ) -> ReconciliationCandidates:
        with self._database.connect() as connection:
            by_raw = None
            if raw_sha256 is not None:
                row = connection.execute(
                    f"SELECT {MESSAGE_FIELDS} FROM mail_messages "
                    "WHERE account_id = ? AND raw_sha256 = ? ORDER BY first_seen_at, id LIMIT 1",
                    (account_id, raw_sha256),
                ).fetchone()
                by_raw = None if row is None else row_to_message(row)
            by_header: tuple[MailMessage, ...] = ()
            if message_id_header is not None:
                by_header = tuple(
                    row_to_message(row)
                    for row in connection.execute(
                        f"SELECT {MESSAGE_FIELDS} FROM mail_messages "
                        "WHERE account_id = ? AND message_id_header = ? "
                        "ORDER BY first_seen_at, id",
                        (account_id, message_id_header),
                    )
                )
            by_fingerprint = tuple(
                row_to_message(row)
                for row in connection.execute(
                    f"SELECT {MESSAGE_FIELDS} FROM mail_messages "
                    "WHERE account_id = ? AND content_fingerprint = ? "
                    "ORDER BY first_seen_at, id",
                    (account_id, content_fingerprint),
                )
            )
        return ReconciliationCandidates(
            by_raw_sha256=by_raw,
            by_message_id=by_header,
            by_fingerprint=by_fingerprint,
        )


def _message_parameters(message: MailMessage) -> tuple[object, ...]:
    return (
        str(message.id),
        message.account_id,
        message.message_id_header,
        message.in_reply_to_header,
        _json_list(message.references),
        message.subject,
        message.from_address,
        _json_list(message.to_addresses),
        _json_list(message.cc_addresses),
        message.date_header,
        None if message.sent_at is None else to_utc_iso(message.sent_at),
        message.body_text,
        str(message.body_status),
        message.raw_sha256,
        message.content_fingerprint,
        message.raw_storage_key,
        message.size_bytes,
        message.parse_warnings,
        to_utc_iso(message.first_seen_at),
        to_utc_iso(message.last_seen_at),
    )


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _parse_json_list(raw: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(raw))
    except ValueError as exc:
        raise CommitmentStoreError(f"stored mail header list is not JSON: {exc}") from exc
    if not isinstance(decoded, list):
        raise CommitmentStoreError("stored mail header list is not a JSON array")
    return tuple(str(item) for item in decoded)


def row_to_message(row: sqlite3.Row) -> MailMessage:
    try:
        return MailMessage(
            id=UUID(str(row["id"])),
            account_id=str(row["account_id"]),
            message_id_header=_optional_text(row["message_id_header"]),
            in_reply_to_header=_optional_text(row["in_reply_to_header"]),
            references=_parse_json_list(row["references_json"]),
            subject=_optional_text(row["subject"]),
            from_address=_optional_text(row["from_address"]),
            to_addresses=_parse_json_list(row["to_addresses_json"]),
            cc_addresses=_parse_json_list(row["cc_addresses_json"]),
            date_header=_optional_text(row["date_header"]),
            sent_at=_optional_instant(row["sent_at"]),
            body_text=_optional_text(row["body_text"]),
            body_status=MailBodyStatus(str(row["body_status"])),
            raw_sha256=_optional_text(row["raw_sha256"]),
            content_fingerprint=str(row["content_fingerprint"]),
            raw_storage_key=_optional_text(row["raw_storage_key"]),
            size_bytes=int(row["size_bytes"]),
            parse_warnings=int(row["parse_warnings"]),
            first_seen_at=from_utc_iso(str(row["first_seen_at"])),
            last_seen_at=from_utc_iso(str(row["last_seen_at"])),
        )
    except (ValueError, DomainError, KeyError) as exc:
        raise CommitmentStoreError(f"stored mail message is not readable: {exc}") from exc


def _row_to_location(row: sqlite3.Row) -> MailMessageLocation:
    return MailMessageLocation(
        message_id=UUID(str(row["message_id"])),
        account_id=str(row["account_id"]),
        mailbox_name=str(row["mailbox_name"]),
        uidvalidity=int(row["uidvalidity"]),
        uid=int(row["uid"]),
        first_seen_at=from_utc_iso(str(row["first_seen_at"])),
        last_seen_at=from_utc_iso(str(row["last_seen_at"])),
    )


def _row_to_attachment(row: sqlite3.Row) -> MailAttachmentMetadata:
    return MailAttachmentMetadata(
        id=UUID(str(row["id"])),
        message_id=UUID(str(row["message_id"])),
        ordinal=int(row["ordinal"]),
        filename=_optional_text(row["filename"]),
        content_type=_optional_text(row["content_type"]),
        content_disposition=_optional_text(row["content_disposition"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
    )


def _row_to_state(row: sqlite3.Row) -> MailboxSyncState:
    return MailboxSyncState(
        account_id=str(row["account_id"]),
        mailbox_name=str(row["mailbox_name"]),
        uidvalidity=int(row["uidvalidity"]),
        last_seen_uid=int(row["last_seen_uid"]),
        mode=MailboxSyncMode(str(row["mode"])),
        last_sync_at=_optional_instant(row["last_sync_at"]),
        last_reconciled_at=_optional_instant(row["last_reconciled_at"]),
        updated_at=from_utc_iso(str(row["updated_at"])),
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else from_utc_iso(str(value))


__all__ = [
    "ATTACHMENT_FIELDS",
    "LOCATION_FIELDS",
    "MESSAGE_FIELDS",
    "STATE_FIELDS",
    "SqliteMailRepository",
    "row_to_message",
]
