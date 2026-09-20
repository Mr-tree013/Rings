"""SQLite implementation of the PlanningRepository port (ADR-0009, ADR-0015).

Two properties live here:

- `load_snapshot` reads every planner-relevant table inside one deferred transaction, so a
  proposal can never be built from a mix of revisions;
- `apply_proposal` performs the whole replacement — cancel old planner blocks, insert the new
  ones, mark the proposal applied, bump the revision — inside one `BEGIN IMMEDIATE`
  transaction, and commits its stale marking rather than rolling it back.

Manual plan blocks are never touched: the replacement statement filters on
`origin = 'planner'`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    AmbiguousId,
    DomainError,
    PlanningSnapshotChanged,
    PlanProposalNotFound,
)
from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin
from assistant.domain.planning import (
    PlanningIssue,
    PlanningIssueCode,
    PlanningSnapshot,
    PlanningWindow,
    PlanProposal,
    PlanProposalDetail,
    PlanProposalId,
    PlanProposalStatus,
    PlanProposalSummary,
    ProposedPlanBlock,
)
from assistant.domain.task import Task, TaskPriority, TaskStatus
from assistant.ports.planning_repository import ApplyOutcome, ApplyResult
from assistant.store.commitment_revision import increment_revision, read_revision
from assistant.store.db import Database, read_transaction, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_PLAN_BLOCK_FIELDS = (
    "id, task_id, starts_at, ends_at, created_at, updated_at, cancelled_at, origin, proposal_id"
)

_PROPOSAL_FIELDS = (
    "id, status, window_start, window_end, timezone, input_fingerprint, input_revision, "
    "created_at, applied_at, superseded_at"
)

_TASK_FIELDS = (
    "id, title, description, status, priority, estimated_minutes, created_at, updated_at, "
    "completed_at, cancelled_at"
)


class SqlitePlanningRepository:
    """Durable proposals plus the atomic apply, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load_snapshot(self, window: PlanningWindow) -> PlanningSnapshot:
        return await asyncio.to_thread(self._load_snapshot_sync, window)

    async def get_current_revision(self) -> int:
        return await asyncio.to_thread(self._current_revision_sync)

    async def create_proposal(
        self,
        proposal: PlanProposal,
        *,
        blocks: Sequence[ProposedPlanBlock],
        issues: Sequence[PlanningIssue],
    ) -> PlanProposal:
        return await asyncio.to_thread(
            self._create_proposal_sync, proposal, tuple(blocks), tuple(issues)
        )

    async def get_proposal(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        return await asyncio.to_thread(self._get_proposal_sync, proposal_id)

    async def get_proposal_detail(
        self, proposal_id: PlanProposalId
    ) -> PlanProposalDetail | None:
        return await asyncio.to_thread(self._get_proposal_detail_sync, proposal_id)

    async def list_proposals(self, *, limit: int | None = 20) -> list[PlanProposal]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_proposals_sync, limit)

    async def list_proposal_summaries(
        self, *, limit: int | None = 20
    ) -> list[PlanProposalSummary]:
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await asyncio.to_thread(self._list_proposal_summaries_sync, limit)

    async def mark_stale(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        return await asyncio.to_thread(self._mark_stale_sync, proposal_id)

    async def apply_proposal(
        self, proposal_id: PlanProposalId, *, applied_at: datetime
    ) -> ApplyResult:
        return await asyncio.to_thread(self._apply_proposal_sync, proposal_id, applied_at)

    async def resolve_proposal_id(self, reference: str) -> PlanProposalId:
        text = reference.strip().lower()
        if not text:
            raise PlanProposalNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        proposals = await self.list_proposals(limit=None)
        if candidate is not None:
            if all(proposal.id != candidate for proposal in proposals):
                raise PlanProposalNotFound(candidate)
            return candidate
        matching = [proposal.id for proposal in proposals if str(proposal.id).startswith(text)]
        if not matching:
            raise PlanProposalNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    # ------------------------------------------------------------ blocking internals

    def _current_revision_sync(self) -> int:
        with self._database.connect() as connection:
            return read_revision(connection)

    def _load_snapshot_sync(self, window: PlanningWindow) -> PlanningSnapshot:
        with self._database.connect() as connection, read_transaction(connection):
            revision = read_revision(connection)
            task_rows = connection.execute(
                f"SELECT {_TASK_FIELDS} FROM tasks WHERE status = ? ORDER BY created_at, id",
                (str(TaskStatus.OPEN),),
            ).fetchall()
            tasks = tuple(_row_to_task(row) for row in task_rows)
            task_ids = [str(task.id) for task in tasks]
            deadlines: dict[UUID, Deadline] = {}
            actual_seconds: dict[UUID, int] = {}
            if task_ids:
                placeholders = ", ".join("?" for _ in task_ids)
                for row in connection.execute(
                    "SELECT id, task_id, due_at, created_at, updated_at FROM deadlines "
                    f"WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                ):
                    deadline = _row_to_deadline(row)
                    deadlines[deadline.task_id] = deadline
                for row in connection.execute(
                    "SELECT task_id, started_at, ended_at FROM work_sessions "
                    f"WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                ):
                    task_id = UUID(str(row["task_id"]))
                    started = from_utc_iso(str(row["started_at"]))
                    ended = from_utc_iso(str(row["ended_at"]))
                    actual_seconds[task_id] = actual_seconds.get(task_id, 0) + int(
                        (ended - started).total_seconds()
                    )
            events = tuple(
                _row_to_calendar_event(row)
                for row in connection.execute(
                    "SELECT id, title, description, starts_at, ends_at, created_at, updated_at, "
                    "cancelled_at FROM calendar_events "
                    "WHERE cancelled_at IS NULL AND starts_at < ? AND ends_at > ? "
                    "ORDER BY starts_at, id",
                    (to_utc_iso(window.ends_at), to_utc_iso(window.starts_at)),
                )
            )
            manual_blocks = tuple(
                _row_to_plan_block(row)
                for row in connection.execute(
                    f"SELECT {_PLAN_BLOCK_FIELDS} FROM plan_blocks "
                    "WHERE cancelled_at IS NULL AND origin = ? AND starts_at < ? AND ends_at > ? "
                    "ORDER BY starts_at, id",
                    (
                        str(PlanBlockOrigin.MANUAL),
                        to_utc_iso(window.ends_at),
                        to_utc_iso(window.starts_at),
                    ),
                )
            )
            planner_blocks = tuple(
                _row_to_plan_block(row)
                for row in connection.execute(
                    f"SELECT {_PLAN_BLOCK_FIELDS} FROM plan_blocks "
                    "WHERE cancelled_at IS NULL AND origin = ? AND starts_at < ? AND ends_at > ? "
                    "ORDER BY starts_at, id",
                    (
                        str(PlanBlockOrigin.PLANNER),
                        to_utc_iso(window.ends_at),
                        to_utc_iso(window.starts_at),
                    ),
                )
            )
        return PlanningSnapshot(
            window=window,
            revision=revision,
            open_tasks=tasks,
            deadlines=deadlines,
            actual_work_seconds=actual_seconds,
            active_calendar_events=events,
            active_manual_plan_blocks=manual_blocks,
            active_planner_plan_blocks=planner_blocks,
        )

    def _create_proposal_sync(
        self,
        proposal: PlanProposal,
        blocks: tuple[ProposedPlanBlock, ...],
        issues: tuple[PlanningIssue, ...],
    ) -> PlanProposal:
        try:
            with self._database.connect() as connection, transaction(connection):
                current = read_revision(connection)
                if current != proposal.input_revision:
                    raise PlanningSnapshotChanged(
                        f"commitment revision moved from {proposal.input_revision} to {current}"
                    )
                connection.execute(
                    "UPDATE plan_proposals SET status = ?, superseded_at = ? "
                    "WHERE status = ? AND window_end = ? AND timezone = ?",
                    (
                        str(PlanProposalStatus.SUPERSEDED),
                        to_utc_iso(proposal.created_at),
                        str(PlanProposalStatus.PENDING),
                        to_utc_iso(proposal.window.ends_at),
                        proposal.window.timezone,
                    ),
                )
                connection.execute(
                    f"INSERT INTO plan_proposals ({_PROPOSAL_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _proposal_parameters(proposal),
                )
                for ordinal, block in enumerate(blocks):
                    connection.execute(
                        "INSERT INTO proposed_plan_blocks "
                        "(id, proposal_id, task_id, starts_at, ends_at, ordinal) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid4()),
                            str(proposal.id),
                            str(block.task_id),
                            to_utc_iso(block.starts_at),
                            to_utc_iso(block.ends_at),
                            ordinal,
                        ),
                    )
                for ordinal, issue in enumerate(issues):
                    connection.execute(
                        "INSERT INTO planning_issues "
                        "(id, proposal_id, ordinal, code, task_id, message, required_minutes, "
                        "scheduled_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(_new_issue_id()),
                            str(proposal.id),
                            ordinal,
                            issue.code.value,
                            None if issue.task_id is None else str(issue.task_id),
                            issue.message,
                            issue.required_minutes,
                            issue.scheduled_minutes,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not persist plan proposal: {exc}") from exc
        return proposal

    def _get_proposal_sync(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
        return None if row is None else _row_to_proposal(row)

    def _get_proposal_detail_sync(
        self, proposal_id: PlanProposalId
    ) -> PlanProposalDetail | None:
        with self._database.connect() as connection, read_transaction(connection):
            row = connection.execute(
                f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                return None
            proposal = _row_to_proposal(row)
            blocks = tuple(
                ProposedPlanBlock(
                    task_id=UUID(str(block_row["task_id"])),
                    starts_at=from_utc_iso(str(block_row["starts_at"])),
                    ends_at=from_utc_iso(str(block_row["ends_at"])),
                    ordinal=int(block_row["ordinal"]),
                )
                for block_row in connection.execute(
                    "SELECT id, proposal_id, task_id, starts_at, ends_at, ordinal "
                    "FROM proposed_plan_blocks WHERE proposal_id = ? ORDER BY ordinal",
                    (str(proposal_id),),
                )
            )
            issues = tuple(
                PlanningIssue(
                    code=PlanningIssueCode(str(issue_row["code"])),
                    message=str(issue_row["message"]),
                    task_id=(
                        None
                        if issue_row["task_id"] is None
                        else UUID(str(issue_row["task_id"]))
                    ),
                    required_minutes=(
                        None
                        if issue_row["required_minutes"] is None
                        else int(issue_row["required_minutes"])
                    ),
                    scheduled_minutes=(
                        None
                        if issue_row["scheduled_minutes"] is None
                        else int(issue_row["scheduled_minutes"])
                    ),
                )
                for issue_row in connection.execute(
                    "SELECT code, task_id, message, required_minutes, scheduled_minutes "
                    "FROM planning_issues WHERE proposal_id = ? ORDER BY ordinal",
                    (str(proposal_id),),
                )
            )
        return PlanProposalDetail(proposal=proposal, blocks=blocks, issues=issues)

    def _list_proposals_sync(self, limit: int | None) -> list[PlanProposal]:
        statement = (
            f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals ORDER BY created_at DESC, id DESC"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def _list_proposal_summaries_sync(self, limit: int | None) -> list[PlanProposalSummary]:
        statement = (
            f"SELECT {_PROPOSAL_FIELDS}, "
            "(SELECT COUNT(*) FROM proposed_plan_blocks AS b "
            "WHERE b.proposal_id = plan_proposals.id) AS block_count, "
            "(SELECT COUNT(*) FROM planning_issues AS i "
            "WHERE i.proposal_id = plan_proposals.id) AS issue_count "
            "FROM plan_proposals ORDER BY created_at DESC, id DESC"
        )
        parameters: tuple[object, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [
            PlanProposalSummary(
                proposal=_row_to_proposal(row),
                block_count=int(row["block_count"]),
                issue_count=int(row["issue_count"]),
            )
            for row in rows
        ]

    def _mark_stale_sync(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE plan_proposals SET status = ? WHERE id = ? AND status = ?",
                (
                    str(PlanProposalStatus.STALE),
                    str(proposal_id),
                    str(PlanProposalStatus.PENDING),
                ),
            )
            updated = connection.execute(
                f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
        return None if updated is None else _row_to_proposal(updated)

    def _apply_proposal_sync(
        self, proposal_id: PlanProposalId, applied_at: datetime
    ) -> ApplyResult:
        with self._database.connect() as connection, transaction(connection):
            row = connection.execute(
                f"SELECT {_PROPOSAL_FIELDS} FROM plan_proposals WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                raise PlanProposalNotFound(proposal_id)
            proposal = _row_to_proposal(row)
            if proposal.status is not PlanProposalStatus.PENDING:
                return ApplyResult(outcome=ApplyOutcome.NOT_PENDING, proposal=proposal)
            current = read_revision(connection)
            if current != proposal.input_revision:
                # Committing the marking is deliberate: raising here would roll it back.
                connection.execute(
                    "UPDATE plan_proposals SET status = ? WHERE id = ?",
                    (str(PlanProposalStatus.STALE), str(proposal_id)),
                )
                return ApplyResult(
                    outcome=ApplyOutcome.STALE,
                    proposal=_with_status(proposal, PlanProposalStatus.STALE),
                )
            replaced = connection.execute(
                "UPDATE plan_blocks SET cancelled_at = ?, updated_at = ? "
                "WHERE origin = ? AND cancelled_at IS NULL AND starts_at < ? AND ends_at > ?",
                (
                    to_utc_iso(applied_at),
                    to_utc_iso(applied_at),
                    str(PlanBlockOrigin.PLANNER),
                    to_utc_iso(proposal.window.ends_at),
                    to_utc_iso(proposal.window.starts_at),
                ),
            ).rowcount
            proposed = connection.execute(
                "SELECT id, proposal_id, task_id, starts_at, ends_at, ordinal "
                "FROM proposed_plan_blocks WHERE proposal_id = ? ORDER BY ordinal",
                (str(proposal_id),),
            ).fetchall()
            for block_row in proposed:
                connection.execute(
                    f"INSERT INTO plan_blocks ({_PLAN_BLOCK_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        str(block_row["task_id"]),
                        str(block_row["starts_at"]),
                        str(block_row["ends_at"]),
                        to_utc_iso(applied_at),
                        to_utc_iso(applied_at),
                        None,
                        str(PlanBlockOrigin.PLANNER),
                        str(proposal_id),
                    ),
                )
            connection.execute(
                "UPDATE plan_proposals SET status = ?, applied_at = ? WHERE id = ?",
                (
                    str(PlanProposalStatus.APPLIED),
                    to_utc_iso(applied_at),
                    str(proposal_id),
                ),
            )
            increment_revision(connection)
        applied = _with_status(proposal, PlanProposalStatus.APPLIED, applied_at=applied_at)
        return ApplyResult(
            outcome=ApplyOutcome.APPLIED,
            proposal=applied,
            created_blocks=len(proposed),
            replaced_blocks=replaced,
        )


def _with_status(
    proposal: PlanProposal, status: PlanProposalStatus, *, applied_at: datetime | None = None
) -> PlanProposal:
    return replace(proposal, status=status, applied_at=applied_at or proposal.applied_at)


def _proposal_parameters(proposal: PlanProposal) -> tuple[object, ...]:
    return (
        str(proposal.id),
        str(proposal.status),
        to_utc_iso(proposal.window.starts_at),
        to_utc_iso(proposal.window.ends_at),
        proposal.window.timezone,
        proposal.input_fingerprint,
        proposal.input_revision,
        to_utc_iso(proposal.created_at),
        None if proposal.applied_at is None else to_utc_iso(proposal.applied_at),
        None if proposal.superseded_at is None else to_utc_iso(proposal.superseded_at),
    )


def _row_to_proposal(row: sqlite3.Row) -> PlanProposal:
    try:
        return PlanProposal(
            id=UUID(str(row["id"])),
            status=PlanProposalStatus(str(row["status"])),
            window=PlanningWindow(
                starts_at=from_utc_iso(str(row["window_start"])),
                ends_at=from_utc_iso(str(row["window_end"])),
                timezone=str(row["timezone"]),
            ),
            input_fingerprint=str(row["input_fingerprint"]),
            input_revision=int(row["input_revision"]),
            created_at=from_utc_iso(str(row["created_at"])),
            applied_at=(
                None if row["applied_at"] is None else from_utc_iso(str(row["applied_at"]))
            ),
            superseded_at=(
                None
                if row["superseded_at"] is None
                else from_utc_iso(str(row["superseded_at"]))
            ),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored plan proposal is not readable: {exc}") from exc


def _row_to_task(row: sqlite3.Row) -> Task:
    try:
        return Task(
            id=UUID(str(row["id"])),
            title=str(row["title"]),
            description=None if row["description"] is None else str(row["description"]),
            status=TaskStatus(str(row["status"])),
            priority=TaskPriority(str(row["priority"])),
            estimated_minutes=(
                None if row["estimated_minutes"] is None else int(row["estimated_minutes"])
            ),
            created_at=from_utc_iso(str(row["created_at"])),
            updated_at=from_utc_iso(str(row["updated_at"])),
            completed_at=(
                None if row["completed_at"] is None else from_utc_iso(str(row["completed_at"]))
            ),
            cancelled_at=(
                None if row["cancelled_at"] is None else from_utc_iso(str(row["cancelled_at"]))
            ),
        )
    except (ValueError, DomainError) as exc:
        raise CommitmentStoreError(f"stored task is not readable: {exc}") from exc


def _row_to_deadline(row: sqlite3.Row) -> Deadline:
    return Deadline(
        id=UUID(str(row["id"])),
        task_id=UUID(str(row["task_id"])),
        due_at=from_utc_iso(str(row["due_at"])),
        created_at=from_utc_iso(str(row["created_at"])),
        updated_at=from_utc_iso(str(row["updated_at"])),
    )


def _row_to_calendar_event(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        id=UUID(str(row["id"])),
        title=str(row["title"]),
        description=None if row["description"] is None else str(row["description"]),
        starts_at=from_utc_iso(str(row["starts_at"])),
        ends_at=from_utc_iso(str(row["ends_at"])),
        created_at=from_utc_iso(str(row["created_at"])),
        updated_at=from_utc_iso(str(row["updated_at"])),
        cancelled_at=(
            None if row["cancelled_at"] is None else from_utc_iso(str(row["cancelled_at"]))
        ),
    )


def _row_to_plan_block(row: sqlite3.Row) -> PlanBlock:
    return PlanBlock(
        id=UUID(str(row["id"])),
        task_id=UUID(str(row["task_id"])),
        starts_at=from_utc_iso(str(row["starts_at"])),
        ends_at=from_utc_iso(str(row["ends_at"])),
        created_at=from_utc_iso(str(row["created_at"])),
        updated_at=from_utc_iso(str(row["updated_at"])),
        cancelled_at=(
            None if row["cancelled_at"] is None else from_utc_iso(str(row["cancelled_at"]))
        ),
        origin=PlanBlockOrigin(str(row["origin"])),
        proposal_id=None if row["proposal_id"] is None else UUID(str(row["proposal_id"])),
    )


def _new_issue_id() -> UUID:
    return uuid4()


__all__ = ["SqlitePlanningRepository"]
