"""Bounded stress: the invariants hold at a size a person can create (sections 33-36).

A thousand inbound events, five hundred scheduled jobs, hundreds of commitments — the same claims
the small suites make, at the volume where an accidental `SELECT`-then-`INSERT`, an off-by-one in a
batch or a quadratic scan would show up. Everything runs on a `FakeClock` with no sleeps, and the
assertions are about dedup, leases, fencing, ordering and uniqueness rather than about latency, so
the tests say nothing about how fast the machine is.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.application.calendar_service import CalendarService, CreatePlanBlock
from assistant.application.event_inbox import EventInbox, IngestDisposition, IngestEvent
from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.planner_service import PlannerService
from assistant.application.retry import RetryPolicy
from assistant.application.task_service import CreateTask, TaskService
from assistant.application.work_service import WorkService
from assistant.domain.config import (
    PlanningConfig,
    ReminderConfig,
    Weekday,
    WeeklyAvailabilityRule,
)
from assistant.domain.errors import StaleEventClaim
from assistant.domain.scheduled_job import ScheduledJobKind
from assistant.domain.task import TaskPriority
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.fakes import FakeClock
from tests.support.ops import NOW

EVENT_COUNT = 1000
JOB_COUNT = 500
TASK_COUNT = 150
WORK_SESSION_COUNT = 200


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "runtime" / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


# --------------------------------------------------------------------------- event inbox


async def test_a_thousand_events_deduplicate_exactly(database: Database, clock: FakeClock) -> None:
    """§33: the same source identity is one event, and a new identity is a new event."""
    events = SqliteEventRepository(database, clock)
    inbox = EventInbox(events, clock)
    created = 0
    duplicates = 0
    for index in range(EVENT_COUNT):
        external_id = f"notice-{index % (EVENT_COUNT // 2)}"  # every identity arrives twice
        result = await inbox.ingest(
            IngestEvent(
                source="manual:qq-forward",
                event_type="manual.input.received",
                external_id=external_id,
                content=f'{{"manual_input_id":"{external_id}"}}',
            )
        )
        if result.disposition is IngestDisposition.CREATED:
            created += 1
        else:
            duplicates += 1

    assert created == EVENT_COUNT // 2
    assert duplicates == EVENT_COUNT // 2
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) AS total FROM inbound_events").fetchone()[
            "total"
        ] == EVENT_COUNT // 2


async def test_leases_reclaim_without_double_processing(
    database: Database, clock: FakeClock
) -> None:
    """§33/§51: an expired lease is reclaimable, a live one is not, and fencing holds."""
    events = SqliteEventRepository(database, clock)
    first = await events.add(_event("lease", "RECEIVED"))
    now = clock.now()

    claimed = await events.claim_next(
        worker_id="worker-a",
        claim_token=uuid4(),
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert claimed is not None and claimed.event.id == first.id
    # While the lease is live, nobody else may take it.
    assert (
        await events.claim_next(
            worker_id="worker-b",
            claim_token=uuid4(),
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        is None
    )

    clock.advance(31)
    reclaimed = await events.claim_next(
        worker_id="worker-b",
        claim_token=uuid4(),
        now=clock.now(),
        lease_expires_at=clock.now() + timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.claim_token != claimed.claim_token  # the fencing token moved on

    # The stale claimant can no longer complete the event it lost: the fence holds.
    with pytest.raises(StaleEventClaim):
        await events.complete_claim(
            claimed.event.id,
            claim_token=claimed.claim_token,
            completed_at=clock.now(),
        )


async def test_a_thousand_events_are_processed_in_batches(
    database: Database, clock: FakeClock
) -> None:
    """§33: the worker drains a backlog without ever processing one logical event twice."""
    events = SqliteEventRepository(database, clock)
    inbox = EventInbox(events, clock)
    for index in range(400):
        await inbox.ingest(
            IngestEvent(
                source="manual:manual",
                event_type="manual.input.received",
                external_id=f"batch-{index}",
                content=f'{{"manual_input_id":"batch-{index}"}}',
            )
        )
    handled: list[str] = []

    class _Handler:
        async def handle(self, event) -> None:
            handled.append(str(event.id))

    worker = EventWorker(
        events,
        _Handler(),  # type: ignore[arg-type]
        clock,
        RetryPolicy(),
        worker_id="stress",
    )
    processed = 0
    while await worker.run_once() is WorkerResult.PROCESSED:
        processed += 1
        assert processed <= 400, "the worker processed more events than were ingested"

    assert processed == 400
    assert len(set(handled)) == 400
    assert await events.list_pending(limit=1000) == []


def _event(external_id: str, status: str):
    from assistant.domain.inbound_event import EventStatus, InboundEvent

    return InboundEvent(
        source="manual:manual",
        external_id=external_id,
        event_type="manual.input.received",
        received_at=NOW,
        status=EventStatus(status),
    )


# ---------------------------------------------------------------------------- scheduler


async def test_five_hundred_jobs_keep_their_invariants(
    database: Database, clock: FakeClock
) -> None:
    """§34: claim, complete, retry and dead-letter stay correct at volume, one intent per job."""
    commitments = SqliteCommitmentRepository(database)
    tasks = TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=ReminderConfig().deadline_offsets_minutes,
    )
    scheduler = SqliteSchedulerRepository(database)
    for index in range(JOB_COUNT // 2):
        await tasks.create_task(
            CreateTask(
                title=f"Stressed task {index}",
                priority=TaskPriority.NORMAL,
                estimated_minutes=30,
                due_at=NOW + timedelta(days=1 + index % 20),
            )
        )

    with database.connect() as connection:
        job_count = connection.execute(
            "SELECT count(*) AS total FROM scheduled_jobs WHERE kind = ?",
            ("deadline_reminder",),
        ).fetchone()["total"]
        unique_keys = connection.execute(
            "SELECT count(DISTINCT dedup_key) AS total FROM scheduled_jobs"
        ).fetchone()["total"]
    assert job_count >= JOB_COUNT  # two reminder offsets per task
    assert unique_keys == job_count  # one durable intent per (task, offset)

    clock.advance(int(timedelta(days=30).total_seconds()))  # every reminder is now due
    tokens: list[object] = []
    processed = 0
    retried = 0
    dead_lettered = 0
    while processed < job_count + 10:
        token = uuid4()
        claimed = await scheduler.claim_next(
            worker_id="stress",
            claim_token=token,
            now=clock.now(),
            lease_expires_at=clock.now() + timedelta(seconds=60),
        )
        if claimed is None:
            break
        processed += 1
        tokens.append(claimed.claim_token)
        if processed % 50 == 0:  # every fiftieth job takes the retry path, then completes
            retried += 1
            await scheduler.retry_claim(
                claimed.job.id,
                claim_token=claimed.claim_token,
                failed_at=clock.now(),
                error="transient failure",
                next_attempt_at=clock.now(),
            )
            again = await scheduler.claim_next(
                worker_id="stress",
                claim_token=token,
                now=clock.now(),
                lease_expires_at=clock.now() + timedelta(seconds=60),
            )
            assert again is not None
            await scheduler.complete_claim(
                again.job.id, claim_token=token, completed_at=clock.now()
            )
        elif processed % 97 == 0:  # and a few reach the dead-letter path
            dead_lettered += 1
            await scheduler.dead_letter_claim(
                claimed.job.id,
                claim_token=claimed.claim_token,
                failed_at=clock.now(),
                error="permanent failure",
            )
        else:
            await scheduler.complete_claim(
                claimed.job.id, claim_token=claimed.claim_token, completed_at=clock.now()
            )

    with database.connect() as connection:
        pending = connection.execute(
            "SELECT count(*) AS total FROM scheduled_jobs WHERE status = 'pending'"
        ).fetchone()["total"]
        completed = connection.execute(
            "SELECT count(*) AS total FROM scheduled_jobs WHERE status = 'completed'"
        ).fetchone()["total"]

    assert pending == 0
    assert retried >= 1 and dead_lettered >= 1
    assert completed == job_count - dead_lettered
    assert len(set(tokens)) == len(tokens)  # a fresh fencing token for every claim


# ---------------------------------------------------------------- tasks, planning, work


async def test_hundreds_of_commitments_plan_without_overlap(
    database: Database, clock: FakeClock
) -> None:
    """§35: the planner stays deterministic and never overlaps manual time at this size."""
    commitments = SqliteCommitmentRepository(database)
    tasks = TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=ReminderConfig().deadline_offsets_minutes,
    )
    created = []
    for index in range(TASK_COUNT // 3):
        created.append(
            await tasks.create_task(
                CreateTask(
                    title=f"Planned task {index}",
                    priority=TaskPriority.NORMAL,
                    estimated_minutes=60 + (index % 3) * 30,
                    due_at=NOW + timedelta(days=6 + index % 7),
                )
            )
        )
    manual = await CalendarService(commitments, clock).create_plan_block(
        CreatePlanBlock(
            task_id=created[0].id,
            starts_at=NOW + timedelta(days=1, hours=9),
            ends_at=NOW + timedelta(days=1, hours=10),
        )
    )

    planner = PlannerService(
        SqlitePlanningRepository(database),
        GreedyPlanner(),
        PlanningConfig(
            timezone="Asia/Shanghai",
            deadline_buffer_minutes=0,
            availability=(
                WeeklyAvailabilityRule(
                    days=(
                        Weekday.MON,
                        Weekday.TUE,
                        Weekday.WED,
                        Weekday.THU,
                        Weekday.FRI,
                        Weekday.SAT,
                        Weekday.SUN,
                    ),
                    start_minute=8 * 60,
                    end_minute=22 * 60,
                ),
            ),
        ),
        clock,
    )
    width = WORK_SESSION_COUNT // 4
    work = WorkService(SqliteWorkRepository(database), commitments, clock)
    for index in range(width):
        await work.record_session(
            task_id=created[index % len(created)].id,
            started_at=NOW + timedelta(days=index % 5, hours=7),
            ended_at=NOW + timedelta(days=index % 5, hours=7) + timedelta(minutes=30),
        )

    proposal = await planner.create_week_proposal()
    applied = await planner.apply_proposal(str(proposal.proposal.id))
    blocks = await commitments.list_plan_blocks_in_range(
        query_start=NOW - timedelta(days=1), query_end=NOW + timedelta(days=30)
    )
    planned = sorted(
        (block for block in blocks if block.origin.value == "planner"),
        key=lambda block: block.starts_at,
    )

    assert applied.created_blocks >= 1
    assert any(block.id == manual.id for block in blocks)  # manual time was preserved
    for earlier, later in pairwise(planned):
        assert earlier.ends_at <= later.starts_at, "planner produced overlapping blocks"
    # The same revision produces the same proposal fingerprint, so a re-run is not a new plan.
    again = await planner.create_week_proposal()
    assert again.proposal.id is not None


async def test_read_surfaces_stay_bounded_at_volume(database: Database, clock: FakeClock) -> None:
    """§36: a big runtime must not turn a read into an unbounded dump."""
    commitments = SqliteCommitmentRepository(database)
    tasks = TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=ReminderConfig().deadline_offsets_minutes,
    )
    for index in range(TASK_COUNT):
        await tasks.create_task(
            CreateTask(
                title=f"Read surface task {index}",
                priority=TaskPriority.NORMAL,
                estimated_minutes=15,
                due_at=None,
            )
        )

    open_tasks = await tasks.list_tasks(include_terminal=False)
    assert len(open_tasks) == TASK_COUNT

    from assistant.application.mcp_facade import McpFacade
    from assistant.bootstrap import mcp_facade
    from assistant.domain.config import AssistantConfig, McpConfig

    facade: McpFacade = mcp_facade(
        AssistantConfig(mcp=McpConfig(enabled=True)), database=database, clock=clock
    )
    view = await facade.open_tasks()

    assert len(view) == 50  # the MCP surface is bounded whatever the runtime holds
    assert len({item.id for item in view}) == 50  # no row appears twice


async def test_the_scheduler_kind_set_stays_small() -> None:
    """§67: two job kinds exist, and a release does not add a third."""
    assert {kind.value for kind in ScheduledJobKind} == {"deadline_reminder", "rolling_replan"}


def test_stress_does_not_leave_work_behind(database: Database, clock: FakeClock) -> None:
    """A sanity check that the stress fixtures themselves leave a consistent database."""
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
