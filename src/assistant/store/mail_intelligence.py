"""SQLite implementation of threads and mail analyses (ADR-0009, ADR-0021).

The same blocking discipline as the mail store applies here: every public method is an async
boundary over a `_*_sync` method, and each blocking method opens its own connection inside the
worker thread that uses it.

Two behaviours are worth naming:

- **recording a membership twice is a no-op that returns the stored row.** Threading is
  derived data and the linker may legitimately run twice for the same message; the second run
  must observe the first decision instead of overwriting it, which is what makes a `ROOT`
  decision stable rather than a race.
- **an analysis is replaced atomically and keeps its `created_at`.** A re-analysis under a new
  fingerprint replaces the row in one transaction, so no reader ever sees a half-updated
  analysis, and the original creation time still says when this message was first analyzed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from uuid import UUID

from assistant.domain.errors import AmbiguousId, MailThreadNotFound
from assistant.domain.mail import MailMessage, MailMessageId
from assistant.domain.mail_analysis import (
    MailActionCandidate,
    MailAnalysis,
    MailCategory,
    MailLinkStatus,
    MailThread,
    MailThreadId,
    MailThreadMember,
    MailThreadSummary,
    parse_action_candidates,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.mail import MESSAGE_FIELDS, row_to_message
from assistant.store.serialization import from_utc_iso, to_utc_iso

_THREAD_FIELDS = "id, account_id, created_at, updated_at"

_MEMBER_FIELDS = (
    "message_id, thread_id, parent_message_id, link_status, link_evidence, linked_at"
)

_ANALYSIS_FIELDS = (
    "message_id, analyzer_version, input_fingerprint, category, requires_reply, summary, "
    "action_candidates_json, created_at, updated_at"
)

# The conversation order: a message with no parseable Date sorts last, never first.
_MESSAGE_ORDER = "(m.sent_at IS NULL), m.sent_at, m.first_seen_at, m.id"

# What "the latest message of a thread" means, in the same terms `latest_at` uses: a message
# without a Date falls back to when it was first seen, and the id breaks ties.
_LATEST_KEY = "COALESCE(m.sent_at, m.first_seen_at)"


class SqliteMailIntelligenceRepository:
    """Durable thread membership and mail analyses, backed by the runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_member(self, message_id: MailMessageId) -> MailThreadMember | None:
        return await asyncio.to_thread(self._get_member_sync, message_id)

    async def record_member(self, member: MailThreadMember) -> MailThreadMember:
        return await asyncio.to_thread(self._record_member_sync, member)

    async def create_thread(self, thread: MailThread) -> MailThread:
        return await asyncio.to_thread(self._create_thread_sync, thread)

    async def get_thread(self, thread_id: MailThreadId) -> MailThread | None:
        return await asyncio.to_thread(self._get_thread_sync, thread_id)

    async def resolve_thread_id(self, reference: str) -> MailThreadId:
        text = reference.strip().lower()
        if not text:
            raise MailThreadNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        threads = await asyncio.to_thread(self._list_thread_ids_sync)
        if candidate is not None:
            if candidate not in threads:
                raise MailThreadNotFound(candidate)
            return candidate
        matching = [thread_id for thread_id in threads if str(thread_id).startswith(text)]
        if not matching:
            raise MailThreadNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    async def list_thread_summaries(
        self, *, account_id: str | None = None, limit: int | None = 20
    ) -> list[MailThreadSummary]:
        return await asyncio.to_thread(self._list_thread_summaries_sync, account_id, limit)

    async def list_thread_members(self, thread_id: MailThreadId) -> list[MailThreadMember]:
        return await asyncio.to_thread(self._list_thread_members_sync, thread_id)

    async def list_thread_messages(self, thread_id: MailThreadId) -> list[MailMessage]:
        return await asyncio.to_thread(self._list_thread_messages_sync, thread_id)

    async def find_messages_by_message_id_header(
        self, *, account_id: str, message_id_header: str
    ) -> list[MailMessageId]:
        return await asyncio.to_thread(
            self._find_by_header_sync, account_id, message_id_header
        )

    async def get_analysis(self, message_id: MailMessageId) -> MailAnalysis | None:
        return await asyncio.to_thread(self._get_analysis_sync, message_id)

    async def persist_analysis(self, analysis: MailAnalysis) -> MailAnalysis:
        return await asyncio.to_thread(self._persist_analysis_sync, analysis)

    async def count_analyses(self, *, account_id: str | None = None) -> int:
        return await asyncio.to_thread(self._count_analyses_sync, account_id)

    # ------------------------------------------------------------ blocking internals

    def _get_member_sync(self, message_id: MailMessageId) -> MailThreadMember | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_MEMBER_FIELDS} FROM mail_thread_members WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
        return None if row is None else _row_to_member(row)

    def _record_member_sync(self, member: MailThreadMember) -> MailThreadMember:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO mail_thread_members ({_MEMBER_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
                    _member_parameters(member),
                )
                connection.execute(
                    "UPDATE mail_threads SET updated_at = ? WHERE id = ?",
                    (to_utc_iso(member.linked_at), str(member.thread_id)),
                )
                row = connection.execute(
                    f"SELECT {_MEMBER_FIELDS} FROM mail_thread_members WHERE message_id = ?",
                    (str(member.message_id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not record a mail thread member: {exc}") from exc
        if row is None:  # pragma: no cover - the insert or a prior row guarantees a row
            raise CommitmentStoreError(
                f"mail thread member {member.message_id} vanished after being recorded"
            )
        return _row_to_member(row)

    def _create_thread_sync(self, thread: MailThread) -> MailThread:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO mail_threads ({_THREAD_FIELDS}) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        str(thread.id),
                        thread.account_id,
                        to_utc_iso(thread.created_at),
                        to_utc_iso(thread.updated_at),
                    ),
                )
                row = connection.execute(
                    f"SELECT {_THREAD_FIELDS} FROM mail_threads WHERE id = ?",
                    (str(thread.id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not create a mail thread: {exc}") from exc
        if row is None:  # pragma: no cover - the insert guarantees a row
            raise CommitmentStoreError(f"mail thread {thread.id} vanished after being created")
        return _row_to_thread(row)

    def _get_thread_sync(self, thread_id: MailThreadId) -> MailThread | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_THREAD_FIELDS} FROM mail_threads WHERE id = ?", (str(thread_id),)
            ).fetchone()
        return None if row is None else _row_to_thread(row)

    def _list_thread_ids_sync(self) -> list[MailThreadId]:
        with self._database.connect() as connection:
            rows = connection.execute("SELECT id FROM mail_threads ORDER BY id").fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _list_thread_summaries_sync(
        self, account_id: str | None, limit: int | None
    ) -> list[MailThreadSummary]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = (
            "SELECT t.id, t.account_id, t.created_at, t.updated_at, "
            "(SELECT count(*) FROM mail_thread_members AS members "
            " WHERE members.thread_id = t.id) AS message_count, "
            "(SELECT max(COALESCE(m.sent_at, m.first_seen_at)) "
            " FROM mail_thread_members AS members "
            " JOIN mail_messages AS m ON m.id = members.message_id "
            " WHERE members.thread_id = t.id) AS latest_at, "
            "(SELECT m.subject FROM mail_thread_members AS members "
            " JOIN mail_messages AS m ON m.id = members.message_id "
            " WHERE members.thread_id = t.id "
            f" ORDER BY {_LATEST_KEY} DESC, (m.sent_at IS NULL), m.id DESC "
            "LIMIT 1) AS subject_preview "
            "FROM mail_threads AS t "
            "WHERE EXISTS (SELECT 1 FROM mail_thread_members AS members "
            " WHERE members.thread_id = t.id)"
        )
        parameters: list[object] = []
        if account_id is not None:
            statement += " AND t.account_id = ?"
            parameters.append(account_id)
        statement += " ORDER BY latest_at DESC, t.id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_summary(row) for row in rows]

    def _list_thread_members_sync(self, thread_id: MailThreadId) -> list[MailThreadMember]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_MEMBER_FIELDS} FROM mail_thread_members WHERE thread_id = ? "
                "ORDER BY linked_at, message_id",
                (str(thread_id),),
            ).fetchall()
        return [_row_to_member(row) for row in rows]

    def _list_thread_messages_sync(self, thread_id: MailThreadId) -> list[MailMessage]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {MESSAGE_FIELDS} FROM mail_thread_members AS members "
                f"JOIN mail_messages AS m ON m.id = members.message_id "
                f"WHERE members.thread_id = ? ORDER BY {_MESSAGE_ORDER}",
                (str(thread_id),),
            ).fetchall()
        return [row_to_message(row) for row in rows]

    def _find_by_header_sync(
        self, account_id: str, message_id_header: str
    ) -> list[MailMessageId]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM mail_messages WHERE account_id = ? AND message_id_header = ? "
                "ORDER BY first_seen_at, id",
                (account_id, message_id_header),
            ).fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _get_analysis_sync(self, message_id: MailMessageId) -> MailAnalysis | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_ANALYSIS_FIELDS} FROM mail_analyses WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
        return None if row is None else _row_to_analysis(row)

    def _persist_analysis_sync(self, analysis: MailAnalysis) -> MailAnalysis:
        candidates = json.dumps(
            [candidate.to_payload() for candidate in analysis.action_candidates],
            ensure_ascii=False,
        )
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO mail_analyses ({_ANALYSIS_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (message_id) DO UPDATE SET "
                    "analyzer_version = excluded.analyzer_version, "
                    "input_fingerprint = excluded.input_fingerprint, "
                    "category = excluded.category, "
                    "requires_reply = excluded.requires_reply, "
                    "summary = excluded.summary, "
                    "action_candidates_json = excluded.action_candidates_json, "
                    "updated_at = excluded.updated_at",
                    (
                        str(analysis.message_id),
                        analysis.analyzer_version,
                        analysis.input_fingerprint,
                        str(analysis.category),
                        1 if analysis.requires_reply else 0,
                        analysis.summary,
                        candidates,
                        to_utc_iso(analysis.created_at),
                        to_utc_iso(analysis.updated_at),
                    ),
                )
                row = connection.execute(
                    f"SELECT {_ANALYSIS_FIELDS} FROM mail_analyses WHERE message_id = ?",
                    (str(analysis.message_id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a mail analysis: {exc}") from exc
        if row is None:  # pragma: no cover - the upsert guarantees a row
            raise CommitmentStoreError(
                f"mail analysis for {analysis.message_id} vanished after being stored"
            )
        return _row_to_analysis(row)

    def _count_analyses_sync(self, account_id: str | None) -> int:
        statement = (
            "SELECT count(*) AS total FROM mail_analyses AS analyses "
            "JOIN mail_messages AS m ON m.id = analyses.message_id"
        )
        parameters: tuple[object, ...] = ()
        if account_id is not None:
            statement += " WHERE m.account_id = ?"
            parameters = (account_id,)
        with self._database.connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        return int(row["total"])


def _member_parameters(member: MailThreadMember) -> tuple[object, ...]:
    return (
        str(member.message_id),
        str(member.thread_id),
        None if member.parent_message_id is None else str(member.parent_message_id),
        str(member.link_status),
        member.link_evidence,
        to_utc_iso(member.linked_at),
    )


def _row_to_member(row: sqlite3.Row) -> MailThreadMember:
    try:
        parent = row["parent_message_id"]
        return MailThreadMember(
            message_id=UUID(str(row["message_id"])),
            thread_id=UUID(str(row["thread_id"])),
            parent_message_id=None if parent is None else UUID(str(parent)),
            link_status=MailLinkStatus(str(row["link_status"])),
            link_evidence=None if row["link_evidence"] is None else str(row["link_evidence"]),
            linked_at=from_utc_iso(str(row["linked_at"])),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(f"stored thread member is not readable: {exc}") from exc


def _row_to_thread(row: sqlite3.Row) -> MailThread:
    return MailThread(
        id=UUID(str(row["id"])),
        account_id=str(row["account_id"]),
        created_at=from_utc_iso(str(row["created_at"])),
        updated_at=from_utc_iso(str(row["updated_at"])),
    )


def _row_to_summary(row: sqlite3.Row) -> MailThreadSummary:
    latest = row["latest_at"]
    if latest is None:  # pragma: no cover - the query only returns threads with members
        raise CommitmentStoreError("a listed mail thread carries no messages")
    return MailThreadSummary(
        thread=_row_to_thread(row),
        message_count=int(row["message_count"]),
        latest_at=from_utc_iso(str(latest)),
        subject_preview=None if row["subject_preview"] is None else str(row["subject_preview"]),
    )


def _row_to_analysis(row: sqlite3.Row) -> MailAnalysis:
    try:
        decoded = json.loads(str(row["action_candidates_json"]))
        candidates: tuple[MailActionCandidate, ...] = parse_action_candidates(decoded)
        return MailAnalysis(
            message_id=UUID(str(row["message_id"])),
            analyzer_version=int(row["analyzer_version"]),
            input_fingerprint=str(row["input_fingerprint"]),
            category=MailCategory(str(row["category"])),
            requires_reply=bool(int(row["requires_reply"])),
            summary=str(row["summary"]),
            action_candidates=candidates,
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(f"stored mail analysis is not readable: {exc}") from exc

__all__ = ["SqliteMailIntelligenceRepository"]
