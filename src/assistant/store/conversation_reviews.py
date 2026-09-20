"""SQLite implementation of the conversational external-review repository (ADR-0034 §4)."""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.conversation_review import (
    ConversationExternalReview,
    ConversationExternalReviewId,
    ConversationExternalReviewStatus,
)
from assistant.domain.errors import ConversationOperationNotFound
from assistant.store.db import Database, transaction
from assistant.store.serialization import from_utc_iso, to_utc_iso

REVIEW_FIELDS = (
    "id, conversation_operation_id, thread_id, action_request_id, action_type, "
    "action_fingerprint, status, expires_at, execution_run_id, created_at, updated_at"
)


class SqliteConversationReviewRepository:
    """Durable conversational reviews, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_review(self, review: ConversationExternalReview) -> ConversationExternalReview:
        return await asyncio.to_thread(self._add_review_sync, review)

    async def update_review(
        self, review: ConversationExternalReview
    ) -> ConversationExternalReview:
        return await asyncio.to_thread(self._update_review_sync, review)

    async def get_review(
        self, review_id: ConversationExternalReviewId
    ) -> ConversationExternalReview | None:
        return await asyncio.to_thread(self._get_review_sync, review_id)

    async def get_review_for_operation(
        self, operation_id: object
    ) -> ConversationExternalReview | None:
        return await asyncio.to_thread(self._get_for_operation_sync, operation_id)

    async def waiting_for_thread(self, thread_id: object) -> list[ConversationExternalReview]:
        return await asyncio.to_thread(self._waiting_sync, thread_id)

    async def list_by_status(
        self, status: ConversationExternalReviewStatus, *, limit: int = 100
    ) -> list[ConversationExternalReview]:
        return await asyncio.to_thread(self._list_by_status_sync, status, limit)

    async def list_reviews(self, *, limit: int = 200) -> list[ConversationExternalReview]:
        return await asyncio.to_thread(self._list_reviews_sync, limit)

    # ------------------------------------------------------------------------- sync

    def _add_review_sync(self, review: ConversationExternalReview) -> ConversationExternalReview:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO conversation_external_reviews ({REVIEW_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _values(review),
            )
        return review

    def _update_review_sync(
        self, review: ConversationExternalReview
    ) -> ConversationExternalReview:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE conversation_external_reviews SET status = ?, execution_run_id = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    review.status.value,
                    None if review.execution_run_id is None else str(review.execution_run_id),
                    to_utc_iso(review.updated_at),
                    str(review.id),
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationOperationNotFound(review.id)
        return review

    def _get_review_sync(
        self, review_id: ConversationExternalReviewId
    ) -> ConversationExternalReview | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {REVIEW_FIELDS} FROM conversation_external_reviews WHERE id = ?",
                (str(review_id),),
            ).fetchone()
        return None if row is None else row_to_review(row)

    def _get_for_operation_sync(self, operation_id: object) -> ConversationExternalReview | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {REVIEW_FIELDS} FROM conversation_external_reviews "
                "WHERE conversation_operation_id = ?",
                (str(operation_id),),
            ).fetchone()
        return None if row is None else row_to_review(row)

    def _waiting_sync(self, thread_id: object) -> list[ConversationExternalReview]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {REVIEW_FIELDS} FROM conversation_external_reviews "
                "WHERE thread_id = ? AND status = ? ORDER BY created_at, rowid",
                (str(thread_id), ConversationExternalReviewStatus.WAITING.value),
            ).fetchall()
        return [row_to_review(row) for row in rows]

    def _list_by_status_sync(
        self, status: ConversationExternalReviewStatus, limit: int
    ) -> list[ConversationExternalReview]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {REVIEW_FIELDS} FROM conversation_external_reviews "
                "WHERE status = ? ORDER BY created_at, rowid LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [row_to_review(row) for row in rows]

    def _list_reviews_sync(self, limit: int) -> list[ConversationExternalReview]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {REVIEW_FIELDS} FROM conversation_external_reviews "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_review(row) for row in rows]


def _values(review: ConversationExternalReview) -> tuple[object, ...]:
    return (
        str(review.id),
        str(review.conversation_operation_id),
        str(review.thread_id),
        str(review.action_request_id),
        review.action_type,
        review.action_fingerprint,
        review.status.value,
        to_utc_iso(review.expires_at),
        None if review.execution_run_id is None else str(review.execution_run_id),
        to_utc_iso(review.created_at),
        to_utc_iso(review.updated_at),
    )


def row_to_review(row: sqlite3.Row | tuple[object, ...]) -> ConversationExternalReview:
    """Rebuild one review from its row."""
    return ConversationExternalReview(
        id=UUID(str(row[0])),
        conversation_operation_id=UUID(str(row[1])),
        thread_id=UUID(str(row[2])),
        action_request_id=UUID(str(row[3])),
        action_type=str(row[4]),
        action_fingerprint=str(row[5]),
        status=ConversationExternalReviewStatus(str(row[6])),
        expires_at=from_utc_iso(str(row[7])),
        execution_run_id=None if row[8] is None else UUID(str(row[8])),
        created_at=from_utc_iso(str(row[9])),
        updated_at=from_utc_iso(str(row[10])),
    )


__all__ = [
    "REVIEW_FIELDS",
    "SqliteConversationReviewRepository",
    "row_to_review",
]
