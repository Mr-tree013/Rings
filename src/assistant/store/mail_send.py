"""SQLite implementation of the send links and reconciliation history (ADR-0024).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

Two transactions carry the safety of this phase:

- **`add_action_with_link`** writes the approved action and its link together. The link's unique
  constraints (`(draft_id, draft_version)` and `rfc_message_id`) are the database's half of "one
  version, one action, one Message-ID", so a second prepare cannot quietly produce a second letter
  from the same approved text;
- **`resolve_to_succeeded`** finishes the run, marks the action `EXECUTED` and appends the audit
  row in one go. A crash can therefore leave the attempt unresolved, but never a resolution that
  was not recorded or a recorded resolution that did not happen.
"""

from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID

from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.execution import ExecutionRunStatus
from assistant.domain.mail_draft import MailDraftId
from assistant.domain.mail_send import (
    MailSendLink,
    MailSendReconciliation,
    MailSendReconciliationResult,
)
from assistant.domain.new_mail_draft import NewMailDraftId
from assistant.store.actions import action_parameters
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_LINK_FIELDS = (
    "action_id, draft_id, new_draft_id, draft_version, rfc_message_id, created_at"
)
_RECONCILIATION_FIELDS = (
    "id, action_id, execution_run_id, result, checked_at, mailbox_name, uidvalidity, uid"
)


class SqliteMailSendRepository:
    """Durable send links and Sent-folder reconciliation history."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_action_with_link(
        self, action: ActionRequest, link: MailSendLink
    ) -> ActionRequest:
        return await asyncio.to_thread(self._add_action_with_link_sync, action, link)

    async def get_link(self, action_id: ActionRequestId) -> MailSendLink | None:
        return await asyncio.to_thread(self._get_link_sync, action_id)

    async def get_link_for_draft_version(
        self, draft_id: MailDraftId, draft_version: int
    ) -> MailSendLink | None:
        return await asyncio.to_thread(
            self._get_link_for_draft_version_sync, draft_id, draft_version
        )

    async def get_link_for_new_draft_version(
        self, draft_id: NewMailDraftId, draft_version: int
    ) -> MailSendLink | None:
        return await asyncio.to_thread(
            self._get_link_for_new_draft_version_sync, draft_id, draft_version
        )

    async def list_links(self, *, limit: int | None = 20) -> list[MailSendLink]:
        return await asyncio.to_thread(self._list_links_sync, limit)

    async def count_links(self) -> int:
        return await asyncio.to_thread(self._count_links_sync)

    async def record_reconciliation(
        self, reconciliation: MailSendReconciliation
    ) -> MailSendReconciliation:
        return await asyncio.to_thread(self._record_reconciliation_sync, reconciliation)

    async def list_reconciliations(
        self, action_id: ActionRequestId
    ) -> list[MailSendReconciliation]:
        return await asyncio.to_thread(self._list_reconciliations_sync, action_id)

    async def latest_reconciliation(
        self, action_id: ActionRequestId
    ) -> MailSendReconciliation | None:
        return await asyncio.to_thread(self._latest_reconciliation_sync, action_id)

    async def resolve_to_succeeded(
        self,
        *,
        action_id: ActionRequestId,
        execution_run_id: UUID,
        reconciliation: MailSendReconciliation,
    ) -> MailSendReconciliation:
        return await asyncio.to_thread(
            self._resolve_to_succeeded_sync, action_id, execution_run_id, reconciliation
        )

    # ------------------------------------------------------------ blocking internals

    def _add_action_with_link_sync(
        self, action: ActionRequest, link: MailSendLink
    ) -> ActionRequest:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    "INSERT INTO action_requests (id, case_id, action_type, payload_json, "
                    "fingerprint, status, created_at, executed_at, cancelled_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    action_parameters(action),
                )
                connection.execute(
                    f"INSERT INTO mail_send_links ({_LINK_FIELDS}) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(link.action_id),
                        None if link.draft_id is None else str(link.draft_id),
                        None if link.new_draft_id is None else str(link.new_draft_id),
                        link.draft_version,
                        link.rfc_message_id,
                        to_utc_iso(link.created_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not store a mail send action: {exc}"
            ) from exc
        return action

    def _get_link_sync(self, action_id: ActionRequestId) -> MailSendLink | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_LINK_FIELDS} FROM mail_send_links WHERE action_id = ?",
                (str(action_id),),
            ).fetchone()
        return None if row is None else _row_to_link(row)

    def _get_link_for_draft_version_sync(
        self, draft_id: MailDraftId, draft_version: int
    ) -> MailSendLink | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_LINK_FIELDS} FROM mail_send_links WHERE draft_id = ? "
                "AND draft_version = ?",
                (str(draft_id), draft_version),
            ).fetchone()
        return None if row is None else _row_to_link(row)

    def _get_link_for_new_draft_version_sync(
        self, draft_id: NewMailDraftId, draft_version: int
    ) -> MailSendLink | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_LINK_FIELDS} FROM mail_send_links WHERE new_draft_id = ? "
                "AND draft_version = ?",
                (str(draft_id), draft_version),
            ).fetchone()
        return None if row is None else _row_to_link(row)

    def _list_links_sync(self, limit: int | None) -> list[MailSendLink]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        statement = (
            f"SELECT {_LINK_FIELDS} FROM mail_send_links ORDER BY created_at DESC, action_id"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_link(row) for row in rows]

    def _count_links_sync(self) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM mail_send_links"
            ).fetchone()
        return int(row["total"])

    def _record_reconciliation_sync(
        self, reconciliation: MailSendReconciliation
    ) -> MailSendReconciliation:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO mail_send_reconciliations ({_RECONCILIATION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _reconciliation_parameters(reconciliation),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not record a send reconciliation: {exc}"
            ) from exc
        return reconciliation

    def _list_reconciliations_sync(
        self, action_id: ActionRequestId
    ) -> list[MailSendReconciliation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_RECONCILIATION_FIELDS} FROM mail_send_reconciliations "
                "WHERE action_id = ? ORDER BY checked_at, id",
                (str(action_id),),
            ).fetchall()
        return [_row_to_reconciliation(row) for row in rows]

    def _latest_reconciliation_sync(
        self, action_id: ActionRequestId
    ) -> MailSendReconciliation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_RECONCILIATION_FIELDS} FROM mail_send_reconciliations "
                "WHERE action_id = ? ORDER BY checked_at DESC, rowid DESC LIMIT 1",
                (str(action_id),),
            ).fetchone()
        return None if row is None else _row_to_reconciliation(row)

    def _resolve_to_succeeded_sync(
        self,
        action_id: ActionRequestId,
        execution_run_id: UUID,
        reconciliation: MailSendReconciliation,
    ) -> MailSendReconciliation:
        if reconciliation.result is not MailSendReconciliationResult.FOUND:
            raise ValueError("only a FOUND reconciliation resolves an attempt")
        try:
            with self._database.connect() as connection, transaction(connection):
                run_row = connection.execute(
                    "SELECT action_id, status FROM execution_runs WHERE id = ?",
                    (str(execution_run_id),),
                ).fetchone()
                if run_row is None:
                    raise CommitmentStoreError(
                        f"execution run {execution_run_id} does not exist"
                    )
                if str(run_row["action_id"]) != str(action_id):
                    raise CommitmentStoreError(
                        "the execution run does not belong to this action"
                    )
                # The evidence lives in the audit row below; a resolved run carries no error.
                cursor = connection.execute(
                    "UPDATE execution_runs SET status = ?, finished_at = ?, error_summary = NULL "
                    "WHERE id = ? AND status IN (?, ?)",
                    (
                        ExecutionRunStatus.SUCCEEDED.value,
                        to_utc_iso(reconciliation.checked_at),
                        str(execution_run_id),
                        ExecutionRunStatus.RUNNING.value,
                        ExecutionRunStatus.UNKNOWN.value,
                    ),
                )
                if cursor.rowcount == 0:
                    raise CommitmentStoreError(
                        f"execution run {execution_run_id} is not unresolved"
                    )
                connection.execute(
                    "UPDATE action_requests SET status = 'executed', executed_at = ? "
                    "WHERE id = ? AND status = 'prepared'",
                    (to_utc_iso(reconciliation.checked_at), str(action_id)),
                )
                connection.execute(
                    f"INSERT INTO mail_send_reconciliations ({_RECONCILIATION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _reconciliation_parameters(reconciliation),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not resolve a send reconciliation: {exc}"
            ) from exc
        return reconciliation


def _reconciliation_parameters(
    reconciliation: MailSendReconciliation,
) -> tuple[object, ...]:
    return (
        str(reconciliation.id),
        str(reconciliation.action_id),
        str(reconciliation.execution_run_id),
        reconciliation.result.value,
        to_utc_iso(reconciliation.checked_at),
        reconciliation.mailbox_name,
        reconciliation.uidvalidity,
        reconciliation.uid,
    )


def _row_to_link(row: sqlite3.Row) -> MailSendLink:
    try:
        return MailSendLink(
            action_id=UUID(str(row["action_id"])),
            draft_id=None if row["draft_id"] is None else UUID(str(row["draft_id"])),
            new_draft_id=(
                None if row["new_draft_id"] is None else UUID(str(row["new_draft_id"]))
            ),
            draft_version=int(row["draft_version"]),
            rfc_message_id=str(row["rfc_message_id"]),
            created_at=from_utc_iso(str(row["created_at"])),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(f"stored mail send link is not readable: {exc}") from exc


def _row_to_reconciliation(row: sqlite3.Row) -> MailSendReconciliation:
    try:
        return MailSendReconciliation(
            id=UUID(str(row["id"])),
            action_id=UUID(str(row["action_id"])),
            execution_run_id=UUID(str(row["execution_run_id"])),
            result=MailSendReconciliationResult(str(row["result"])),
            checked_at=from_utc_iso(str(row["checked_at"])),
            mailbox_name=None
            if row["mailbox_name"] is None
            else str(row["mailbox_name"]),
            uidvalidity=None if row["uidvalidity"] is None else int(row["uidvalidity"]),
            uid=None if row["uid"] is None else int(row["uid"]),
        )
    except (ValueError, KeyError) as exc:
        raise CommitmentStoreError(
            f"stored send reconciliation is not readable: {exc}"
        ) from exc


__all__ = ["SqliteMailSendRepository"]
