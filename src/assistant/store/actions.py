"""SQLite implementation of the ActionRepository port (ADR-0009, ADR-0023).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

Three operations carry the safety of this phase, and all three run inside one `BEGIN IMMEDIATE`
transaction:

- **`redeem_challenge`** — the token hash is compared, the expiry and single use are checked, the
  action's status and fingerprint are re-verified, an expired outstanding approval is superseded,
  the challenge is marked consumed and the approval is inserted. Any failure rolls the whole thing
  back, so a spent token can never exist without the approval it bought;
- **`begin_execution`** — the fingerprint is re-verified, an unresolved earlier attempt blocks the
  new one, the exact approval is consumed with a compare-and-set (`WHERE consumed_at IS NULL`) and
  the `RUNNING` run is inserted. Two callers racing here cannot both succeed: the loser's update
  matches no row;
- **`finish_execution`** — the run is closed and, on a definite success, the action becomes
  `EXECUTED` in the same transaction.

`payload_json` is the authority for the fingerprint. Every operation that authorises something
re-hashes the stored bytes instead of trusting the stored `fingerprint` column, so a tampered row
is refused rather than executed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from assistant.domain.action import (
    ActionRequest,
    ActionRequestId,
    ActionRequestStatus,
    ActionType,
)
from assistant.domain.approval import (
    ApprovalChallenge,
    ApprovalChallengeId,
    ApprovalId,
    ApprovalRecord,
)
from assistant.domain.case import CaseId
from assistant.domain.errors import (
    ActionExecutionUnresolved,
    ActionFingerprintMismatch,
    ActionNotExecutable,
    ActionRequestNotFound,
    AmbiguousId,
    ApprovalAlreadyOutstanding,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalChallengeNotFound,
    ApprovalUnavailable,
    DomainError,
    ExecutionRunNotFound,
    InvalidApprovalToken,
    InvalidExecutionRun,
)
from assistant.domain.execution import (
    ExecutionOutcome,
    ExecutionRun,
    ExecutionRunId,
    ExecutionRunStatus,
)
from assistant.ports.action_repository import ApprovalGrant, StartedExecution
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_ACTION_FIELDS = (
    "id, case_id, action_type, payload_json, fingerprint, status, created_at, executed_at, "
    "cancelled_at"
)
_CHALLENGE_FIELDS = (
    "id, action_id, action_fingerprint, token_hash, created_at, expires_at, consumed_at"
)
_APPROVAL_FIELDS = (
    "id, action_id, action_fingerprint, approved_at, expires_at, consumed_at, superseded_at"
)
_RUN_FIELDS = (
    "id, action_id, approval_id, status, started_at, finished_at, error_summary"
)

_UNRESOLVED_STATUSES = (ExecutionRunStatus.RUNNING.value, ExecutionRunStatus.UNKNOWN.value)


class SqliteActionRepository:
    """Durable actions, approval challenges, approvals and execution runs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_action(self, action: ActionRequest) -> ActionRequest:
        return await asyncio.to_thread(self._add_action_sync, action)

    async def get_action(self, action_id: ActionRequestId) -> ActionRequest | None:
        return await asyncio.to_thread(self._get_action_sync, action_id)

    async def list_actions(
        self, *, case_id: CaseId | None = None, limit: int | None = 20
    ) -> list[ActionRequest]:
        return await asyncio.to_thread(self._list_actions_sync, case_id, limit)

    async def resolve_action_id(self, reference: str) -> ActionRequestId:
        text = reference.strip().lower()
        if not text:
            raise ActionRequestNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        actions = await asyncio.to_thread(self._list_action_ids_sync)
        if candidate is not None:
            if candidate not in actions:
                raise ActionRequestNotFound(candidate)
            return candidate
        matching = [action_id for action_id in actions if str(action_id).startswith(text)]
        if not matching:
            raise ActionRequestNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    async def count_actions(
        self, *, statuses: Collection[ActionRequestStatus] | None = None
    ) -> int:
        return await asyncio.to_thread(
            self._count_actions_sync, None if statuses is None else tuple(statuses)
        )

    async def cancel_action(self, action: ActionRequest) -> ActionRequest:
        return await asyncio.to_thread(self._cancel_action_sync, action)

    async def add_challenge(self, challenge: ApprovalChallenge) -> ApprovalChallenge:
        return await asyncio.to_thread(self._add_challenge_sync, challenge)

    async def get_challenge(
        self, challenge_id: ApprovalChallengeId
    ) -> ApprovalChallenge | None:
        return await asyncio.to_thread(self._get_challenge_sync, challenge_id)

    async def latest_challenge(
        self, action_id: ActionRequestId
    ) -> ApprovalChallenge | None:
        return await asyncio.to_thread(self._latest_challenge_sync, action_id)

    async def redeem_challenge(
        self,
        *,
        challenge_id: ApprovalChallengeId,
        token_hash: str,
        action_id: ActionRequestId,
        action_fingerprint: str,
        approval_id: ApprovalId,
        now: datetime,
    ) -> ApprovalGrant:
        return await asyncio.to_thread(
            self._redeem_challenge_sync,
            challenge_id,
            token_hash,
            action_id,
            action_fingerprint,
            approval_id,
            now,
        )

    async def list_approvals(self, action_id: ActionRequestId) -> list[ApprovalRecord]:
        return await asyncio.to_thread(self._list_approvals_sync, action_id)

    async def latest_approval(self, action_id: ActionRequestId) -> ApprovalRecord | None:
        return await asyncio.to_thread(self._latest_approval_sync, action_id)

    async def begin_execution(
        self,
        *,
        action_id: ActionRequestId,
        action_fingerprint: str,
        run_id: ExecutionRunId,
        now: datetime,
    ) -> StartedExecution:
        return await asyncio.to_thread(
            self._begin_execution_sync, action_id, action_fingerprint, run_id, now
        )

    async def finish_execution(
        self,
        *,
        run_id: ExecutionRunId,
        outcome: ExecutionOutcome,
        at: datetime,
    ) -> ExecutionRun:
        return await asyncio.to_thread(
            self._finish_execution_sync, run_id, outcome, at
        )

    async def get_execution(self, run_id: ExecutionRunId) -> ExecutionRun | None:
        return await asyncio.to_thread(self._get_execution_sync, run_id)

    async def list_executions(self, action_id: ActionRequestId) -> list[ExecutionRun]:
        return await asyncio.to_thread(self._list_executions_sync, action_id)

    async def latest_execution(self, action_id: ActionRequestId) -> ExecutionRun | None:
        return await asyncio.to_thread(self._latest_execution_sync, action_id)

    async def count_executions(self, action_id: ActionRequestId) -> int:
        return await asyncio.to_thread(self._count_executions_sync, action_id)

    # ------------------------------------------------------------------ actions

    def _add_action_sync(self, action: ActionRequest) -> ActionRequest:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO action_requests ({_ACTION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    action_parameters(action),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store an action request: {exc}") from exc
        return action

    def _get_action_sync(self, action_id: ActionRequestId) -> ActionRequest | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_ACTION_FIELDS} FROM action_requests WHERE id = ?", (str(action_id),)
            ).fetchone()
        return None if row is None else _row_to_action(row)

    def _list_actions_sync(
        self, case_id: CaseId | None, limit: int | None
    ) -> list[ActionRequest]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = f"SELECT {_ACTION_FIELDS} FROM action_requests"
        parameters: list[object] = []
        if case_id is not None:
            statement += " WHERE case_id = ?"
            parameters.append(str(case_id))
        statement += " ORDER BY created_at DESC, id"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_action(row) for row in rows]

    def _list_action_ids_sync(self) -> list[ActionRequestId]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM action_requests ORDER BY id"
            ).fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _count_actions_sync(
        self, statuses: tuple[ActionRequestStatus, ...] | None
    ) -> int:
        statement = "SELECT count(*) AS total FROM action_requests"
        parameters: tuple[object, ...] = ()
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            statement += f" WHERE status IN ({placeholders})"
            parameters = tuple(str(status) for status in statuses)
        with self._database.connect() as connection:
            row = connection.execute(statement, parameters).fetchone()
        return int(row["total"])

    def _cancel_action_sync(self, action: ActionRequest) -> ActionRequest:
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE action_requests SET status = ?, cancelled_at = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        str(action.status),
                        None if action.cancelled_at is None else to_utc_iso(action.cancelled_at),
                        str(action.id),
                        ActionRequestStatus.PREPARED.value,
                    ),
                )
                if cursor.rowcount == 0:
                    raise _not_prepared(connection, action.id)
                row = connection.execute(
                    f"SELECT {_ACTION_FIELDS} FROM action_requests WHERE id = ?",
                    (str(action.id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not cancel an action: {exc}") from exc
        if row is None:  # pragma: no cover - the update guarantees a row
            raise CommitmentStoreError(f"action {action.id} vanished after being cancelled")
        return _row_to_action(row)

    # ---------------------------------------------------------------- approvals

    def _add_challenge_sync(self, challenge: ApprovalChallenge) -> ApprovalChallenge:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO approval_challenges ({_CHALLENGE_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(challenge.id),
                        str(challenge.action_id),
                        challenge.action_fingerprint,
                        challenge.token_hash,
                        to_utc_iso(challenge.created_at),
                        to_utc_iso(challenge.expires_at),
                        None
                        if challenge.consumed_at is None
                        else to_utc_iso(challenge.consumed_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store an approval challenge: {exc}") from exc
        return challenge

    def _get_challenge_sync(
        self, challenge_id: ApprovalChallengeId
    ) -> ApprovalChallenge | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CHALLENGE_FIELDS} FROM approval_challenges WHERE id = ?",
                (str(challenge_id),),
            ).fetchone()
        return None if row is None else _row_to_challenge(row)

    def _latest_challenge_sync(
        self, action_id: ActionRequestId
    ) -> ApprovalChallenge | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_CHALLENGE_FIELDS} FROM approval_challenges WHERE action_id = ? "
                # rowid, not id: "latest" must mean the most recently created row, and
                # two challenges can share a timestamp (a UUID tie-break is random).
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(action_id),),
            ).fetchone()
        return None if row is None else _row_to_challenge(row)

    def _redeem_challenge_sync(
        self,
        challenge_id: ApprovalChallengeId,
        token_hash: str,
        action_id: ActionRequestId,
        action_fingerprint: str,
        approval_id: ApprovalId,
        now: datetime,
    ) -> ApprovalGrant:
        superseded = 0
        try:
            with self._database.connect() as connection, transaction(connection):
                challenge_row = connection.execute(
                    f"SELECT {_CHALLENGE_FIELDS} FROM approval_challenges WHERE id = ?",
                    (str(challenge_id),),
                ).fetchone()
                if challenge_row is None:
                    raise ApprovalChallengeNotFound(challenge_id)
                challenge = _row_to_challenge(challenge_row)
                if challenge.is_consumed():
                    raise ApprovalChallengeConsumed(challenge_id)
                if challenge.is_expired(now):
                    raise ApprovalChallengeExpired(challenge_id)
                if challenge.token_hash != token_hash:
                    # The presented secret is never echoed, logged or stored.
                    raise InvalidApprovalToken(
                        "the presented token does not match the challenge"
                    )
                if challenge.action_id != action_id:
                    raise ApprovalChallengeNotFound(challenge_id)

                action_row = connection.execute(
                    f"SELECT {_ACTION_FIELDS} FROM action_requests WHERE id = ?",
                    (str(action_id),),
                ).fetchone()
                if action_row is None:
                    raise ActionRequestNotFound(action_id)
                stored = _row_to_action(action_row)
                _require_matching_fingerprint(stored, action_fingerprint)
                _require_prepared(stored)

                superseded = _supersede_expired_approvals(connection, action_id, now)
                active = connection.execute(
                    "SELECT 1 FROM approvals WHERE action_id = ? AND consumed_at IS NULL "
                    "AND superseded_at IS NULL LIMIT 1",
                    (str(action_id),),
                ).fetchone()
                if active is not None:
                    raise ApprovalAlreadyOutstanding(action_id)

                cursor = connection.execute(
                    "UPDATE approval_challenges SET consumed_at = ? "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (to_utc_iso(now), str(challenge_id)),
                )
                if cursor.rowcount == 0:  # pragma: no cover - the row was read in this transaction
                    raise ApprovalChallengeConsumed(challenge_id)

                approval = ApprovalRecord(
                    id=approval_id,
                    action_id=action_id,
                    action_fingerprint=action_fingerprint,
                    approved_at=now,
                    expires_at=challenge.expires_at,
                )
                connection.execute(
                    f"INSERT INTO approvals ({_APPROVAL_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    _approval_parameters(approval),
                )
        except sqlite3.IntegrityError as exc:
            # The partial unique index is the database's half of "one live approval per action".
            if "approvals_active_idx" in str(exc) or (
                "UNIQUE" in str(exc) and "approvals" in str(exc)
            ):
                raise ApprovalAlreadyOutstanding(action_id) from exc
            raise CommitmentStoreError(f"could not record an approval: {exc}") from exc
        return ApprovalGrant(approval=approval, superseded=superseded)

    def _list_approvals_sync(self, action_id: ActionRequestId) -> list[ApprovalRecord]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_APPROVAL_FIELDS} FROM approvals WHERE action_id = ? "
                "ORDER BY approved_at, id",
                (str(action_id),),
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def _latest_approval_sync(self, action_id: ActionRequestId) -> ApprovalRecord | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_APPROVAL_FIELDS} FROM approvals WHERE action_id = ? "
                "ORDER BY approved_at DESC, rowid DESC LIMIT 1",
                (str(action_id),),
            ).fetchone()
        return None if row is None else _row_to_approval(row)

    # --------------------------------------------------------------- executions

    def _begin_execution_sync(
        self,
        action_id: ActionRequestId,
        action_fingerprint: str,
        run_id: ExecutionRunId,
        now: datetime,
    ) -> StartedExecution:
        try:
            with self._database.connect() as connection, transaction(connection):
                action_row = connection.execute(
                    f"SELECT {_ACTION_FIELDS} FROM action_requests WHERE id = ?",
                    (str(action_id),),
                ).fetchone()
                if action_row is None:
                    raise ActionRequestNotFound(action_id)
                action = _row_to_action(action_row)
                _require_matching_fingerprint(action, action_fingerprint)
                _require_prepared(action)

                unresolved = connection.execute(
                    "SELECT status FROM execution_runs WHERE action_id = ? AND status IN (?, ?) "
                    "ORDER BY started_at DESC LIMIT 1",
                    (str(action_id), *_UNRESOLVED_STATUSES),
                ).fetchone()
                if unresolved is not None:
                    raise ActionExecutionUnresolved(action_id, str(unresolved["status"]))

                approval_row = connection.execute(
                    f"SELECT {_APPROVAL_FIELDS} FROM approvals "
                    "WHERE action_id = ? AND consumed_at IS NULL AND superseded_at IS NULL "
                    "ORDER BY approved_at DESC, rowid DESC LIMIT 1",
                    (str(action_id),),
                ).fetchone()
                if approval_row is None:
                    raise ApprovalUnavailable(
                        f"no usable approval exists for action request {action_id}"
                    )
                approval = _row_to_approval(approval_row)
                if not approval.matches_fingerprint(action_fingerprint):
                    raise ApprovalUnavailable(
                        "the outstanding approval is for different action content"
                    )
                if approval.is_expired(now):
                    raise ApprovalUnavailable(
                        f"the approval for action request {action_id} expired at "
                        f"{approval.expires_at}"
                    )

                cursor = connection.execute(
                    "UPDATE approvals SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL "
                    "AND superseded_at IS NULL",
                    (to_utc_iso(now), str(approval.id)),
                )
                if cursor.rowcount == 0:
                    # Another caller consumed it between the read and the write.
                    raise ApprovalUnavailable(
                        f"the approval for action request {action_id} was already consumed"
                    )
                run = ExecutionRun(
                    id=run_id,
                    action_id=action_id,
                    approval_id=approval.id,
                    started_at=now,
                )
                connection.execute(
                    f"INSERT INTO execution_runs ({_RUN_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    _run_parameters(run),
                )
                consumed_row = connection.execute(
                    f"SELECT {_APPROVAL_FIELDS} FROM approvals WHERE id = ?",
                    (str(approval.id),),
                ).fetchone()
                if consumed_row is None:  # pragma: no cover - the update guarantees a row
                    raise CommitmentStoreError(
                        f"approval {approval.id} vanished while being consumed"
                    )
                consumed = _row_to_approval(consumed_row)
        except sqlite3.IntegrityError as exc:
            if "execution_runs_running_idx" in str(exc) or (
                "UNIQUE" in str(exc) and "execution_runs" in str(exc)
            ):
                raise ActionExecutionUnresolved(
                    action_id, ExecutionRunStatus.RUNNING.value
                ) from exc
            raise CommitmentStoreError(f"could not start an execution: {exc}") from exc
        return StartedExecution(run=run, approval=consumed)

    def _finish_execution_sync(
        self, run_id: ExecutionRunId, outcome: ExecutionOutcome, at: datetime
    ) -> ExecutionRun:
        try:
            with self._database.connect() as connection, transaction(connection):
                row = connection.execute(
                    f"SELECT {_RUN_FIELDS} FROM execution_runs WHERE id = ?", (str(run_id),)
                ).fetchone()
                if row is None:
                    raise ExecutionRunNotFound(run_id)
                run = _row_to_run(row).finish(outcome, at=at)
                cursor = connection.execute(
                    "UPDATE execution_runs SET status = ?, finished_at = ?, error_summary = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        str(run.status),
                        None if run.finished_at is None else to_utc_iso(run.finished_at),
                        run.error_summary,
                        str(run.id),
                        ExecutionRunStatus.RUNNING.value,
                    ),
                )
                if cursor.rowcount == 0:
                    raise InvalidExecutionRun(
                        f"execution {run_id} is no longer RUNNING"
                    )
                if run.status is ExecutionRunStatus.SUCCEEDED:
                    # The action becomes EXECUTED in the same transaction as the success.
                    connection.execute(
                        "UPDATE action_requests SET status = ?, executed_at = ? "
                        "WHERE id = ? AND status = ?",
                        (
                            ActionRequestStatus.EXECUTED.value,
                            to_utc_iso(at),
                            str(run.action_id),
                            ActionRequestStatus.PREPARED.value,
                        ),
                    )
                stored = connection.execute(
                    f"SELECT {_RUN_FIELDS} FROM execution_runs WHERE id = ?", (str(run_id),)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not finish an execution: {exc}") from exc
        if stored is None:  # pragma: no cover - the update guarantees a row
            raise CommitmentStoreError(f"execution {run_id} vanished after being finished")
        return _row_to_run(stored)

    def _get_execution_sync(self, run_id: ExecutionRunId) -> ExecutionRun | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_RUN_FIELDS} FROM execution_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def _list_executions_sync(self, action_id: ActionRequestId) -> list[ExecutionRun]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_RUN_FIELDS} FROM execution_runs WHERE action_id = ? "
                "ORDER BY started_at, id",
                (str(action_id),),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def _latest_execution_sync(self, action_id: ActionRequestId) -> ExecutionRun | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_RUN_FIELDS} FROM execution_runs WHERE action_id = ? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (str(action_id),),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def _count_executions_sync(self, action_id: ActionRequestId) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM execution_runs WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
        return int(row["total"])


def _supersede_expired_approvals(
    connection: sqlite3.Connection, action_id: ActionRequestId, now: datetime
) -> int:
    """Retire approvals that are still unconsumed but already expired.

    Retired, not deleted: the audit trail keeps every decision, while the partial unique index
    stops counting a spent decision as outstanding authority.
    """
    cursor = connection.execute(
        "UPDATE approvals SET superseded_at = ? "
        "WHERE action_id = ? AND consumed_at IS NULL AND superseded_at IS NULL AND expires_at <= ?",
        (to_utc_iso(now), str(action_id), to_utc_iso(now)),
    )
    return cursor.rowcount


def _require_prepared(action: ActionRequest) -> None:
    if not action.is_prepared:
        raise ActionNotExecutable(
            f"action request {action.id} is {action.status}, not prepared"
        )


def _require_matching_fingerprint(action: ActionRequest, expected: str) -> None:
    """Re-hash the stored payload; never trust the stored fingerprint column alone."""
    if not action.fingerprint_matches():
        raise ActionFingerprintMismatch(action.id)
    if action.fingerprint != expected:
        raise ActionFingerprintMismatch(action.id)


def _not_prepared(connection: sqlite3.Connection, action_id: ActionRequestId) -> Exception:
    row = connection.execute(
        "SELECT status FROM action_requests WHERE id = ?", (str(action_id),)
    ).fetchone()
    if row is None:
        raise ActionRequestNotFound(action_id)
    raise ActionNotExecutable(
        f"action request {action_id} is {row['status']}, not prepared"
    )


def action_parameters(action: ActionRequest) -> tuple[object, ...]:
    return (
        str(action.id),
        str(action.case_id),
        action.action_type.value,
        action.payload_json,
        action.fingerprint,
        str(action.status),
        to_utc_iso(action.created_at),
        None if action.executed_at is None else to_utc_iso(action.executed_at),
        None if action.cancelled_at is None else to_utc_iso(action.cancelled_at),
    )


def _approval_parameters(approval: ApprovalRecord) -> tuple[object, ...]:
    return (
        str(approval.id),
        str(approval.action_id),
        approval.action_fingerprint,
        to_utc_iso(approval.approved_at),
        to_utc_iso(approval.expires_at),
        None if approval.consumed_at is None else to_utc_iso(approval.consumed_at),
        None if approval.superseded_at is None else to_utc_iso(approval.superseded_at),
    )


def _run_parameters(run: ExecutionRun) -> tuple[object, ...]:
    return (
        str(run.id),
        str(run.action_id),
        str(run.approval_id),
        str(run.status),
        to_utc_iso(run.started_at),
        None if run.finished_at is None else to_utc_iso(run.finished_at),
        run.error_summary,
    )


def _row_to_action(row: sqlite3.Row) -> ActionRequest:
    try:
        executed = row["executed_at"]
        cancelled = row["cancelled_at"]
        return ActionRequest(
            id=UUID(str(row["id"])),
            case_id=UUID(str(row["case_id"])),
            action_type=ActionType(str(row["action_type"])),
            payload_json=str(row["payload_json"]),
            fingerprint=str(row["fingerprint"]),
            status=ActionRequestStatus(str(row["status"])),
            created_at=from_utc_iso(str(row["created_at"])),
            executed_at=None if executed is None else from_utc_iso(str(executed)),
            cancelled_at=None if cancelled is None else from_utc_iso(str(cancelled)),
        )
    except (ValueError, KeyError, DomainError) as exc:
        # A row whose payload and fingerprint disagree is corrupt storage, not a domain mistake:
        # the entity refuses to exist, and the caller is told the store cannot be trusted.
        raise CommitmentStoreError(f"stored action request is not readable: {exc}") from exc


def _row_to_challenge(row: sqlite3.Row) -> ApprovalChallenge:
    consumed = row["consumed_at"]
    return ApprovalChallenge(
        id=UUID(str(row["id"])),
        action_id=UUID(str(row["action_id"])),
        action_fingerprint=str(row["action_fingerprint"]),
        token_hash=str(row["token_hash"]),
        created_at=from_utc_iso(str(row["created_at"])),
        expires_at=from_utc_iso(str(row["expires_at"])),
        consumed_at=None if consumed is None else from_utc_iso(str(consumed)),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
    consumed = row["consumed_at"]
    superseded = row["superseded_at"]
    return ApprovalRecord(
        id=UUID(str(row["id"])),
        action_id=UUID(str(row["action_id"])),
        action_fingerprint=str(row["action_fingerprint"]),
        approved_at=from_utc_iso(str(row["approved_at"])),
        expires_at=from_utc_iso(str(row["expires_at"])),
        consumed_at=None if consumed is None else from_utc_iso(str(consumed)),
        superseded_at=None if superseded is None else from_utc_iso(str(superseded)),
    )


def _row_to_run(row: sqlite3.Row) -> ExecutionRun:
    finished = row["finished_at"]
    summary = row["error_summary"]
    return ExecutionRun(
        id=UUID(str(row["id"])),
        action_id=UUID(str(row["action_id"])),
        approval_id=UUID(str(row["approval_id"])),
        status=ExecutionRunStatus(str(row["status"])),
        started_at=from_utc_iso(str(row["started_at"])),
        finished_at=None if finished is None else from_utc_iso(str(finished)),
        error_summary=None if summary is None else str(summary),
    )


__all__ = ["SqliteActionRepository", "action_parameters"]
