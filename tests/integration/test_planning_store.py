"""Integration tests for durable proposals, revisions and the atomic apply (ADR-0015).

Real SQLite throughout: the properties under test are one transaction per mutation, an
apply fenced on the revision, manual blocks that survive replacement, and a rollback that
leaves no half-applied plan.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    InvalidPlanBlock,
    PlanningSnapshotChanged,
    PlanProposalNotFound,
)
from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin
from assistant.domain.planning import (
    PlanningIssue,
    PlanningIssueCode,
    PlanningWindow,
    PlanProposal,
    PlanProposalStatus,
    ProposedPlanBlock,
)
from assistant.domain.task import Task, TaskPriority
from assistant.domain.work_session import WorkSession
from assistant.store import planning as planning_store
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
WINDOW = PlanningWindow(
    starts_at=NOW, ends_at=NOW + timedelta(days=2), timezone="Asia/Shanghai"
)
OTHER_WINDOW = PlanningWindow(
    starts_at=NOW + timedelta(days=7),
    ends_at=NOW + timedelta(days=9),
    timezone="Asia/Shanghai",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def commitments(database: Database) -> SqliteCommitmentRepository:
    return SqliteCommitmentRepository(database)


@pytest.fixture
def work(database: Database) -> SqliteWorkRepository:
    return SqliteWorkRepository(database)


@pytest.fixture
def planning(database: Database) -> SqlitePlanningRepository:
    return SqlitePlanningRepository(database)


def _revision(database: Database) -> int:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT value FROM commitment_meta WHERE key = 'revision'"
        ).fetchone()
    return int(str(row["value"]))


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "title": "Write SE lab report",
        "created_at": NOW,
        "updated_at": NOW,
        "priority": TaskPriority.HIGH,
        "estimated_minutes": 300,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _manual_block(task_id: UUID, *, start_hours: int, end_hours: int) -> PlanBlock:
    starts_at = NOW + timedelta(hours=start_hours)
    return PlanBlock(
        task_id=task_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=end_hours - start_hours),
        created_at=NOW,
        updated_at=NOW,
    )


def _proposal(
    *,
    window: PlanningWindow = WINDOW,
    revision: int = 0,
    fingerprint: str = "a" * 64,
    created_at: datetime = NOW,
) -> PlanProposal:
    return PlanProposal(
        window=window,
        input_fingerprint=fingerprint,
        input_revision=revision,
        created_at=created_at,
    )


def _proposed(
    task_id: UUID, *, start_hours: int, end_hours: int, ordinal: int
) -> ProposedPlanBlock:
    starts_at = NOW + timedelta(hours=start_hours)
    return ProposedPlanBlock(
        task_id=task_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=end_hours - start_hours),
        ordinal=ordinal,
    )


def _deadline(task_id: UUID, *, hours: int, at: datetime = NOW) -> Deadline:
    return Deadline(
        task_id=task_id,
        due_at=NOW + timedelta(hours=hours),
        created_at=at,
        updated_at=at,
    )


# ------------------------------------------------------------------ revision wiring


async def test_every_planning_relevant_mutation_bumps_the_revision_once(
    database: Database,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    clock: FakeClock,
) -> None:
    baseline = _revision(database)
    assert baseline == 0

    task = _task()
    await commitments.add_task(task)
    assert _revision(database) == baseline + 1

    clock.advance(60)
    edited = task.update_details(
        title="Write SE lab report v2",
        description="with references",
        priority=TaskPriority.NORMAL,
        estimated_minutes=240,
        at=clock.now(),
    )
    await commitments.update_task(edited, expected_updated_at=task.updated_at)
    assert _revision(database) == baseline + 2

    clock.advance(60)
    await commitments.set_deadline(
        _deadline(task.id, hours=100, at=clock.now()),
        expected_updated_at=edited.updated_at,
        at=clock.now(),
    )
    assert _revision(database) == baseline + 3
    after_deadline = await commitments.get_task(task.id)
    assert after_deadline is not None

    clock.advance(60)
    await commitments.clear_deadline(
        task_id=task.id, expected_updated_at=after_deadline.updated_at, at=clock.now()
    )
    assert _revision(database) == baseline + 4

    event = CalendarEvent(
        title="SE lecture",
        starts_at=NOW + timedelta(hours=6),
        ends_at=NOW + timedelta(hours=8),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await commitments.add_calendar_event(event)
    assert _revision(database) == baseline + 5

    clock.advance(60)
    await commitments.cancel_calendar_event(event.id, at=clock.now())
    assert _revision(database) == baseline + 6

    block = _manual_block(task.id, start_hours=10, end_hours=12)
    await commitments.add_plan_block(block)
    assert _revision(database) == baseline + 7

    clock.advance(60)
    await commitments.cancel_plan_block(block.id, at=clock.now())
    assert _revision(database) == baseline + 8

    session = WorkSession(
        task_id=task.id,
        started_at=NOW + timedelta(hours=12),
        ended_at=NOW + timedelta(hours=13),
        created_at=clock.now(),
    )
    await work.add_work_session(session)
    assert _revision(database) == baseline + 9


async def test_terminal_transition_bumps_the_revision_only_once(
    database: Database,
    commitments: SqliteCommitmentRepository,
    clock: FakeClock,
) -> None:
    task = _task()
    await commitments.add_task(task)
    for start in (4, 30, 50):
        await commitments.add_plan_block(
            _manual_block(task.id, start_hours=start, end_hours=start + 2)
        )
    before = _revision(database)

    clock.advance(3600)
    result = await commitments.complete_task(
        task.complete(at=clock.now()), expected_updated_at=task.updated_at
    )

    assert result.cancelled_plan_blocks == 3
    assert _revision(database) == before + 1

    other = _task(title="Other task")
    await commitments.add_task(other)
    before_cancel = _revision(database)
    clock.advance(60)
    await commitments.cancel_task(
        other.cancel(at=clock.now()), expected_updated_at=other.updated_at
    )
    assert _revision(database) == before_cancel + 1


async def test_reads_and_proposal_lifecycle_do_not_bump_the_revision(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    await planning.load_snapshot(WINDOW)
    before = _revision(database)

    proposal = _proposal(revision=before)
    await planning.create_proposal(
        proposal,
        blocks=(_proposed(task.id, start_hours=1, end_hours=2, ordinal=0),),
        issues=(),
    )
    assert _revision(database) == before

    assert await planning.list_proposals(limit=None) != []
    assert await planning.get_proposal(proposal.id) is not None
    assert await planning.get_proposal_detail(proposal.id) is not None
    assert _revision(database) == before

    await planning.mark_stale(proposal.id)
    assert _revision(database) == before


async def test_apply_bumps_the_revision_exactly_once(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    revision = _revision(database)
    proposal = _proposal(revision=revision)
    await planning.create_proposal(
        proposal,
        blocks=(
            _proposed(task.id, start_hours=1, end_hours=2, ordinal=0),
            _proposed(task.id, start_hours=3, end_hours=4, ordinal=1),
        ),
        issues=(),
    )

    result = await planning.apply_proposal(proposal.id, applied_at=NOW + timedelta(hours=1))

    assert result.outcome.value == "applied"
    assert result.created_blocks == 2
    assert _revision(database) == revision + 1


# --------------------------------------------------------------------- snapshots


async def test_snapshot_is_consistent_and_separates_block_origins(
    database: Database,
    commitments: SqliteCommitmentRepository,
    work: SqliteWorkRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task, deadline=_deadline(task.id, hours=72))
    await work.add_work_session(
        WorkSession(
            task_id=task.id,
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=90, seconds=30),
            created_at=NOW,
        )
    )
    await commitments.add_plan_block(_manual_block(task.id, start_hours=1, end_hours=2))
    inside = CalendarEvent(
        title="Lecture",
        starts_at=NOW + timedelta(hours=3),
        ends_at=NOW + timedelta(hours=4),
        created_at=NOW,
        updated_at=NOW,
    )
    outside = CalendarEvent(
        title="Next month",
        starts_at=NOW + timedelta(days=30),
        ends_at=NOW + timedelta(days=30, hours=1),
        created_at=NOW,
        updated_at=NOW,
    )
    cancelled = CalendarEvent(
        title="Cancelled lecture",
        starts_at=NOW + timedelta(hours=5),
        ends_at=NOW + timedelta(hours=6),
        created_at=NOW,
        updated_at=NOW,
    )
    await commitments.add_calendar_event(inside)
    await commitments.add_calendar_event(outside)
    await commitments.add_calendar_event(cancelled)
    await commitments.cancel_calendar_event(cancelled.id, at=NOW)
    done = _task(title="Already done")
    await commitments.add_task(done)
    await commitments.complete_task(
        done.complete(at=NOW + timedelta(minutes=1)),
        expected_updated_at=done.updated_at,
    )

    # A planner block arrives through a real apply, which is the only way it can be written.
    proposal = _proposal(revision=_revision(database))
    await planning.create_proposal(
        proposal,
        blocks=(_proposed(task.id, start_hours=7, end_hours=8, ordinal=0),),
        issues=(),
    )
    await planning.apply_proposal(proposal.id, applied_at=NOW + timedelta(minutes=2))

    snapshot = await planning.load_snapshot(WINDOW)

    assert snapshot.revision == _revision(database)
    assert [open_task.id for open_task in snapshot.open_tasks] == [task.id]
    assert snapshot.deadlines[task.id].due_at == NOW + timedelta(hours=72)
    assert snapshot.actual_work_seconds[task.id] == 5430
    assert [event.id for event in snapshot.active_calendar_events] == [inside.id]
    assert len(snapshot.active_manual_plan_blocks) == 1
    assert [block.origin for block in snapshot.active_planner_plan_blocks] == [
        PlanBlockOrigin.PLANNER
    ]
    assert snapshot.active_planner_plan_blocks[0].proposal_id == proposal.id


async def test_cancelled_manual_blocks_leave_the_snapshot(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    block = _manual_block(task.id, start_hours=1, end_hours=2)
    await commitments.add_plan_block(block)
    await commitments.cancel_plan_block(block.id, at=NOW + timedelta(minutes=5))

    snapshot = await planning.load_snapshot(WINDOW)

    assert snapshot.active_manual_plan_blocks == ()


# --------------------------------------------------------------------- proposals


async def test_create_proposal_rejects_a_revision_that_moved(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    await commitments.add_task(_task())
    stale_proposal = _proposal(revision=0)

    with pytest.raises(PlanningSnapshotChanged):
        await planning.create_proposal(stale_proposal, blocks=(), issues=())

    assert await planning.get_proposal(stale_proposal.id) is None


async def test_only_a_same_window_pending_proposal_is_superseded(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    await commitments.add_task(_task())
    before = _revision(database)
    first = _proposal(revision=1)
    await planning.create_proposal(first, blocks=(), issues=())
    other_window = _proposal(window=OTHER_WINDOW, revision=1)
    await planning.create_proposal(other_window, blocks=(), issues=())
    second = _proposal(revision=1, created_at=NOW + timedelta(minutes=1))
    await planning.create_proposal(second, blocks=(), issues=())

    stored_first = await planning.get_proposal(first.id)
    stored_other = await planning.get_proposal(other_window.id)
    stored_second = await planning.get_proposal(second.id)

    assert stored_first is not None
    assert stored_first.status is PlanProposalStatus.SUPERSEDED
    assert stored_first.superseded_at == NOW + timedelta(minutes=1)
    assert stored_other is not None and stored_other.status is PlanProposalStatus.PENDING
    assert stored_second is not None and stored_second.status is PlanProposalStatus.PENDING
    assert _revision(database) == before  # superseding is not a commitment mutation


async def test_proposal_summaries_count_blocks_and_issues_newest_first(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    first = _proposal(revision=1)
    await planning.create_proposal(
        first,
        blocks=(_proposed(task.id, start_hours=1, end_hours=2, ordinal=0),),
        issues=(),
    )
    second = _proposal(revision=1, created_at=NOW + timedelta(minutes=5))
    await planning.create_proposal(
        second,
        blocks=(
            _proposed(task.id, start_hours=3, end_hours=4, ordinal=0),
            _proposed(task.id, start_hours=5, end_hours=6, ordinal=1),
        ),
        issues=(
            PlanningIssue(
                code=PlanningIssueCode.MISSING_ESTIMATE,
                message="task has no estimate",
            ),
        ),
    )

    summaries = await planning.list_proposal_summaries(limit=10)

    assert [summary.proposal.id for summary in summaries] == [second.id, first.id]
    assert [(summary.block_count, summary.issue_count) for summary in summaries] == [
        (2, 1),
        (1, 0),
    ]
    assert await planning.list_proposal_summaries(limit=1) == summaries[:1]


async def test_proposal_detail_is_the_stored_proposal_not_a_recomputation(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    proposal = _proposal(revision=1)
    blocks = (
        _proposed(task.id, start_hours=1, end_hours=2, ordinal=0),
        _proposed(task.id, start_hours=3, end_hours=4, ordinal=1),
    )
    issues = (
        PlanningIssue(
            code=PlanningIssueCode.BUFFER_VIOLATED,
            task_id=task.id,
            message="the deadline buffer was not enough",
            required_minutes=120,
            scheduled_minutes=60,
        ),
        PlanningIssue(
            code=PlanningIssueCode.MISSING_ESTIMATE,
            task_id=None,
            message="task has no estimate",
        ),
    )
    await planning.create_proposal(proposal, blocks=blocks, issues=issues)

    detail = await planning.get_proposal_detail(proposal.id)

    assert detail is not None
    assert [(b.task_id, b.starts_at, b.ends_at, b.ordinal) for b in detail.blocks] == [
        (b.task_id, b.starts_at, b.ends_at, b.ordinal) for b in blocks
    ]
    assert [
        (i.code, i.task_id, i.message, i.required_minutes, i.scheduled_minutes)
        for i in detail.issues
    ] == [
        (i.code, i.task_id, i.message, i.required_minutes, i.scheduled_minutes)
        for i in issues
    ]


async def test_proposal_ids_resolve_by_prefix_and_reject_ambiguity(
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    await commitments.add_task(_task())
    proposal = _proposal(revision=1)
    await planning.create_proposal(proposal, blocks=(), issues=())

    assert await planning.resolve_proposal_id(str(proposal.id)[:8]) == proposal.id
    assert await planning.resolve_proposal_id(str(proposal.id)) == proposal.id
    with pytest.raises(PlanProposalNotFound):
        await planning.resolve_proposal_id("ffffffff")


# ------------------------------------------------------------------- atomic apply


async def test_apply_replaces_only_planner_blocks_and_preserves_manual_ones(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    manual = _manual_block(task.id, start_hours=1, end_hours=2)
    await commitments.add_plan_block(manual)

    first = _proposal(revision=_revision(database))
    await planning.create_proposal(
        first,
        blocks=(
            _proposed(task.id, start_hours=3, end_hours=4, ordinal=0),
            _proposed(task.id, start_hours=5, end_hours=6, ordinal=1),
        ),
        issues=(),
    )
    applied_first = await planning.apply_proposal(
        first.id, applied_at=NOW + timedelta(minutes=10)
    )
    assert applied_first.created_blocks == 2
    revision_after_first = _revision(database)

    second = _proposal(
        revision=revision_after_first, created_at=NOW + timedelta(minutes=20)
    )
    await planning.create_proposal(
        second,
        blocks=(_proposed(task.id, start_hours=9, end_hours=10, ordinal=0),),
        issues=(),
    )
    applied_second = await planning.apply_proposal(
        second.id, applied_at=NOW + timedelta(minutes=30)
    )

    assert applied_second.created_blocks == 1
    assert applied_second.replaced_blocks == 2
    assert _revision(database) == revision_after_first + 1

    blocks = await commitments.list_plan_blocks_in_range(
        query_start=NOW, query_end=WINDOW.ends_at, include_cancelled=True
    )
    by_id = {block.id: block for block in blocks}
    assert by_id[manual.id].cancelled_at is None
    assert by_id[manual.id].origin is PlanBlockOrigin.MANUAL
    assert by_id[manual.id].proposal_id is None

    active_planner = [
        block
        for block in blocks
        if block.origin is PlanBlockOrigin.PLANNER and block.cancelled_at is None
    ]
    assert len(active_planner) == 1
    assert active_planner[0].proposal_id == second.id
    assert active_planner[0].starts_at == NOW + timedelta(hours=9)
    assert active_planner[0].created_at == NOW + timedelta(minutes=30)

    cancelled_planner = [
        block
        for block in blocks
        if block.origin is PlanBlockOrigin.PLANNER and block.cancelled_at is not None
    ]
    assert len(cancelled_planner) == 2
    assert {block.cancelled_at for block in cancelled_planner} == {
        NOW + timedelta(minutes=30)
    }
    assert {block.proposal_id for block in cancelled_planner} == {first.id}

    stored = await planning.get_proposal(second.id)
    assert stored is not None
    assert stored.status is PlanProposalStatus.APPLIED
    assert stored.applied_at == NOW + timedelta(minutes=30)


async def test_apply_rolls_back_completely_when_a_block_insert_fails(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    await commitments.add_task(task)
    manual = _manual_block(task.id, start_hours=1, end_hours=2)
    await commitments.add_plan_block(manual)

    first = _proposal(revision=_revision(database))
    await planning.create_proposal(
        first,
        blocks=(_proposed(task.id, start_hours=3, end_hours=4, ordinal=0),),
        issues=(),
    )
    await planning.apply_proposal(first.id, applied_at=NOW + timedelta(minutes=10))
    revision_before = _revision(database)
    active_before = await commitments.list_plan_blocks_in_range(
        query_start=NOW, query_end=WINDOW.ends_at
    )

    second = _proposal(revision=revision_before, created_at=NOW + timedelta(minutes=20))
    await planning.create_proposal(
        second,
        blocks=(
            _proposed(task.id, start_hours=9, end_hours=10, ordinal=0),
            _proposed(task.id, start_hours=11, end_hours=12, ordinal=1),
        ),
        issues=(),
    )

    real_uuid4 = planning_store.uuid4
    calls = {"count": 0}

    def flaky_uuid4() -> UUID:
        calls["count"] += 1
        if calls["count"] == 2:  # the second inserted block explodes
            raise RuntimeError("injected failure while inserting the second block")
        return real_uuid4()

    monkeypatch.setattr(planning_store, "uuid4", flaky_uuid4)
    try:
        with pytest.raises(RuntimeError):
            await planning.apply_proposal(second.id, applied_at=NOW + timedelta(minutes=30))
    finally:
        monkeypatch.setattr(planning_store, "uuid4", real_uuid4)

    assert _revision(database) == revision_before
    assert (
        await commitments.list_plan_blocks_in_range(
            query_start=NOW, query_end=WINDOW.ends_at
        )
        == active_before
    )
    stored = await planning.get_proposal(second.id)
    assert stored is not None and stored.status is PlanProposalStatus.PENDING


async def test_stale_apply_marks_the_proposal_and_changes_nothing(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
    work: SqliteWorkRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    manual = _manual_block(task.id, start_hours=1, end_hours=2)
    await commitments.add_plan_block(manual)
    revision = _revision(database)
    proposal = _proposal(revision=revision)
    await planning.create_proposal(
        proposal,
        blocks=(_proposed(task.id, start_hours=5, end_hours=6, ordinal=0),),
        issues=(),
    )
    blocks_before = await commitments.list_plan_blocks_in_range(
        query_start=NOW, query_end=WINDOW.ends_at, include_cancelled=True
    )
    await work.add_work_session(
        WorkSession(
            task_id=task.id,
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=30),
            created_at=NOW,
        )
    )

    result = await planning.apply_proposal(proposal.id, applied_at=NOW + timedelta(hours=1))

    assert result.outcome.value == "stale"
    assert _revision(database) == revision + 1  # only the work session bumped it
    stored = await planning.get_proposal(proposal.id)
    assert stored is not None and stored.status is PlanProposalStatus.STALE
    assert (
        await commitments.list_plan_blocks_in_range(
            query_start=NOW, query_end=WINDOW.ends_at, include_cancelled=True
        )
        == blocks_before
    )


async def test_applying_a_proposal_twice_is_refused(
    database: Database,
    commitments: SqliteCommitmentRepository,
    planning: SqlitePlanningRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    proposal = _proposal(revision=_revision(database))
    await planning.create_proposal(
        proposal,
        blocks=(_proposed(task.id, start_hours=1, end_hours=2, ordinal=0),),
        issues=(),
    )
    await planning.apply_proposal(proposal.id, applied_at=NOW + timedelta(minutes=1))
    revision = _revision(database)

    again = await planning.apply_proposal(proposal.id, applied_at=NOW + timedelta(minutes=2))

    assert again.outcome.value == "not_pending"
    assert again.created_blocks == 0
    assert _revision(database) == revision


async def test_unknown_proposal_is_reported(planning: SqlitePlanningRepository) -> None:
    with pytest.raises(PlanProposalNotFound):
        await planning.apply_proposal(uuid4(), applied_at=NOW)


# ------------------------------------------------------------- provenance safety


async def test_database_refuses_inconsistent_block_provenance(
    database: Database,
    commitments: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    rows = (
        ("planner", None),
        ("manual", str(uuid4())),
        ("whatever", None),
    )

    with database.connect() as connection:
        for index, (origin, proposal_id) in enumerate(rows):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO plan_blocks "
                    "(id, task_id, starts_at, ends_at, created_at, updated_at, cancelled_at, "
                    "origin, proposal_id) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        str(uuid4()),
                        str(task.id),
                        f"2026-09-20T{10 + index:02d}:00:00+00:00",
                        f"2026-09-20T{11 + index:02d}:00:00+00:00",
                        "2026-09-20T09:00:00+00:00",
                        "2026-09-20T09:00:00+00:00",
                        origin,
                        proposal_id,
                    ),
                )


async def test_commitment_repository_refuses_to_forge_a_planner_block(
    commitments: SqliteCommitmentRepository,
) -> None:
    task = _task()
    await commitments.add_task(task)
    forged = PlanBlock(
        task_id=task.id,
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        created_at=NOW,
        updated_at=NOW,
        origin=PlanBlockOrigin.PLANNER,
        proposal_id=uuid4(),
    )

    with pytest.raises(InvalidPlanBlock):
        await commitments.add_plan_block(forged)
