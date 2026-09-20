"""SQLite implementation of the PlaybookRepository port (ADR-0009, ADR-0028).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

`promote_candidate` is the one transaction worth reading twice. It re-reads the candidate, requires
it to still be pending, finds the newest recorded dry run that passed under the *current* contract
version with this exact snapshot fingerprint, inserts the playbook and records the candidate's
single transition — all inside one `BEGIN IMMEDIATE`. A failure anywhere leaves the candidate
pending and no playbook behind, because "promoted" without its playbook would be an audit record
that lies about what happened.

Nothing in this module writes to `action_requests`, `approval_challenges`, `approvals` or
`execution_runs`. A playbook references an action; it never creates or rewrites one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assistant.domain.action import ActionType
from assistant.domain.errors import (
    AmbiguousId,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookTransition,
    PlaybookCandidateExists,
    PlaybookCandidateNotFound,
    PlaybookCandidateNotTested,
    PlaybookNotFound,
)
from assistant.domain.playbook import (
    Playbook,
    PlaybookCandidate,
    PlaybookCandidateId,
    PlaybookCandidateStatus,
    PlaybookId,
    PlaybookReplayTest,
    PlaybookReplayTestId,
    PlaybookStatus,
    ReplayTestStatus,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_CANDIDATE_FIELDS = (
    "id, name, note, source_action_id, source_execution_run_id, source_action_type, "
    "source_action_fingerprint, status, created_at, resolved_at"
)
_TEST_FIELDS = (
    "id, candidate_id, action_type, contract_version, input_fingerprint, status, "
    "issue_codes_json, tested_at"
)
_PLAYBOOK_FIELDS = (
    "id, candidate_id, name, note, action_type, source_action_id, source_execution_run_id, "
    "source_action_fingerprint, replay_contract_version, promoted_from_test_id, status, "
    "created_at, retired_at"
)


class SqlitePlaybookRepository:
    """Durable playbook candidates, replay tests and playbooks."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------- candidates

    async def create_candidate(self, candidate: PlaybookCandidate) -> PlaybookCandidate:
        return await asyncio.to_thread(self._create_candidate_sync, candidate)

    async def get_candidate(
        self, candidate_id: PlaybookCandidateId
    ) -> PlaybookCandidate | None:
        return await asyncio.to_thread(self._get_candidate_sync, candidate_id)

    async def list_candidates(
        self,
        *,
        statuses: Collection[PlaybookCandidateStatus] | None = None,
        limit: int | None = 20,
    ) -> list[PlaybookCandidate]:
        return await asyncio.to_thread(
            self._list_candidates_sync,
            None if statuses is None else tuple(statuses),
            limit,
        )

    async def resolve_candidate_id(self, reference: str) -> PlaybookCandidateId:
        candidates = await self.list_candidates(limit=None)
        return _resolve_id(
            reference,
            [item.id for item in candidates],
            PlaybookCandidateNotFound(reference),
        )

    # ---------------------------------------------------------------- replay tests

    async def add_replay_test(self, test: PlaybookReplayTest) -> PlaybookReplayTest:
        return await asyncio.to_thread(self._add_replay_test_sync, test)

    async def list_replay_tests(
        self, candidate_id: PlaybookCandidateId
    ) -> list[PlaybookReplayTest]:
        return await asyncio.to_thread(self._list_replay_tests_sync, candidate_id)

    async def get_replay_test(
        self, test_id: PlaybookReplayTestId
    ) -> PlaybookReplayTest | None:
        return await asyncio.to_thread(self._get_replay_test_sync, test_id)

    async def latest_qualifying_test(
        self,
        *,
        candidate_id: PlaybookCandidateId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
    ) -> PlaybookReplayTest | None:
        return await asyncio.to_thread(
            self._latest_qualifying_test_sync,
            candidate_id,
            action_type,
            contract_version,
            input_fingerprint,
        )

    # -------------------------------------------------------------------- promotion

    async def promote_candidate(
        self,
        *,
        candidate_id: PlaybookCandidateId,
        playbook_id: PlaybookId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
        now: datetime,
    ) -> Playbook:
        return await asyncio.to_thread(
            self._promote_candidate_sync,
            candidate_id,
            playbook_id,
            action_type,
            contract_version,
            input_fingerprint,
            now,
        )

    async def reject_candidate(
        self, *, candidate_id: PlaybookCandidateId, now: datetime
    ) -> PlaybookCandidate:
        return await asyncio.to_thread(self._reject_candidate_sync, candidate_id, now)

    # -------------------------------------------------------------------- playbooks

    async def get_playbook(self, playbook_id: PlaybookId) -> Playbook | None:
        return await asyncio.to_thread(self._get_playbook_sync, playbook_id)

    async def list_playbooks(
        self,
        *,
        statuses: Collection[PlaybookStatus] | None = None,
        limit: int | None = None,
    ) -> list[Playbook]:
        return await asyncio.to_thread(
            self._list_playbooks_sync,
            None if statuses is None else tuple(statuses),
            limit,
        )

    async def resolve_playbook_id(self, reference: str) -> PlaybookId:
        playbooks = await self.list_playbooks(limit=None)
        return _resolve_id(
            reference, [item.id for item in playbooks], PlaybookNotFound(reference)
        )

    async def playbook_for_candidate(
        self, candidate_id: PlaybookCandidateId
    ) -> Playbook | None:
        return await asyncio.to_thread(self._playbook_for_candidate_sync, candidate_id)

    async def retire_playbook(
        self, *, playbook_id: PlaybookId, now: datetime
    ) -> Playbook:
        return await asyncio.to_thread(self._retire_playbook_sync, playbook_id, now)

    # ------------------------------------------------------------ blocking internals

    def _create_candidate_sync(self, candidate: PlaybookCandidate) -> PlaybookCandidate:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO playbook_candidates ({_CANDIDATE_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _candidate_parameters(candidate),
                )
        except sqlite3.IntegrityError as exc:
            if _is_unique_violation(exc, "playbook_candidates"):
                raise PlaybookCandidateExists(candidate.source_action_id) from exc
            raise CommitmentStoreError(f"could not store the candidate: {exc}") from exc
        return candidate

    def _get_candidate_sync(
        self, candidate_id: PlaybookCandidateId
    ) -> PlaybookCandidate | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_FIELDS} FROM playbook_candidates WHERE id = ?",
                (str(candidate_id),),
            ).fetchone()
        return None if row is None else _row_to_candidate(row)

    def _list_candidates_sync(
        self,
        statuses: tuple[PlaybookCandidateStatus, ...] | None,
        limit: int | None,
    ) -> list[PlaybookCandidate]:
        statement = f"SELECT {_CANDIDATE_FIELDS} FROM playbook_candidates"
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

    def _add_replay_test_sync(self, test: PlaybookReplayTest) -> PlaybookReplayTest:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO playbook_replay_tests ({_TEST_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _test_parameters(test),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not record the replay test: {exc}") from exc
        return test

    def _list_replay_tests_sync(
        self, candidate_id: PlaybookCandidateId
    ) -> list[PlaybookReplayTest]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_TEST_FIELDS} FROM playbook_replay_tests WHERE candidate_id = ? "
                "ORDER BY tested_at DESC, id DESC",
                (str(candidate_id),),
            ).fetchall()
        return [_row_to_test(row) for row in rows]

    def _get_replay_test_sync(
        self, test_id: PlaybookReplayTestId
    ) -> PlaybookReplayTest | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_TEST_FIELDS} FROM playbook_replay_tests WHERE id = ?",
                (str(test_id),),
            ).fetchone()
        return None if row is None else _row_to_test(row)

    def _latest_qualifying_test_sync(
        self,
        candidate_id: PlaybookCandidateId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
    ) -> PlaybookReplayTest | None:
        with self._database.connect() as connection:
            return _qualifying_test(
                connection,
                candidate_id=candidate_id,
                action_type=action_type,
                contract_version=contract_version,
                input_fingerprint=input_fingerprint,
            )

    def _promote_candidate_sync(
        self,
        candidate_id: PlaybookCandidateId,
        playbook_id: PlaybookId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
        now: datetime,
    ) -> Playbook:
        try:
            with self._database.connect() as connection, transaction(connection):
                candidate = _require_pending_candidate(
                    connection, candidate_id, PlaybookCandidateStatus.PROMOTED
                )
                test = _qualifying_test(
                    connection,
                    candidate_id=candidate_id,
                    action_type=action_type,
                    contract_version=contract_version,
                    input_fingerprint=input_fingerprint,
                )
                if test is None:
                    raise PlaybookCandidateNotTested(
                        candidate_id,
                        "no dry run with the current contract version and this exact source "
                        "fingerprint has passed",
                    )
                playbook = Playbook(
                    id=playbook_id,
                    candidate_id=candidate.id,
                    name=candidate.name,
                    note=candidate.note,
                    action_type=candidate.source_action_type,
                    source_action_id=candidate.source_action_id,
                    source_execution_run_id=candidate.source_execution_run_id,
                    source_action_fingerprint=candidate.source_action_fingerprint,
                    replay_contract_version=test.contract_version,
                    promoted_from_test_id=test.id,
                    created_at=now,
                )
                connection.execute(
                    f"INSERT INTO playbooks ({_PLAYBOOK_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    _playbook_parameters(playbook),
                )
                cursor = connection.execute(
                    "UPDATE playbook_candidates SET status = ?, resolved_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        PlaybookCandidateStatus.PROMOTED.value,
                        to_utc_iso(now),
                        str(candidate.id),
                        PlaybookCandidateStatus.PENDING.value,
                    ),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise InvalidPlaybookCandidateTransition(
                        candidate.id,
                        candidate.status,
                        PlaybookCandidateStatus.PROMOTED,
                    )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not promote the candidate: {exc}") from exc
        return playbook

    def _reject_candidate_sync(
        self, candidate_id: PlaybookCandidateId, now: datetime
    ) -> PlaybookCandidate:
        try:
            with self._database.connect() as connection, transaction(connection):
                candidate = _require_pending_candidate(
                    connection, candidate_id, PlaybookCandidateStatus.REJECTED
                )
                cursor = connection.execute(
                    "UPDATE playbook_candidates SET status = ?, resolved_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        PlaybookCandidateStatus.REJECTED.value,
                        to_utc_iso(now),
                        str(candidate.id),
                        PlaybookCandidateStatus.PENDING.value,
                    ),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise InvalidPlaybookCandidateTransition(
                        candidate.id,
                        candidate.status,
                        PlaybookCandidateStatus.REJECTED,
                    )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not reject the candidate: {exc}") from exc
        return candidate.reject(now)

    def _get_playbook_sync(self, playbook_id: PlaybookId) -> Playbook | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PLAYBOOK_FIELDS} FROM playbooks WHERE id = ?",
                (str(playbook_id),),
            ).fetchone()
        return None if row is None else _row_to_playbook(row)

    def _list_playbooks_sync(
        self,
        statuses: tuple[PlaybookStatus, ...] | None,
        limit: int | None,
    ) -> list[Playbook]:
        statement = f"SELECT {_PLAYBOOK_FIELDS} FROM playbooks"
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
        return [_row_to_playbook(row) for row in rows]

    def _playbook_for_candidate_sync(
        self, candidate_id: PlaybookCandidateId
    ) -> Playbook | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PLAYBOOK_FIELDS} FROM playbooks WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
        return None if row is None else _row_to_playbook(row)

    def _retire_playbook_sync(self, playbook_id: PlaybookId, now: datetime) -> Playbook:
        try:
            with self._database.connect() as connection, transaction(connection):
                row = connection.execute(
                    f"SELECT {_PLAYBOOK_FIELDS} FROM playbooks WHERE id = ?",
                    (str(playbook_id),),
                ).fetchone()
                if row is None:
                    raise PlaybookNotFound(playbook_id)
                playbook = _row_to_playbook(row)
                if not playbook.is_active:
                    raise InvalidPlaybookTransition(
                        playbook.id, playbook.status, PlaybookStatus.RETIRED
                    )
                cursor = connection.execute(
                    "UPDATE playbooks SET status = ?, retired_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        PlaybookStatus.RETIRED.value,
                        to_utc_iso(now),
                        str(playbook.id),
                        PlaybookStatus.ACTIVE.value,
                    ),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise InvalidPlaybookTransition(
                        playbook.id, playbook.status, PlaybookStatus.RETIRED
                    )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not retire the playbook: {exc}") from exc
        return playbook.retire(now)


def _qualifying_test(
    connection: sqlite3.Connection,
    *,
    candidate_id: PlaybookCandidateId,
    action_type: str,
    contract_version: int,
    input_fingerprint: str,
) -> PlaybookReplayTest | None:
    """The newest passed dry run that binds this snapshot under this contract version."""
    row = connection.execute(
        f"SELECT {_TEST_FIELDS} FROM playbook_replay_tests "
        "WHERE candidate_id = ? AND action_type = ? AND contract_version = ? "
        "AND input_fingerprint = ? AND status = ? "
        "ORDER BY tested_at DESC, id DESC LIMIT 1",
        (
            str(candidate_id),
            action_type,
            contract_version,
            input_fingerprint,
            ReplayTestStatus.PASSED.value,
        ),
    ).fetchone()
    return None if row is None else _row_to_test(row)


def _require_pending_candidate(
    connection: sqlite3.Connection,
    candidate_id: PlaybookCandidateId,
    target: PlaybookCandidateStatus,
) -> PlaybookCandidate:
    """Load a candidate that may still move, or refuse the transition."""
    row = connection.execute(
        f"SELECT {_CANDIDATE_FIELDS} FROM playbook_candidates WHERE id = ?",
        (str(candidate_id),),
    ).fetchone()
    if row is None:
        raise PlaybookCandidateNotFound(candidate_id)
    candidate = _row_to_candidate(row)
    if not candidate.is_pending:
        raise InvalidPlaybookCandidateTransition(candidate.id, candidate.status, target)
    return candidate


def _is_unique_violation(error: sqlite3.IntegrityError, table: str) -> bool:
    message = str(error)
    return "UNIQUE" in message and table in message


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


def _candidate_parameters(candidate: PlaybookCandidate) -> tuple[object, ...]:
    return (
        str(candidate.id),
        candidate.name,
        candidate.note,
        str(candidate.source_action_id),
        str(candidate.source_execution_run_id),
        candidate.source_action_type.value,
        candidate.source_action_fingerprint,
        candidate.status.value,
        to_utc_iso(candidate.created_at),
        None if candidate.resolved_at is None else to_utc_iso(candidate.resolved_at),
    )


def _test_parameters(test: PlaybookReplayTest) -> tuple[object, ...]:
    return (
        str(test.id),
        str(test.candidate_id),
        test.action_type.value,
        test.contract_version,
        test.input_fingerprint,
        test.status.value,
        json.dumps(list(test.issue_codes), separators=(",", ":")),
        to_utc_iso(test.tested_at),
    )


def _playbook_parameters(playbook: Playbook) -> tuple[object, ...]:
    return (
        str(playbook.id),
        str(playbook.candidate_id),
        playbook.name,
        playbook.note,
        playbook.action_type.value,
        str(playbook.source_action_id),
        str(playbook.source_execution_run_id),
        playbook.source_action_fingerprint,
        playbook.replay_contract_version,
        str(playbook.promoted_from_test_id),
        playbook.status.value,
        to_utc_iso(playbook.created_at),
    )


def _row_to_candidate(row: sqlite3.Row) -> PlaybookCandidate:
    resolved = row["resolved_at"]
    return PlaybookCandidate(
        id=UUID(str(row["id"])),
        name=str(row["name"]),
        note=str(row["note"]),
        source_action_id=UUID(str(row["source_action_id"])),
        source_execution_run_id=UUID(str(row["source_execution_run_id"])),
        source_action_type=ActionType(str(row["source_action_type"])),
        source_action_fingerprint=str(row["source_action_fingerprint"]),
        status=PlaybookCandidateStatus(str(row["status"])),
        created_at=from_utc_iso(str(row["created_at"])),
        resolved_at=None if resolved is None else from_utc_iso(str(resolved)),
    )


def _row_to_test(row: sqlite3.Row) -> PlaybookReplayTest:
    codes = json.loads(str(row["issue_codes_json"]))
    return PlaybookReplayTest(
        id=UUID(str(row["id"])),
        candidate_id=UUID(str(row["candidate_id"])),
        action_type=ActionType(str(row["action_type"])),
        contract_version=int(row["contract_version"]),
        input_fingerprint=str(row["input_fingerprint"]),
        status=ReplayTestStatus(str(row["status"])),
        issue_codes=tuple(str(code) for code in codes),
        tested_at=from_utc_iso(str(row["tested_at"])),
    )


def _row_to_playbook(row: sqlite3.Row) -> Playbook:
    retired = row["retired_at"]
    return Playbook(
        id=UUID(str(row["id"])),
        candidate_id=UUID(str(row["candidate_id"])),
        name=str(row["name"]),
        note=str(row["note"]),
        action_type=ActionType(str(row["action_type"])),
        source_action_id=UUID(str(row["source_action_id"])),
        source_execution_run_id=UUID(str(row["source_execution_run_id"])),
        source_action_fingerprint=str(row["source_action_fingerprint"]),
        replay_contract_version=int(row["replay_contract_version"]),
        promoted_from_test_id=UUID(str(row["promoted_from_test_id"])),
        status=PlaybookStatus(str(row["status"])),
        created_at=from_utc_iso(str(row["created_at"])),
        retired_at=None if retired is None else from_utc_iso(str(retired)),
    )


__all__ = ["SqlitePlaybookRepository"]
