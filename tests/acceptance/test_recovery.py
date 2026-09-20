"""Acceptance: restart, lease recovery, ambiguous results and log privacy (ADR-0031).

These are the tests that matter after something goes wrong. A restart is modelled as a *new*
database handle, a new worker and a new service over the same directory — never the same object
pretending to be fresh. Lease recovery is modelled by taking a lease, letting it expire and handing
the work to a second worker. And the privacy sweep runs a whole lifecycle with sentinels in every
store and checks that none of them reaches the log.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.mail_event_handler import InboundEventDispatcher
from assistant.application.manual_input_service import ManualInputService
from assistant.application.observation_context import ObservationContextBuilder
from assistant.application.observation_event_handler import (
    MANUAL_EVENT_TYPE,
    ObservationInboundEventHandler,
)
from assistant.application.retry import RetryPolicy
from assistant.application.structured_model import StructuredModel
from assistant.application.task_service import CreateTask, TaskService
from assistant.domain.errors import ActionExecutionUnresolved
from assistant.domain.inbound_event import EventStatus
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.manual_inputs import SqliteManualInputRepository
from assistant.store.migrations import apply_migrations
from assistant.store.observation_analyses import SqliteObservationAnalysisRepository
from assistant.store.web_watch import SqliteWebWatchRepository
from tests.support.actions import SECRET_TOKEN, FakeActionExecutor, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.ops import NOW
from tests.support.watchers import observation_analysis_response


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    return root


def _open(runtime_root: Path, clock: FakeClock) -> Database:
    """Open the runtime the way a new process would: a fresh handle over the same directory."""
    database = Database.at(runtime_root / "assistant.db")
    apply_migrations(database, clock=clock)
    return database


def _handler(
    database: Database, clock: FakeClock, runtime_root: Path, model: FakeModelAdapter
) -> ObservationInboundEventHandler:
    from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore

    snapshots = WebSnapshotStore(runtime_root)
    return ObservationInboundEventHandler(
        SqliteWebWatchRepository(database),
        SqliteManualInputRepository(database),
        SqliteObservationAnalysisRepository(database),
        ObservationContextBuilder(snapshots, SqliteWebWatchRepository(database)),
        StructuredModel(model),
        clock,
    )


# --------------------------------------------------------------------------- restart


async def test_state_survives_a_new_process_instance(runtime_root: Path) -> None:
    """§49: instance A writes, closes; instance B opens the same directory and continues."""
    clock_a = FakeClock(start=NOW)
    database_a = _open(runtime_root, clock_a)
    manual = ManualInputService(
        SqliteManualInputRepository(database_a),
        SqliteEventRepository(database_a, clock_a),
        clock_a,
    )
    stored = await manual.create_input("Forwarded: the lab report is due Friday.")
    task = await TaskService(SqliteCommitmentRepository(database_a), clock_a).create_task(
        CreateTask(title="A task from the first process")
    )

    # Instance B: new handle, new worker, new services over the same directory.
    clock_b = FakeClock(start=NOW + timedelta(minutes=5))
    database_b = _open(runtime_root, clock_b)
    events = SqliteEventRepository(database_b, clock_b)
    pending = await events.list_pending(limit=10)
    assert [item.id for item in pending] == [stored.event_id]
    assert await events.get(stored.event_id) is not None

    model = FakeModelAdapter().queue_text(observation_analysis_response())
    worker = EventWorker(
        events,
        InboundEventDispatcher(
            {MANUAL_EVENT_TYPE: _handler(database_b, clock_b, runtime_root, model)}
        ),
        clock_b,
        RetryPolicy(),
        worker_id="second-process",
    )
    assert await worker.run_once() is WorkerResult.PROCESSED

    analysis = await SqliteObservationAnalysisRepository(database_b).get_analysis(
        stored.event_id
    )
    assert analysis is not None and analysis.summary
    tasks = await TaskService(
        SqliteCommitmentRepository(database_b), clock_b
    ).list_tasks(include_terminal=True)
    assert [item.id for item in tasks] == [task.id]


# --------------------------------------------------------------------- lease recovery


async def test_an_expired_lease_is_reclaimed_without_duplicating_analysis(
    runtime_root: Path,
) -> None:
    """§51: a worker that died holding a lease loses it, and the analysis happens exactly once."""
    clock = FakeClock(start=NOW)
    database = _open(runtime_root, clock)
    manual = ManualInputService(
        SqliteManualInputRepository(database),
        SqliteEventRepository(database, clock),
        clock,
    )
    stored = await manual.create_input("Forwarded: the lab report is due Friday.")
    events = SqliteEventRepository(database, clock)
    # The first worker claims the event and then "dies" without completing it.
    claim = await events.claim_next(
        worker_id="dead-worker",
        claim_token=__import__("uuid").uuid4(),
        now=clock.now(),
        lease_expires_at=clock.now() + timedelta(seconds=30),
    )
    assert claim is not None and claim.event.id == stored.event_id

    model = FakeModelAdapter().queue_text(observation_analysis_response())
    second = EventWorker(
        events,
        InboundEventDispatcher(
            {MANUAL_EVENT_TYPE: _handler(database, clock, runtime_root, model)}
        ),
        clock,
        RetryPolicy(),
        worker_id="second-worker",
        lease_duration=timedelta(seconds=30),
    )

    # Before the lease expires the event is not eligible: nothing is processed twice.
    assert await second.run_once() is WorkerResult.IDLE
    assert len(model.requests) == 0

    clock.advance(60)  # the lease is now expired
    assert await second.run_once() is WorkerResult.PROCESSED
    assert len(model.requests) == 1
    final = await events.get(stored.event_id)
    assert final is not None and final.status is EventStatus.PROCESSED
    stored_analysis = await SqliteObservationAnalysisRepository(database).get_analysis(
        stored.event_id
    )
    assert stored_analysis is not None


# ------------------------------------------------------------ ambiguous external result


async def test_an_unknown_execution_is_never_retried_after_a_restart(
    runtime_root: Path,
) -> None:
    """§52: an ambiguous external result stays unresolved, across processes."""
    clock = FakeClock(start=NOW)
    database = _open(runtime_root, clock)
    actions = SqliteActionRepository(database)
    cases = CaseService(SqliteCaseRepository(database), actions, clock)
    case = await cases.create_case("A send that may or may not have arrived")
    action = await cases.prepare_action(
        case.id, "mail.send", {"to": "ada@example.edu", "subject": "x", "body": "y"}
    )
    approvals = ApprovalService(actions, clock, token_factory=FixedTokenFactory(SECRET_TOKEN))
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    first_executor = FakeActionExecutor(action_type=action.action_type).script_unknown()
    first = await ActionExecutionService(
        actions, {action.action_type: first_executor}, clock
    ).execute(action.id)
    assert first.run.status.value == "unknown"
    assert len(first_executor.calls) == 1

    # A new process opens the same runtime and tries again, the way a scheduler restart would.
    clock_b = FakeClock(start=NOW + timedelta(hours=1))
    database_b = _open(runtime_root, clock_b)
    actions_b = SqliteActionRepository(database_b)
    second_executor = FakeActionExecutor(action_type=action.action_type)
    second = ActionExecutionService(
        actions_b, {action.action_type: second_executor}, clock_b
    )
    with pytest.raises(ActionExecutionUnresolved):
        await second.execute(action.id)
    assert second_executor.calls == []

    # A person may approve again, and it still does not execute: the unresolved outcome blocks it
    # until someone reconciles what actually happened.
    approvals_b = ApprovalService(
        actions_b,
        clock_b,
        token_factory=FixedTokenFactory("SECOND-APPROVAL-TOKEN-0123456789ABCDEF"),
    )
    issued_b = await approvals_b.create_challenge(action.id)
    await approvals_b.approve(action.id, issued_b.token)
    with pytest.raises(ActionExecutionUnresolved):
        await second.execute(action.id)
    assert second_executor.calls == []


# ------------------------------------------------------------------ log privacy sweep


async def test_a_full_lifecycle_keeps_its_sentinels_out_of_the_log(
    runtime_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """§55: one run, sentinels everywhere, and a clean log."""
    clock = FakeClock(start=NOW)
    database = _open(runtime_root, clock)
    manual_text = "SENTINEL-MANUAL-TEXT-KEEP-OUT-OF-LOGS"
    manual = ManualInputService(
        SqliteManualInputRepository(database),
        SqliteEventRepository(database, clock),
        clock,
    )
    await manual.create_input(manual_text, source="qq-forward")
    await TaskService(SqliteCommitmentRepository(database), clock).create_task(
        CreateTask(title="SENTINEL-TASK-TITLE-KEEP-OUT-OF-LOGS")
    )
    model = FakeModelAdapter().queue_text(observation_analysis_response())

    with caplog.at_level(logging.DEBUG):
        worker = EventWorker(
            SqliteEventRepository(database, clock),
            InboundEventDispatcher(
                {MANUAL_EVENT_TYPE: _handler(database, clock, runtime_root, model)}
            ),
            clock,
            RetryPolicy(),
            worker_id="privacy",
        )
        assert await worker.run_once() is WorkerResult.PROCESSED

    logged = caplog.text
    for sentinel in (manual_text, "SENTINEL-TASK-TITLE-KEEP-OUT-OF-LOGS"):
        assert sentinel not in logged
