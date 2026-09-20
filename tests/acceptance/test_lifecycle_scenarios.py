"""Acceptance: the whole lifecycle, end to end, with fakes only at the external edges (ADR-0031).

The scenarios walk the pipeline this project exists for — observe, understand, commit, plan,
execute, review, learn — over real SQLite, real stores, the real `EventWorker` and the real
application services. Only the outside world is scripted: the model, SMTP, the eHall page, the
watched HTTP page and the clock. The socket guard stays on for every one of them, so a scenario
that tried to reach the network would fail rather than pass quietly.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from assistant.adapters.mail.smtp import SmtpMailExecutor
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.mail_context import MailContextBuilder
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mail_event_handler import (
    MAIL_EVENT_TYPE,
    InboundEventDispatcher,
)
from assistant.application.mail_send_actions import (
    MAIL_SEND_ACTION_TYPE,
    MailSendActionService,
)
from assistant.application.manual_input_service import ManualInputService
from assistant.application.observation_context import ObservationContextBuilder
from assistant.application.observation_event_handler import (
    MANUAL_EVENT_TYPE,
    WEB_EVENT_TYPE,
    ObservationInboundEventHandler,
)
from assistant.application.planner_service import PlannerService
from assistant.application.playbook_replay import PlaybookReplayRegistry
from assistant.application.playbook_service import PlaybookService
from assistant.application.retry import RetryPolicy
from assistant.application.structured_model import StructuredModel
from assistant.application.task_service import CreateTask, TaskService
from assistant.application.web_watch import WebWatchService
from assistant.application.work_service import WorkService
from assistant.domain.config import (
    PlanningConfig,
    ReminderConfig,
    WatchersConfig,
    WebTargetConfig,
    Weekday,
    WeeklyAvailabilityRule,
)
from assistant.domain.errors import ApprovalUnavailable
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.mail_send import SqliteMailSendRepository
from assistant.store.manual_inputs import SqliteManualInputRepository
from assistant.store.migrations import apply_migrations
from assistant.store.observation_analyses import SqliteObservationAnalysisRepository
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.playbooks import SqlitePlaybookRepository
from assistant.store.web_watch import SqliteWebWatchRepository
from assistant.store.work import SqliteWorkRepository
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.mail_drafts import draft_response
from tests.support.mail_intelligence import MailStores, analysis_response, build_message
from tests.support.mail_send import (
    SEND_ACCOUNT_ID,
    SMTP_PASSWORD,
    StrictSmtpServer,
    assistant_config,
    smtp_account,
)
from tests.support.mobile import MobileStack, ScriptedTokenFactory
from tests.support.ops import NOW
from tests.support.watchers import (
    TARGET_ID,
    TARGET_URL,
    FakeWebSource,
    observation_analysis_response,
)


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(runtime_root: Path, clock: FakeClock) -> Database:
    db = Database.at(runtime_root / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


def _observation_handler(
    database: Database,
    clock: FakeClock,
    runtime_root: Path,
    model: FakeModelAdapter,
) -> ObservationInboundEventHandler:
    snapshots = WebSnapshotStore(runtime_root)
    return ObservationInboundEventHandler(
        SqliteWebWatchRepository(database),
        SqliteManualInputRepository(database),
        SqliteObservationAnalysisRepository(database),
        ObservationContextBuilder(snapshots, SqliteWebWatchRepository(database)),
        StructuredModel(model),
        clock,
    )


# --------------------------------------------------------- Scenario A: the lifecycle


async def test_scenario_a_observe_understand_commit_plan_review(
    database: Database, clock: FakeClock, runtime_root: Path
) -> None:
    """A forwarded QQ notice becomes a planned, worked-on, completed task — by hand, in order."""
    manual = ManualInputService(
        SqliteManualInputRepository(database), SqliteEventRepository(database, clock), clock
    )
    stored = await manual.create_input(
        "Forwarded: the SE lab report is due Friday 23:59.", source="qq-forward"
    )
    model = FakeModelAdapter().queue_text(
        observation_analysis_response(
            summary="A forwarded reminder that the lab report is due Friday."
        )
    )
    worker = EventWorker(
        SqliteEventRepository(database, clock),
        InboundEventDispatcher(
            {MANUAL_EVENT_TYPE: _observation_handler(database, clock, runtime_root, model)}
        ),
        clock,
        RetryPolicy(),
        worker_id="acceptance",
    )

    # Observe → understand: one durable analysis, and nothing created on its own.
    assert await worker.run_once() is WorkerResult.PROCESSED
    analysis = await SqliteObservationAnalysisRepository(database).get_analysis(
        stored.event_id
    )
    assert analysis is not None
    assert analysis.category.value == "actionable"
    candidate = analysis.action_candidates[0]
    assert candidate.text == "Submit the SE lab report"
    commitments = SqliteCommitmentRepository(database)
    assert await commitments.list_tasks() == []

    # Commit: the person turns the candidate into a task, keeping the deadline it carried.
    tasks = TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=ReminderConfig().deadline_offsets_minutes,
    )
    task = await tasks.create_task(
        CreateTask(
            title=candidate.text,
            estimated_minutes=120,
            due_at=candidate.interpreted_at,
        )
    )
    assert await tasks.get_deadline(task.id) is not None
    with database.connect() as connection:
        reminders = connection.execute(
            "SELECT count(*) AS t FROM scheduled_jobs WHERE kind = 'deadline_reminder'"
        ).fetchone()["t"]
    assert reminders >= 2  # the reminder offsets were materialized with the task

    # Plan: a deterministic proposal for the week, applied by hand.
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
                    ),
                    start_minute=9 * 60,
                    end_minute=22 * 60,
                ),
            ),
        ),
        clock,
    )
    proposal = await planner.create_week_proposal()
    applied = await planner.apply_proposal(str(proposal.proposal.id))
    assert applied.created_blocks >= 1

    # Work and finish: one recorded session, then the terminal transition.
    work = WorkService(SqliteWorkRepository(database), commitments, clock)
    session = await work.record_session(
        task_id=task.id,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=45),
    )
    assert await work.get_task_actual_seconds(task.id) == 45 * 60
    completed = await tasks.complete_task(task.id)

    assert session.id is not None
    assert completed.task.status.value == "completed"
    assert await tasks.list_tasks(include_terminal=False) == []  # no open work left
    assert [item.title for item in await tasks.list_tasks(include_terminal=True)] == [
        "Submit the SE lab report"
    ]


# ---------------------------------------------------- Scenario B: approved mail delivery


async def test_scenario_b_approved_mail_send_and_reviewed_playbook(
    database: Database, clock: FakeClock, runtime_root: Path
) -> None:
    """Mail in, analysis, draft, case, exact action, human approval, one SMTP DATA, one playbook."""
    mail = SqliteMailRepository(database)
    stores = MailStores(database, clock)
    message = await stores.store(
        build_message(
            message_id_header="<original@example.edu>",
            subject="SE lab deadline",
            body_text="Could you submit the report by Friday?",
        )
    )
    event = await stores.bridge(message)
    analysis_model = FakeModelAdapter().queue_text(analysis_response(summary="A request."))
    from tests.support.mail_intelligence import build_handler

    worker = EventWorker(
        stores.events,
        InboundEventDispatcher(
            {MAIL_EVENT_TYPE: build_handler(database, clock, analysis_model)}
        ),
        clock,
        RetryPolicy(),
        worker_id="acceptance",
    )
    assert await worker.run_once() is WorkerResult.PROCESSED
    assert event.id is not None

    actions = SqliteActionRepository(database)
    cases = CaseService(SqliteCaseRepository(database), actions, clock)
    case = await cases.create_case("Reply to Ada about the report")

    # A reply draft is an explicit act, made by the drafting service with a scripted provider.
    draft_model = FakeModelAdapter().queue_text(draft_response())
    drafts = MailDraftService(
        mail,
        SqliteMailIntelligenceRepository(database),
        SqliteMailDraftRepository(database),
        MailContextBuilder(mail, SqliteMailIntelligenceRepository(database)),
        None,
        StructuredModel(draft_model),
        clock,
    )
    drafted = await drafts.create_reply_draft(message.id)

    # Preparing a send freezes one exact version into an immutable action.
    config = assistant_config()
    send_actions = MailSendActionService(
        SqliteMailDraftRepository(database),
        mail,
        SqliteCaseRepository(database),
        actions,
        SqliteMailSendRepository(database),
        clock,
        accounts=config.mail.accounts,
        message_id_factory=lambda domain: f"<acceptance-1@{domain}>",
        date_header_factory=lambda now: "Thu, 25 Sep 2026 09:00:00 +0000",
    )
    preparation = await send_actions.prepare_send(drafted.draft.id, case.id)

    # The approved bytes are frozen: editing the draft afterwards changes nothing about them.
    await drafts.edit_draft(drafted.draft.id, body="Dear Ada, Monday instead.")

    approvals = ApprovalService(actions, clock, token_factory=FixedTokenFactory(SECRET_TOKEN))
    issued = await approvals.create_challenge(preparation.action.id)
    server = StrictSmtpServer()
    execution = ActionExecutionService(
        actions,
        {
            MAIL_SEND_ACTION_TYPE: SmtpMailExecutor(
                {SEND_ACCOUNT_ID: smtp_account()},
                password_lookup=lambda account_id: SMTP_PASSWORD,
                client_factory=lambda target: server,
            )
        },
        clock,
    )

    # No background approval exists: nothing can run before the human approves it.
    with pytest.raises(ApprovalUnavailable):
        await execution.execute(preparation.action.id)
    assert server.stages == []

    await approvals.approve(preparation.action.id, issued.token)
    result = await execution.execute(preparation.action.id)

    assert result.run.status.value == "succeeded"
    assert server.stages.count("data") == 1
    sent = server.sent_bytes
    assert sent is not None and b"Friday" in sent
    assert b"Monday instead" not in sent

    # Learning is a human act too: a candidate, a dry run, a promotion — and no SMTP traffic.
    playbooks = PlaybookService(
        SqlitePlaybookRepository(database),
        actions,
        clock,
        replay=PlaybookReplayRegistry.default(),
    )
    assert await playbooks.list_candidates(limit=None) == []
    candidate = await playbooks.create_candidate(
        preparation.action.id, name="Approved mail send", note="Reviewed the successful run."
    )
    test = await playbooks.test_candidate(candidate.id)
    playbook = await playbooks.promote_candidate(candidate.id)

    assert test.passed is True
    assert playbook.status.value == "active"
    assert server.stages.count("data") == 1


# ------------------------------------------------------------------ Scenario F: watcher


async def test_scenario_f_watcher_baseline_change_and_retry_reuse(
    database: Database, clock: FakeClock, runtime_root: Path
) -> None:
    """A watched page changes once, its analysis is paid for once, and a retry is free."""
    snapshots = WebSnapshotStore(runtime_root)
    source = FakeWebSource(
        {TARGET_URL: "Notices\nRegistration closes Oct 20.\n"}, snapshots=snapshots
    )
    service = WebWatchService(
        source,
        SqliteWebWatchRepository(database),
        SqliteEventRepository(database, clock),
        clock,
        WatchersConfig(web=(WebTargetConfig(id=TARGET_ID, url=TARGET_URL),)),
    )
    await service.sync_once()
    events = SqliteEventRepository(database, clock)
    assert await events.list_pending(limit=10) == []  # a baseline announces nothing

    source.set_page(TARGET_URL, "Notices\nRegistration closes Oct 25.\n")
    clock.advance(300)
    changed = await service.sync_once()
    assert changed[0].event_id is not None
    await service.sync_once()  # the same content again: no second event
    assert len(await events.list_pending(limit=10)) == 1

    model = FakeModelAdapter().queue_text(
        observation_analysis_response(
            category="informational", summary="The deadline moved."
        )
    )
    worker = EventWorker(
        events,
        InboundEventDispatcher(
            {WEB_EVENT_TYPE: _observation_handler(database, clock, runtime_root, model)}
        ),
        clock,
        RetryPolicy(),
        worker_id="acceptance",
    )
    assert await worker.run_once() is WorkerResult.PROCESSED
    assert len(model.requests) == 1

    # A crash and a retry reuse the stored analysis: the provider is not called again.
    with database.connect() as connection:
        connection.execute(
            "UPDATE inbound_events SET status = 'RECEIVED', attempts = 0, "
            "lease_expires_at = NULL, claim_token = NULL"
        )
    assert await worker.run_once() is WorkerResult.PROCESSED
    assert len(model.requests) == 1


# ------------------------------------------------------- Scenario E: mobile boundary


def test_scenario_e_mobile_may_approve_but_never_execute(
    database: Database, clock: FakeClock
) -> None:
    """A phone can review and approve an exact action; execution stays on the host."""
    stack = MobileStack(database, clock, ScriptedTokenFactory())
    actions = SqliteActionRepository(database)
    cases = CaseService(SqliteCaseRepository(database), actions, clock)

    async def _prepare():
        case = await cases.create_case("A case the phone may review")
        return await cases.prepare_action(case.id, "mail.send", {"to": "ada@example.edu"})

    import asyncio

    action = asyncio.run(_prepare())
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    detail = client.get(f"/api/actions/{action.id}")
    challenge = client.post(f"/api/actions/{action.id}/challenge", headers=headers).json()
    approved = client.post(
        f"/api/actions/{action.id}/approve",
        json={"token": challenge["token"]},
        headers=headers,
    )

    assert detail.status_code == 200
    assert detail.json()["fingerprint"] == action.fingerprint
    assert approved.status_code == 200
    assert approved.json()["executed"] is False
    assert asyncio.run(actions.count_executions(action.id)) == 0
