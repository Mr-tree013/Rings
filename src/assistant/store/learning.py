"""SQLite implementation of the LearningRepository port (ADR-0009, ADR-0027).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

The two transactional operations are the ones worth reading twice:

- **`create_candidate_with_correction`** inserts the user's text and the proposal together, so a
  candidate without provenance cannot exist and a correction cannot be orphaned by a crash;
- **`confirm_candidate`** runs in one `BEGIN IMMEDIATE`: it re-reads the candidate, requires it to
  be `PENDING`, retires the key's current fact with a compare-and-set, inserts the new fact and
  records the single status transition. Two callers racing on the same key serialise here, so at
  every committed state there is at most one current fact; a failure anywhere rolls the retirement
  back, which is why a failed confirmation cannot leave the key empty.

Expiry never appears in a write. A fact that has passed `valid_until` is still the current row for
its key — it simply is not *active* — which is what lets a new confirmation retire it without a
background job ever running.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from assistant.domain.correction import Correction, CorrectionId
from assistant.domain.errors import (
    AmbiguousId,
    ConfirmedFactNotFound,
    CorrectionNotFound,
    ExpiredFactCandidate,
    FactCandidateNotFound,
    InvalidFactCandidateTransition,
)
from assistant.domain.fact import (
    ConfirmedFact,
    ConfirmedFactId,
    FactCandidate,
    FactCandidateId,
    FactCandidateStatus,
    FactKey,
)
from assistant.ports.learning_repository import FactConfirmation
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_CORRECTION_FIELDS = "id, text, created_at"
_CANDIDATE_FIELDS = (
    "id, fact_key, value, correction_id, status, proposed_valid_until, created_at, resolved_at"
)
_FACT_FIELDS = (
    "id, candidate_id, fact_key, value, valid_from, valid_until, created_at, superseded_at"
)

_ACTIVE_CONDITION = "superseded_at IS NULL AND (valid_until IS NULL OR valid_until > ?)"


class SqliteLearningRepository:
    """Durable corrections, candidate facts, confirmed facts and their history."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_correction(self, correction: Correction) -> Correction:
        return await asyncio.to_thread(self._add_correction_sync, correction)

    async def create_candidate_with_correction(
        self, *, correction: Correction, candidate: FactCandidate
    ) -> tuple[Correction, FactCandidate]:
        return await asyncio.to_thread(
            self._create_candidate_with_correction_sync, correction, candidate
        )

    async def get_correction(self, correction_id: CorrectionId) -> Correction | None:
        return await asyncio.to_thread(self._get_correction_sync, correction_id)

    async def list_corrections(self, *, limit: int | None = 20) -> list[Correction]:
        return await asyncio.to_thread(self._list_corrections_sync, limit)

    async def resolve_correction_id(self, reference: str) -> CorrectionId:
        corrections = await self.list_corrections(limit=None)
        return _resolve_id(
            reference, [item.id for item in corrections], CorrectionNotFound(reference)
        )

    async def get_candidate(self, candidate_id: FactCandidateId) -> FactCandidate | None:
        return await asyncio.to_thread(self._get_candidate_sync, candidate_id)

    async def list_candidates(
        self,
        *,
        statuses: Collection[FactCandidateStatus] | None = None,
        limit: int | None = 20,
    ) -> list[FactCandidate]:
        return await asyncio.to_thread(
            self._list_candidates_sync,
            None if statuses is None else tuple(statuses),
            limit,
        )

    async def resolve_candidate_id(self, reference: str) -> FactCandidateId:
        candidates = await self.list_candidates(limit=None)
        return _resolve_id(
            reference, [item.id for item in candidates], FactCandidateNotFound(reference)
        )

    async def confirm_candidate(
        self,
        *,
        candidate_id: FactCandidateId,
        fact_id: ConfirmedFactId,
        now: datetime,
    ) -> FactConfirmation:
        return await asyncio.to_thread(
            self._confirm_candidate_sync, candidate_id, fact_id, now
        )

    async def reject_candidate(
        self, *, candidate_id: FactCandidateId, now: datetime
    ) -> FactCandidate:
        return await asyncio.to_thread(self._reject_candidate_sync, candidate_id, now)

    async def get_confirmed_fact(self, fact_id: ConfirmedFactId) -> ConfirmedFact | None:
        return await asyncio.to_thread(self._get_confirmed_fact_sync, fact_id)

    async def list_confirmed_facts(self, *, limit: int | None = None) -> list[ConfirmedFact]:
        return await asyncio.to_thread(self._list_facts_sync, None, limit)

    async def resolve_confirmed_fact_id(self, reference: str) -> ConfirmedFactId:
        facts = await self.list_confirmed_facts(limit=None)
        return _resolve_id(
            reference, [item.id for item in facts], ConfirmedFactNotFound(reference)
        )

    async def get_active_fact_by_key(
        self, fact_key: FactKey, *, now: datetime
    ) -> ConfirmedFact | None:
        return await asyncio.to_thread(self._get_active_fact_by_key_sync, fact_key, now)

    async def list_active_facts(
        self, *, now: datetime, limit: int | None = None
    ) -> list[ConfirmedFact]:
        return await asyncio.to_thread(self._list_facts_sync, now, limit)

    # ------------------------------------------------------------ blocking internals

    def _add_correction_sync(self, correction: Correction) -> Correction:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO corrections ({_CORRECTION_FIELDS}) VALUES (?, ?, ?)",
                    _correction_parameters(correction),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store the correction: {exc}") from exc
        return correction

    def _create_candidate_with_correction_sync(
        self, correction: Correction, candidate: FactCandidate
    ) -> tuple[Correction, FactCandidate]:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO corrections ({_CORRECTION_FIELDS}) VALUES (?, ?, ?)",
                    _correction_parameters(correction),
                )
                connection.execute(
                    f"INSERT INTO fact_candidates ({_CANDIDATE_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _candidate_parameters(candidate),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not store the correction and its candidate: {exc}"
            ) from exc
        return correction, candidate

    def _get_correction_sync(self, correction_id: CorrectionId) -> Correction | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CORRECTION_FIELDS} FROM corrections WHERE id = ?",
                (str(correction_id),),
            ).fetchone()
        return None if row is None else _row_to_correction(row)

    def _list_corrections_sync(self, limit: int | None) -> list[Correction]:
        statement = (
            f"SELECT {_CORRECTION_FIELDS} FROM corrections ORDER BY created_at DESC, id"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_correction(row) for row in rows]

    def _get_candidate_sync(self, candidate_id: FactCandidateId) -> FactCandidate | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_FIELDS} FROM fact_candidates WHERE id = ?",
                (str(candidate_id),),
            ).fetchone()
        return None if row is None else _row_to_candidate(row)

    def _list_candidates_sync(
        self, statuses: tuple[FactCandidateStatus, ...] | None, limit: int | None
    ) -> list[FactCandidate]:
        statement = f"SELECT {_CANDIDATE_FIELDS} FROM fact_candidates"
        parameters: list[object] = []
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            statement += f" WHERE status IN ({placeholders})"
            parameters.extend(item.value for item in statuses)
        statement += " ORDER BY created_at DESC, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def _confirm_candidate_sync(
        self, candidate_id: FactCandidateId, fact_id: ConfirmedFactId, now: datetime
    ) -> FactConfirmation:
        try:
            with self._database.connect() as connection, transaction(connection):
                candidate = _require_pending_candidate(
                    connection, candidate_id, FactCandidateStatus.CONFIRMED
                )
                if (
                    candidate.proposed_valid_until is not None
                    and candidate.proposed_valid_until <= now
                ):
                    # The window closed while the candidate waited; confirming it would create a
                    # fact that is born expired, which the domain refuses.
                    raise ExpiredFactCandidate(candidate.id, candidate.proposed_valid_until)

                current = _current_fact(connection, candidate.fact_key)
                superseded: ConfirmedFact | None = None
                if current is not None:
                    connection.execute(
                        "UPDATE confirmed_facts SET superseded_at = ? "
                        "WHERE id = ? AND superseded_at IS NULL",
                        (to_utc_iso(now), str(current.id)),
                    )
                    # Hand back the row as it now stands: retired at `now`, not as it was read.
                    superseded = replace(current, superseded_at=now)

                fact = ConfirmedFact(
                    id=fact_id,
                    candidate_id=candidate.id,
                    fact_key=candidate.fact_key,
                    value=candidate.value,
                    valid_from=now,
                    valid_until=candidate.proposed_valid_until,
                    created_at=now,
                )
                connection.execute(
                    f"INSERT INTO confirmed_facts ({_FACT_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                    _fact_parameters(fact),
                )
                cursor = connection.execute(
                    "UPDATE fact_candidates SET status = ?, resolved_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        FactCandidateStatus.CONFIRMED.value,
                        to_utc_iso(now),
                        str(candidate.id),
                        FactCandidateStatus.PENDING.value,
                    ),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise InvalidFactCandidateTransition(
                        candidate.id, candidate.status, FactCandidateStatus.CONFIRMED
                    )
        except sqlite3.IntegrityError as exc:
            # The partial unique index is the database's half of "one current fact per key"; a
            # candidate can only be confirmed once because its id is UNIQUE on the fact side.
            raise CommitmentStoreError(f"could not confirm the candidate: {exc}") from exc
        return FactConfirmation(fact=fact, superseded=superseded)

    def _reject_candidate_sync(
        self, candidate_id: FactCandidateId, now: datetime
    ) -> FactCandidate:
        try:
            with self._database.connect() as connection, transaction(connection):
                candidate = _require_pending_candidate(
                    connection, candidate_id, FactCandidateStatus.REJECTED
                )
                cursor = connection.execute(
                    "UPDATE fact_candidates SET status = ?, resolved_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        FactCandidateStatus.REJECTED.value,
                        to_utc_iso(now),
                        str(candidate.id),
                        FactCandidateStatus.PENDING.value,
                    ),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise InvalidFactCandidateTransition(
                        candidate.id, candidate.status, FactCandidateStatus.REJECTED
                    )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not reject the candidate: {exc}") from exc
        return candidate.reject(now)

    def _get_confirmed_fact_sync(self, fact_id: ConfirmedFactId) -> ConfirmedFact | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_FACT_FIELDS} FROM confirmed_facts WHERE id = ?",
                (str(fact_id),),
            ).fetchone()
        return None if row is None else _row_to_fact(row)

    def _list_facts_sync(
        self, now: datetime | None, limit: int | None
    ) -> list[ConfirmedFact]:
        """List facts, newest first. `now` narrows the list to the active ones."""
        statement = f"SELECT {_FACT_FIELDS} FROM confirmed_facts"
        parameters: list[object] = []
        if now is not None:
            statement += f" WHERE {_ACTIVE_CONDITION}"
            parameters.append(to_utc_iso(now))
        statement += " ORDER BY created_at DESC, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_fact(row) for row in rows]

    def _get_active_fact_by_key_sync(
        self, fact_key: FactKey, now: datetime
    ) -> ConfirmedFact | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_FACT_FIELDS} FROM confirmed_facts "
                f"WHERE fact_key = ? AND {_ACTIVE_CONDITION} LIMIT 1",
                (fact_key, to_utc_iso(now)),
            ).fetchone()
        return None if row is None else _row_to_fact(row)


def _current_fact(
    connection: sqlite3.Connection, fact_key: FactKey
) -> ConfirmedFact | None:
    """The current row for a key, expired or not.

    Expiry is deliberately ignored here: an expired-but-current row still owns the key, so it has
    to be retired before a new value can take over.
    """
    row = connection.execute(
        f"SELECT {_FACT_FIELDS} FROM confirmed_facts "
        "WHERE fact_key = ? AND superseded_at IS NULL",
        (fact_key,),
    ).fetchone()
    return None if row is None else _row_to_fact(row)


def _require_pending_candidate(
    connection: sqlite3.Connection,
    candidate_id: FactCandidateId,
    target: FactCandidateStatus,
) -> FactCandidate:
    """Load a candidate that may still move to `target`, or refuse the transition."""
    row = connection.execute(
        f"SELECT {_CANDIDATE_FIELDS} FROM fact_candidates WHERE id = ?",
        (str(candidate_id),),
    ).fetchone()
    if row is None:
        raise FactCandidateNotFound(candidate_id)
    candidate = _row_to_candidate(row)
    if not candidate.is_pending:
        raise InvalidFactCandidateTransition(candidate.id, candidate.status, target)
    return candidate


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


def _correction_parameters(correction: Correction) -> tuple[object, ...]:
    return (str(correction.id), correction.text, to_utc_iso(correction.created_at))


def _candidate_parameters(candidate: FactCandidate) -> tuple[object, ...]:
    return (
        str(candidate.id),
        candidate.fact_key,
        candidate.value,
        str(candidate.correction_id),
        candidate.status.value,
        None
        if candidate.proposed_valid_until is None
        else to_utc_iso(candidate.proposed_valid_until),
        to_utc_iso(candidate.created_at),
        None if candidate.resolved_at is None else to_utc_iso(candidate.resolved_at),
    )


def _fact_parameters(fact: ConfirmedFact) -> tuple[object, ...]:
    return (
        str(fact.id),
        str(fact.candidate_id),
        fact.fact_key,
        fact.value,
        to_utc_iso(fact.valid_from),
        None if fact.valid_until is None else to_utc_iso(fact.valid_until),
        to_utc_iso(fact.created_at),
    )


def _row_to_correction(row: sqlite3.Row) -> Correction:
    return Correction(
        id=UUID(str(row["id"])),
        text=str(row["text"]),
        created_at=from_utc_iso(str(row["created_at"])),
    )


def _row_to_candidate(row: sqlite3.Row) -> FactCandidate:
    proposed = row["proposed_valid_until"]
    resolved = row["resolved_at"]
    return FactCandidate(
        id=UUID(str(row["id"])),
        fact_key=str(row["fact_key"]),
        value=str(row["value"]),
        correction_id=UUID(str(row["correction_id"])),
        status=FactCandidateStatus(str(row["status"])),
        proposed_valid_until=None if proposed is None else from_utc_iso(str(proposed)),
        created_at=from_utc_iso(str(row["created_at"])),
        resolved_at=None if resolved is None else from_utc_iso(str(resolved)),
    )


def _row_to_fact(row: sqlite3.Row) -> ConfirmedFact:
    until = row["valid_until"]
    superseded = row["superseded_at"]
    return ConfirmedFact(
        id=UUID(str(row["id"])),
        candidate_id=UUID(str(row["candidate_id"])),
        fact_key=str(row["fact_key"]),
        value=str(row["value"]),
        valid_from=from_utc_iso(str(row["valid_from"])),
        valid_until=None if until is None else from_utc_iso(str(until)),
        created_at=from_utc_iso(str(row["created_at"])),
        superseded_at=None if superseded is None else from_utc_iso(str(superseded)),
    )


__all__ = ["SqliteLearningRepository"]
