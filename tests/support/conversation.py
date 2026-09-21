"""A real Tree conversation runtime on a real database, with scripted model and mail (ADR-0033/34).

Nothing here fakes the runtime: the harness builds the same `ConversationService` the CLI builds
(through `bootstrap.conversation_service`), against a real migrated SQLite database, with only two
things replaced:

* the provider — `FakeModelAdapter`, so a test says exactly what the model "answered";
* the outbound SMTP transport — `ScriptedMailExecutor`, so a test says exactly how the send ended
  and can assert it happened at most once.

Inbound mail is *not* faked at the storage layer: `seed_mail` pushes raw RFC 822 bytes through the
real `MailSyncService` and the real parser, so what the conversation reads is what ingestion wrote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.calendar_service import CalendarService
from assistant.application.conversation_external_review import (
    ConversationExternalReviewService,
)
from assistant.application.conversation_service import ConversationService
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mail_send_status import MailSendStatusService
from assistant.application.mail_sync import MailSyncService
from assistant.application.planner_service import PlannerService
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.application.task_service import TaskService
from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.config import AssistantConfig
from assistant.domain.execution import ExecutionOutcome
from assistant.domain.mail import MailMessage
from assistant.store.actions import SqliteActionRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.conversation_reviews import SqliteConversationReviewRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.db import Database
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.recurring_calendar import SqliteRecurringCalendarRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock
from tests.support.mail_fakes import FakeMailSource

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
"""Monday 08:00 Asia/Shanghai — the fixed instant every conversation test runs at."""

MAIL_ACCOUNT = "\n".join(
    (
        "[mail]",
        "poll_interval_seconds = 60",
        "",
        "[[mail.accounts]]",
        'id = "smail"',
        'host = "imap.example.edu"',
        "port = 993",
        'username = "student@example.edu"',
        'mailbox = "INBOX"',
        "enabled = true",
        'smtp_host = "smtp.example.edu"',
        "smtp_port = 465",
        'smtp_security = "ssl"',
        'smtp_username = "student@example.edu"',
        'from_address = "student@example.edu"',
        "",
    )
)
"""One inbound account with an outbound half. No credential is ever read in tests."""

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "min_block_minutes = 30",
        "max_block_minutes = 120",
        "deadline_buffer_minutes = 120",
        "",
        "[[planning.availability]]",
        'days = ["mon", "tue", "wed", "thu", "fri"]',
        'start = "09:00"',
        'end = "22:00"',
        "",
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
        'reasoning_effort = "low"',
        "max_output_tokens = 4096",
        "timeout_seconds = 120",
        "",
        MAIL_ACCOUNT,
    )
)

CONFIG_WITHOUT_TIMEZONE = "\n".join(
    (
        "format_version = 1",
        "",
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
        'reasoning_effort = "low"',
        "max_output_tokens = 4096",
        "timeout_seconds = 120",
        "",
        MAIL_ACCOUNT,
    )
)

CONFIG_WITHOUT_MAIL = "\n".join(
    (
        "format_version = 1",
        "",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "",
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
        'reasoning_effort = "low"',
        "max_output_tokens = 4096",
        "timeout_seconds = 120",
        "",
    )
)


def write_config(tmp_path: Path, body: str = CONFIG) -> Path:
    """Write a host configuration and return its path."""
    path = tmp_path / "host" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def operation(
    operation_type: str, arguments: dict[str, Any] | None = None, note: str | None = None
) -> dict[str, Any]:
    """One operation object, as the provider would render it inside the schema."""
    return {"type": operation_type, "arguments": arguments or {}, "note": note}


def plan(
    *operations: dict[str, Any],
    mode: str = "operations",
    reply: str | None = None,
    clarification: str | None = None,
) -> str:
    """A complete model answer, serialised the way the adapter returns it."""
    return json.dumps(
        {
            "mode": mode,
            "reply": reply,
            "clarification": clarification,
            "operations": list(operations),
        }
    )


def direct_reply(text: str) -> str:
    """A `direct_reply` answer."""
    return plan(mode="direct_reply", reply=text)


def clarification(question: str) -> str:
    """A `clarification` answer."""
    return plan(mode="clarification", clarification=question)


def draft_answer(body: str, *, needs_user_input: tuple[str, ...] = ()) -> str:
    """A reply-draft answer in the shape `mail_reply_draft_v1` expects."""
    return json.dumps(
        {
            "body": body,
            "used_source_ids": [],
            "needs_user_input": list(needs_user_input),
        }
    )


class ScriptedMailExecutor:
    """An `ActionExecutor` for `mail.send` whose outcome the test chooses.

    It records every call, so "executed at most once" is an assertion rather than a hope, and no
    SMTP transport is ever constructed.
    """

    def __init__(self, outcome: ExecutionOutcome | Exception | None = None) -> None:
        self.outcome: ExecutionOutcome | Exception = outcome or ExecutionOutcome.succeeded()
        self.calls: list[ActionRequest] = []

    @property
    def action_type(self) -> ActionType:
        """The one action type this executor handles."""
        return ActionType("mail.send")

    def supports(self, action: ActionRequest) -> bool:
        """A scripted executor can always perform its own action type."""
        return action.action_type == self.action_type

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        """Record the call, then return or raise the scripted outcome."""
        self.calls.append(action)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@dataclass
class ConversationHarness:
    """Everything one conversation test needs to act and to check."""

    tmp_path: Path
    clock: FakeClock
    config: AssistantConfig
    database: Database
    model: FakeModelAdapter
    service: ConversationService
    conversations: SqliteConversationRepository
    commitments: SqliteCommitmentRepository
    planning: SqlitePlanningRepository
    scheduler: SqliteSchedulerRepository
    tasks: TaskService
    planner: PlannerService
    calendar: CalendarService
    recurring: RecurringCalendarService
    recurring_rules: SqliteRecurringCalendarRepository
    reviews: SqliteConversationReviewRepository
    actions: SqliteActionRepository
    mail: SqliteMailRepository
    intelligence: SqliteMailIntelligenceRepository
    drafts: SqliteMailDraftRepository
    draft_service: MailDraftService
    send_status: MailSendStatusService
    review_service: ConversationExternalReviewService
    executor: ScriptedMailExecutor
    mail_source: FakeMailSource
    mail_sync: MailSyncService | None

    def queue(self, *answers: str) -> FakeModelAdapter:
        """Queue model answers, in order."""
        for answer in answers:
            self.model.queue_text(answer)
        return self.model

    async def create_task(
        self,
        title: str,
        *,
        due_at: datetime | None = None,
        estimated_minutes: int | None = None,
    ):
        """Create a task through the real service, for tests that need prior state."""
        from assistant.application.task_service import CreateTask

        return await self.tasks.create_task(
            CreateTask(title=title, due_at=due_at, estimated_minutes=estimated_minutes)
        )

    async def task_titles(self) -> list[str]:
        """Every task title in the database, ordered by creation."""
        entries = await self.commitments.list_tasks(statuses=None)
        return [task.title for task in sorted(entries, key=lambda item: item.created_at)]

    async def seed_mail(
        self,
        *,
        sender: str = "teacher@example.edu",
        subject: str = "SE 实验三",
        body: str = "请在本周五之前提交实验报告。",
        uid: int = 1,
        message_id: str | None = None,
    ) -> MailMessage:
        """Deliver one message through the real ingestion path and return the stored message."""
        header_id = message_id or f"seed-{uid}@example.edu"
        raw = (
            f"From: {sender}\r\n"
            "To: student@example.edu\r\n"
            f"Subject: {subject}\r\n"
            f"Message-ID: <{header_id}>\r\n"
            "Date: Mon, 21 Sep 2026 09:00:00 +0800\r\n"
            "\r\n"
            f"{body}\r\n"
        )
        self.mail_source.add(uid, raw.encode("utf-8"))
        assert self.mail_sync is not None, "this harness was built without a mail account"
        await self.mail_sync.sync_once()
        messages = await self.mail.list_messages(limit=50)
        assert messages, "the seeded message was not stored"
        return next(
            (message for message in messages if message.message_id_header == f"<{header_id}>"),
            messages[0],
        )

    async def seed_mail_analysis(
        self, message: MailMessage, *, requires_reply: bool = True
    ) -> None:
        """Attach a stored analysis, the way the event worker would."""
        from assistant.domain.mail_analysis import MailAnalysis, MailCategory

        now = self.clock.now()
        await self.intelligence.persist_analysis(
            MailAnalysis(
                message_id=message.id,
                account_id=message.account_id,
                analyzer_version=1,
                input_fingerprint="f" * 64,
                category=MailCategory.REQUEST,
                requires_reply=requires_reply,
                summary="老师要求周五之前提交实验报告。",
                created_at=now,
                updated_at=now,
            )
        )

    async def waiting_reviews(self):
        """Live reviews of one thread, through the repository."""
        thread, _ = await self.service.resume_or_start()
        return await self.reviews.waiting_for_thread(thread.id)


async def build_harness(
    tmp_path: Path,
    *,
    config_body: str = CONFIG,
    start: datetime = NOW,
    executor: ScriptedMailExecutor | None = None,
) -> ConversationHarness:
    """Build the real conversation runtime over a fresh migrated database."""
    clock = FakeClock(start=start)
    database = Database.at(tmp_path / "data" / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await bootstrap.config_loader(write_config(tmp_path, config_body)).load()
    model = FakeModelAdapter()
    scripted = executor or ScriptedMailExecutor()
    executors = {ActionType("mail.send"): scripted}
    service = bootstrap.conversation_service(
        database, clock, config, model=model, executors=executors
    )
    mail_source = FakeMailSource()
    mail_sync = (
        bootstrap.mail_sync_service(
            config, clock, database, source_factory=lambda account: mail_source
        )
        if config.mail.accounts
        else None
    )
    return ConversationHarness(
        tmp_path=tmp_path,
        clock=clock,
        config=config,
        database=database,
        model=model,
        service=service,
        conversations=bootstrap.conversation_repository(database),
        commitments=bootstrap.commitment_repository(database),
        planning=bootstrap.planning_repository(database),
        scheduler=bootstrap.scheduler_repository(database),
        tasks=bootstrap.task_service(database, clock, config),
        planner=bootstrap.planner_service(database, clock, config),
        calendar=bootstrap.calendar_service(database, clock, config),
        recurring=bootstrap.recurring_calendar_service(database, clock, config),
        recurring_rules=bootstrap.recurring_calendar_repository(database),
        reviews=bootstrap.conversation_review_repository(database),
        actions=bootstrap.action_repository(database),
        mail=bootstrap.mail_repository(database),
        intelligence=bootstrap.mail_intelligence_repository(database),
        drafts=bootstrap.mail_draft_repository(database),
        draft_service=bootstrap.mail_draft_writer(config, clock, database, model=model),
        send_status=bootstrap.mail_send_status_service(clock, database),
        review_service=bootstrap.conversation_external_review_service(
            database, clock, config, executors=executors
        ),
        executor=scripted,
        mail_source=mail_source,
        mail_sync=mail_sync,
    )


def utc(hours: int = 0, minutes: int = 0, days: int = 0) -> datetime:
    """A convenience instant relative to the harness's `NOW`."""
    return NOW + timedelta(days=days, hours=hours, minutes=minutes)


__all__ = [
    "CONFIG",
    "CONFIG_WITHOUT_MAIL",
    "CONFIG_WITHOUT_TIMEZONE",
    "MAIL_ACCOUNT",
    "NOW",
    "ConversationHarness",
    "ScriptedMailExecutor",
    "build_harness",
    "clarification",
    "direct_reply",
    "draft_answer",
    "operation",
    "plan",
    "utc",
    "write_config",
]
