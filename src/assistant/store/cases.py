"""SQLite implementation of the CaseRepository port (ADR-0009, ADR-0023).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

`update_case` is the compare-and-set the case lifecycle relies on: `WHERE id = ? AND
updated_at = ?` with a rowcount check, so two windows cannot both complete the same case.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assistant.domain.case import Case, CaseId, CaseStatus
from assistant.domain.errors import (
    AmbiguousId,
    CaseNotFound,
    StaleCaseUpdate,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_CASE_FIELDS = "id, title, status, created_at, updated_at, completed_at, cancelled_at"


class SqliteCaseRepository:
    """Durable cases, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_case(self, case: Case) -> Case:
        return await asyncio.to_thread(self._add_case_sync, case)

    async def get_case(self, case_id: CaseId) -> Case | None:
        return await asyncio.to_thread(self._get_case_sync, case_id)

    async def list_cases(
        self, *, statuses: Collection[CaseStatus] | None = None, limit: int | None = 20
    ) -> list[Case]:
        return await asyncio.to_thread(
            self._list_cases_sync, None if statuses is None else tuple(statuses), limit
        )

    async def update_case(self, case: Case, *, expected_updated_at: datetime) -> Case:
        return await asyncio.to_thread(
            self._update_case_sync, case, expected_updated_at
        )

    async def resolve_case_id(self, reference: str) -> CaseId:
        text = reference.strip().lower()
        if not text:
            raise CaseNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        cases = await asyncio.to_thread(self._list_case_ids_sync)
        if candidate is not None:
            if candidate not in cases:
                raise CaseNotFound(candidate)
            return candidate
        matching = [case_id for case_id in cases if str(case_id).startswith(text)]
        if not matching:
            raise CaseNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    async def count_cases(self, *, statuses: Collection[CaseStatus] | None = None) -> int:
        return await asyncio.to_thread(
            self._count_cases_sync, None if statuses is None else tuple(statuses)
        )

    # ------------------------------------------------------------ blocking internals

    def _add_case_sync(self, case: Case) -> Case:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO cases ({_CASE_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    _case_parameters(case),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a case: {exc}") from exc
        return case

    def _get_case_sync(self, case_id: CaseId) -> Case | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CASE_FIELDS} FROM cases WHERE id = ?", (str(case_id),)
            ).fetchone()
        return None if row is None else _row_to_case(row)

    def _list_cases_sync(
        self, statuses: tuple[CaseStatus, ...] | None, limit: int | None
    ) -> list[Case]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = f"SELECT {_CASE_FIELDS} FROM cases"
        parameters: list[object] = []
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            statement += f" WHERE status IN ({placeholders})"
            parameters.extend(str(status) for status in statuses)
        statement += " ORDER BY updated_at DESC, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_case(row) for row in rows]

    def _list_case_ids_sync(self) -> list[CaseId]:
        with self._database.connect() as connection:
            rows = connection.execute("SELECT id FROM cases ORDER BY id").fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _update_case_sync(self, case: Case, expected_updated_at: datetime) -> Case:
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE cases SET title = ?, status = ?, updated_at = ?, completed_at = ?, "
                    "cancelled_at = ? WHERE id = ? AND updated_at = ?",
                    (
                        case.title,
                        str(case.status),
                        to_utc_iso(case.updated_at),
                        None if case.completed_at is None else to_utc_iso(case.completed_at),
                        None if case.cancelled_at is None else to_utc_iso(case.cancelled_at),
                        str(case.id),
                        to_utc_iso(expected_updated_at),
                    ),
                )
                if cursor.rowcount == 0:
                    _stale_or_missing(connection, case.id, expected_updated_at)
                row = connection.execute(
                    f"SELECT {_CASE_FIELDS} FROM cases WHERE id = ?", (str(case.id),)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not update a case: {exc}") from exc
        if row is None:  # pragma: no cover - the update guarantees a row
            raise CommitmentStoreError(f"case {case.id} vanished after being updated")
        return _row_to_case(row)

    def _count_cases_sync(self, statuses: tuple[CaseStatus, ...] | None) -> int:
        statement = "SELECT count(*) AS total FROM cases"
        parameters: tuple[object, ...] = ()
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            statement += f" WHERE status IN ({placeholders})"
            parameters = tuple(str(status) for status in statuses)
        with self._database.connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        return int(row["total"])


def _stale_or_missing(
    connection: sqlite3.Connection, case_id: CaseId, expected_updated_at: datetime
) -> None:
    row = connection.execute(
        "SELECT updated_at FROM cases WHERE id = ?", (str(case_id),)
    ).fetchone()
    if row is None:
        raise CaseNotFound(case_id)
    raise StaleCaseUpdate(case_id, expected_updated_at, from_utc_iso(str(row["updated_at"])))


def _case_parameters(case: Case) -> tuple[object, ...]:
    return (
        str(case.id),
        case.title,
        str(case.status),
        to_utc_iso(case.created_at),
        to_utc_iso(case.updated_at),
        None if case.completed_at is None else to_utc_iso(case.completed_at),
        None if case.cancelled_at is None else to_utc_iso(case.cancelled_at),
    )


def _row_to_case(row: sqlite3.Row) -> Case:
    try:
        completed = row["completed_at"]
        cancelled = row["cancelled_at"]
        return Case(
            id=UUID(str(row["id"])),
            title=str(row["title"]),
            status=CaseStatus(str(row["status"])),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            completed_at=None if completed is None else from_utc_iso(str(completed)),
            cancelled_at=None if cancelled is None else from_utc_iso(str(cancelled)),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(f"stored case is not readable: {exc}") from exc


__all__ = ["SqliteCaseRepository"]
