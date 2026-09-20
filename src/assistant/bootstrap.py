"""Composition helpers shared by the CLI and the daemon (ADR-0013).

This is a composition root: it may import concrete adapters and store implementations, and
its only job is to turn configuration into wired services. Application and domain layers stay
free of these imports — `tests/unit/test_architecture.py` enforces that.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path

from assistant.adapters.config.toml_config import TomlConfigLoader
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.adapters.mail.credentials import (
    available_password,
    available_smtp_password,
    require_password,
)
from assistant.adapters.mail.imap import ImapMailSource
from assistant.adapters.mail.message_ids import new_rfc_message_id
from assistant.adapters.mail.parser import Rfc822MailParser
from assistant.adapters.mail.raw_store import RawMailStore
from assistant.adapters.mail.sent_lookup import ImapSentMailLookup
from assistant.adapters.mail.smtp import SmtpMailExecutor, rfc2822_date
from assistant.adapters.model.deepseek import DeepSeekAdapter
from assistant.adapters.security.tokens import secure_approval_token_factory
from assistant.adapters.system_clock import SystemClock
from assistant.application.action_execution import ActionExecutionService
from assistant.application.action_service import ActionService
from assistant.application.approval_service import ApprovalService
from assistant.application.calendar_service import CalendarService
from assistant.application.case_service import CaseService
from assistant.application.event_inbox import EventInbox
from assistant.application.event_worker import EventWorker
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.grounded_answer import GroundedAnswerService
from assistant.application.grounded_context import GroundedContextBuilder
from assistant.application.index_sync import IndexSyncService
from assistant.application.interpreter import InterpreterService
from assistant.application.interpreter_context import InterpreterContextBuilder
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.mail_context import MailContextBuilder
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mail_event_handler import (
    MAIL_EVENT_TYPE,
    InboundEventDispatcher,
    MailInboundEventHandler,
)
from assistant.application.mail_send_actions import MailSendActionService
from assistant.application.mail_send_reconciliation import (
    MailSendReconciliationService,
)
from assistant.application.mail_send_status import MailSendStatusService
from assistant.application.mail_sync import MailSyncService
from assistant.application.mail_threading import MailThreadLinker
from assistant.application.paths import AppPaths
from assistant.application.planner_service import PlannerService
from assistant.application.retry import RetryPolicy
from assistant.application.rolling_replan import RollingReplanRequester
from assistant.application.scheduler_service import SchedulerService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.application.structured_model import StructuredModel
from assistant.application.task_service import TaskService
from assistant.application.work_service import WorkService
from assistant.domain.action import ActionType
from assistant.domain.config import (
    DEFAULT_MAIL_TIMEOUT_SECONDS,
    AssistantConfig,
    MailAccountConfig,
    ModelConfig,
    SchedulerConfig,
)
from assistant.domain.errors import (
    MailConfigurationError,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
from assistant.ports.action_executor import ActionExecutor
from assistant.ports.clock import Clock
from assistant.ports.mail_source import MailSource
from assistant.ports.model import ModelPort
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.mail_send import SqliteMailSendRepository
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.work import SqliteWorkRepository


def config_loader(path: Path | None = None) -> TomlConfigLoader:
    """Load and validate the host configuration."""
    return TomlConfigLoader(path)


def runtime_database(clock: Clock) -> Database:
    """Open the host runtime database and apply pending migrations synchronously."""
    database = Database.at(AppPaths.resolve().database_file)
    apply_migrations(database, clock=clock)
    return database


def catalog_repository(database: Database) -> SqliteCatalogRepository:
    """The host catalog store."""
    return SqliteCatalogRepository(database)


def case_repository(database: Database) -> SqliteCaseRepository:
    """The durable case container store."""
    return SqliteCaseRepository(database)


def action_repository(database: Database) -> SqliteActionRepository:
    """Durable actions, approval challenges, approvals and execution runs."""
    return SqliteActionRepository(database)


def case_service(clock: Clock, database: Database) -> CaseService:
    """Cases and the actions prepared inside them."""
    return CaseService(case_repository(database), action_repository(database), clock)


def action_service(clock: Clock, database: Database) -> ActionService:
    """Read-only views of actions, their approvals and their executions."""
    return ActionService(action_repository(database), clock)


def approval_service(clock: Clock, database: Database) -> ApprovalService:
    """Human approval of one exact action fingerprint.

    The token factory is the only randomness in the approval path, and it is injected here at the
    composition root rather than imported by the layers that use it.
    """
    return ApprovalService(
        action_repository(database), clock, token_factory=secure_approval_token_factory
    )


def registered_action_executors(
    config: AssistantConfig | None = None,
) -> dict[ActionType, ActionExecutor]:
    """The external capabilities this deployment can actually perform.

    One capability exists today: `mail.send`, and only when at least one account configures an
    outbound SMTP block. Everything else that can be named — `ehall.submit-certificate` — has no
    executor, so `pw action execute` answers `CapabilityUnavailable` without consuming an
    approval. A capability appears here only when it has been written and reviewed on purpose.
    """
    if config is None or not any(
        account.smtp_configured for account in config.mail.accounts
    ):
        return {}
    return {smtp_mail_executor(config).action_type: smtp_mail_executor(config)}


def smtp_mail_executor(config: AssistantConfig | None) -> SmtpMailExecutor:
    """The `mail.send` executor over this host's configured SMTP accounts.

    `supports` asks for a credential; `execute` uses it. Neither ever puts the secret in a
    payload, a log line or an error message.
    """
    accounts = () if config is None else config.mail.accounts
    return SmtpMailExecutor(
        {account.id: account for account in accounts},
        password_lookup=available_smtp_password,
        timeout_seconds=(
            DEFAULT_MAIL_TIMEOUT_SECONDS if config is None else config.mail.timeout_seconds
        ),
    )


def action_execution_service(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    executors: Mapping[ActionType, ActionExecutor] | None = None,
) -> ActionExecutionService:
    """The executor boundary, over the registered capability set."""
    return ActionExecutionService(
        action_repository(database),
        registered_action_executors(config) if executors is None else executors,
        clock,
    )


def mail_send_repository(database: Database) -> SqliteMailSendRepository:
    """Durable send links and Sent-folder reconciliation history."""
    return SqliteMailSendRepository(database)


def mail_send_action_service(
    config: AssistantConfig | None, clock: Clock, database: Database
) -> MailSendActionService:
    """Preparing an approvable send from one draft version.

    The Message-ID factory is the composition layer's job: the domain may not decide how a
    globally unique identifier is minted, and tests want to inject a deterministic one.
    """
    accounts = () if config is None else config.mail.accounts
    return MailSendActionService(
        mail_draft_repository(database),
        mail_repository(database),
        case_repository(database),
        action_repository(database),
        mail_send_repository(database),
        clock,
        accounts=accounts,
        message_id_factory=new_rfc_message_id,
        date_header_factory=rfc2822_date,
    )


def mail_send_status_service(
    clock: Clock, database: Database
) -> MailSendStatusService:
    """Read-only delivery state for prepared sends."""
    return MailSendStatusService(
        action_repository(database),
        mail_send_repository(database),
        mail_draft_repository(database),
        clock,
    )


def mail_send_reconciliation_service(
    config: AssistantConfig | None, clock: Clock, database: Database
) -> MailSendReconciliationService:
    """Sent-folder reconciliation, using the *inbound* credential (ADR-0024).

    A missing inbound credential is not an error here: it becomes an `UNAVAILABLE` record and the
    execution state is left exactly as it was.
    """
    accounts = () if config is None else config.mail.accounts
    lookup = None
    for account in accounts:
        if account.id and available_password(account.id) is not None:
            lookup = ImapSentMailLookup(
                host=account.host,
                username=account.username,
                password=require_password(account.id),
                port=account.port,
                timeout_seconds=(
                    DEFAULT_MAIL_TIMEOUT_SECONDS
                    if config is None
                    else config.mail.timeout_seconds
                ),
            )
            break
    return MailSendReconciliationService(
        action_repository(database),
        mail_send_repository(database),
        clock,
        lookup=lookup,
        accounts=accounts,
    )


def mail_repository(database: Database) -> SqliteMailRepository:
    """Durable inbound mail: messages, locations, attachments and the mailbox cursor."""
    return SqliteMailRepository(database)


def mail_intelligence_repository(database: Database) -> SqliteMailIntelligenceRepository:
    """Durable mail threads and analyses."""
    return SqliteMailIntelligenceRepository(database)


def mail_draft_repository(database: Database) -> SqliteMailDraftRepository:
    """Durable local reply drafts and their knowledge provenance."""
    return SqliteMailDraftRepository(database)


def raw_mail_store() -> RawMailStore:
    """Content-addressed raw message storage under the runtime data directory."""
    return RawMailStore(AppPaths.resolve().runtime / "mail")


def event_inbox(database: Database, clock: Clock) -> EventInbox:
    """The single ingestion entry point every external source writes through."""
    return EventInbox(SqliteEventRepository(database, clock), clock)


def mail_source(
    account: MailAccountConfig, *, timeout_seconds: int = DEFAULT_MAIL_TIMEOUT_SECONDS
) -> MailSource:
    """Build the IMAP source for one account.

    The credential is read here, at composition time, and never stored on the config object.

    Raises:
        MailCredentialsMissing: the environment holds no secret for this account.
    """
    return ImapMailSource(
        host=account.host,
        port=account.port,
        username=account.username,
        password=require_password(account.id),
        timeout_seconds=timeout_seconds,
    )


def mail_sync_service(
    config: AssistantConfig,
    clock: Clock,
    database: Database,
    *,
    source_factory: Callable[[MailAccountConfig], MailSource] | None = None,
) -> MailSyncService:
    """The daemon's mail service.

    Raises:
        MailConfigurationError: the host configured no mail accounts.
    """
    if not config.mail.accounts:
        raise MailConfigurationError("no mail accounts are configured")
    factory = source_factory or (
        lambda account: mail_source(
            account, timeout_seconds=config.mail.timeout_seconds
        )
    )
    return MailSyncService(
        config.mail,
        mail_repository(database),
        event_inbox(database, clock),
        raw_mail_store(),
        clock,
        factory,
        Rfc822MailParser(),
        waiter=AsyncioIntervalWaiter(),
    )


def catalog_service(
    clock: Clock, database: Database, *, manifests: VaultManifestFile | None = None
) -> StorageCatalogService:
    """Scanning service: physical path in, catalog update out."""
    return StorageCatalogService(
        FilesystemScanner(clock),
        manifests if manifests is not None else VaultManifestFile(clock),
        catalog_repository(database),
        clock,
    )


def knowledge_indexer(clock: Clock, database: Database) -> KnowledgeIndexer:
    """Knowledge indexer over the configured extractors and per-root indexes."""
    return KnowledgeIndexer(
        catalog_repository(database),
        SuffixExtractorRegistry(),
        SqliteKnowledgeIndexFactory(KnowledgeIndexLocator()),
        VaultManifestFile(clock),
        clock,
    )


def search_service(clock: Clock, database: Database) -> KnowledgeSearchService:
    """Search over every reachable root index plus the host catalog."""
    return KnowledgeSearchService(
        catalog_repository(database),
        SqliteKnowledgeIndexFactory(KnowledgeIndexLocator()),
        VaultManifestFile(clock),
    )


def sync_service(
    config: AssistantConfig, clock: Clock, database: Database
) -> IndexSyncService:
    """The periodic reconciliation service for configured roots."""
    manifests = VaultManifestFile(clock)
    return IndexSyncService(
        config,
        catalog_service(clock, database, manifests=manifests),
        knowledge_indexer(clock, database),
        manifests,
        clock,
        AsyncioIntervalWaiter(),
    )


def system_clock() -> SystemClock:
    """The production clock."""
    return SystemClock()


def commitment_repository(database: Database) -> SqliteCommitmentRepository:
    """The commitment store: tasks, deadlines, calendar events and plan blocks."""
    return SqliteCommitmentRepository(database)


def work_repository(database: Database) -> SqliteWorkRepository:
    """The work-session store."""
    return SqliteWorkRepository(database)


def scheduler_repository(database: Database) -> SqliteSchedulerRepository:
    """Durable scheduled jobs, their claims, and the notification inbox."""
    return SqliteSchedulerRepository(database)


def rolling_replan_requester(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> RollingReplanRequester:
    """Debounced replan requests, or a no-op requester when planning is not configured."""
    return RollingReplanRequester(
        scheduler_repository(database),
        clock,
        debounce_seconds=(
            SchedulerConfig().replan_debounce_seconds
            if config is None
            else config.scheduler.replan_debounce_seconds
        ),
        timezone=None if config is None or config.planning is None else config.planning.timezone,
    )


def task_service(
    database: Database, clock: Clock, config: AssistantConfig | None = None
) -> TaskService:
    """Tasks and deadlines, materializing deadline reminders inside the same mutations."""
    return TaskService(
        commitment_repository(database),
        clock,
        reminder_offsets_minutes=(
            () if config is None else config.reminders.deadline_offsets_minutes
        ),
        replan=rolling_replan_requester(database, clock, config),
    )


def calendar_service(
    database: Database, clock: Clock, config: AssistantConfig | None = None
) -> CalendarService:
    """Calendar events, plan blocks and busy time."""
    return CalendarService(
        commitment_repository(database),
        clock,
        replan=rolling_replan_requester(database, clock, config),
    )


def work_service(
    database: Database, clock: Clock, config: AssistantConfig | None = None
) -> WorkService:
    """Actual work sessions."""
    return WorkService(
        work_repository(database),
        commitment_repository(database),
        clock,
        replan=rolling_replan_requester(database, clock, config),
    )


def planning_repository(database: Database) -> SqlitePlanningRepository:
    """Proposals, planning snapshots and the atomic apply."""
    return SqlitePlanningRepository(database)


def planner_service(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> PlannerService:
    """The deterministic weekly planner over the configured planning preferences.

    `config=None` means "this command did not need host configuration"; the planner then
    reports `PlanningNotConfigured` instead of guessing a timezone.
    """
    return PlannerService(
        planning_repository(database),
        GreedyPlanner(),
        None if config is None else config.planning,
        clock,
    )


def scheduler_service(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> SchedulerService:
    """The daemon scheduler: executes due reminders and rolling replans."""
    scheduler_config = SchedulerConfig() if config is None else config.scheduler
    return SchedulerService(
        scheduler_repository(database),
        commitment_repository(database),
        work_repository(database),
        planner_service(database, clock, config),
        clock,
        RetryPolicy(),
        AsyncioIntervalWaiter(),
        poll_interval=timedelta(seconds=scheduler_config.poll_interval_seconds),
    )


MODEL_API_KEY_ENV = "DEEPSEEK_API_KEY"
"""Where the provider credential comes from. It is never read from host configuration."""


def model_api_key() -> str | None:
    """Return the provider credential from the environment, or `None` when unset."""
    value = os.environ.get(MODEL_API_KEY_ENV, "").strip()
    return value or None


def require_model_config(config: AssistantConfig | None) -> ModelConfig:
    """Return the `[model]` configuration or explain that this host has no model capability."""
    if config is None or config.model is None:
        raise ModelNotConfigured(
            "no [model] section in the host config; the model boundary is optional"
        )
    return config.model


def model_adapter(config: AssistantConfig | None) -> ModelPort:
    """Build the configured provider adapter.

    The credential is read here, at composition time, so that a host without a model
    configuration — or without a key — still runs every Phase 1-3 capability unchanged.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    if settings.provider != "deepseek":  # pragma: no cover - config validation guarantees it
        raise ModelNotConfigured(f"unsupported model provider {settings.provider!r}")
    api_key = model_api_key()
    if api_key is None:
        raise ModelCredentialsMissing(
            f"no credential available; set {MODEL_API_KEY_ENV} in the environment"
        )
    return DeepSeekAdapter(
        api_key=api_key,
        model=settings.model,
        timeout_seconds=settings.timeout_seconds,
    )


def structured_model(config: AssistantConfig | None) -> StructuredModel:
    """The structured-output service over the configured adapter."""
    return StructuredModel(model_adapter(config))


def mail_analysis_available(
    config: AssistantConfig | None, *, model: ModelPort | None = None
) -> bool:
    """Whether this host can analyze mail at all.

    Both halves are required: a `[model]` section *and* a credential. A host with mail accounts
    but no model keeps receiving mail and keeps producing `RECEIVED` events — it simply does not
    start a worker that would dead-letter every one of them.
    """
    if config is None or config.model is None:
        return False
    return model is not None or model_api_key() is not None


def mail_context_builder(
    database: Database, config: AssistantConfig | None
) -> MailContextBuilder:
    """The bounded, untrusted thread context an analysis is allowed to see."""
    return MailContextBuilder(
        mail_repository(database),
        mail_intelligence_repository(database),
        planning_timezone=(
            None if config is None or config.planning is None else config.planning.timezone
        ),
    )


def mail_event_handler(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
) -> MailInboundEventHandler:
    """The mail analysis handler over the configured adapter.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    return MailInboundEventHandler(
        mail_repository(database),
        mail_intelligence_repository(database),
        MailThreadLinker(mail_repository(database), mail_intelligence_repository(database), clock),
        mail_context_builder(database, config),
        StructuredModel(model if model is not None else model_adapter(config)),
        clock,
        reasoning_effort=settings.reasoning_effort,
        max_output_tokens=settings.max_output_tokens,
    )


def mail_event_worker(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
    worker_id: str = "event-worker",
) -> EventWorker:
    """The durable worker that turns received mail into analyses.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    handler = mail_event_handler(config, clock, database, model=model)
    return EventWorker(
        SqliteEventRepository(database, clock),
        InboundEventDispatcher({MAIL_EVENT_TYPE: handler}),
        clock,
        RetryPolicy(),
        worker_id=worker_id,
    )


def mail_draft_service(
    clock: Clock,
    database: Database,
    *,
    knowledge: GroundedContextBuilder | None = None,
) -> MailDraftService:
    """Reading and editing stored drafts. Local state only: no provider is constructed.

    Nothing in the daemon builds this service, so no mail, no event and no schedule can create a
    draft — or read the personal index — on its own.
    """
    return _mail_draft_service(clock, database, None, knowledge=knowledge)


def mail_draft_writer(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
    knowledge: GroundedContextBuilder | None = None,
) -> MailDraftService:
    """The same service, with the configured provider attached so a draft can be written.

    Only `pw mail draft create` builds this, and only because the user asked for a draft.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    return _mail_draft_service(
        clock,
        database,
        StructuredModel(model if model is not None else model_adapter(config)),
        knowledge=knowledge,
        config=config,
        reasoning_effort=settings.reasoning_effort,
        max_output_tokens=settings.max_output_tokens,
    )


def _mail_draft_service(
    clock: Clock,
    database: Database,
    model: StructuredModel | None,
    *,
    knowledge: GroundedContextBuilder | None = None,
    config: AssistantConfig | None = None,
    reasoning_effort: str = "low",
    max_output_tokens: int = 4096,
) -> MailDraftService:
    mail = mail_repository(database)
    intelligence = mail_intelligence_repository(database)
    return MailDraftService(
        mail,
        intelligence,
        mail_draft_repository(database),
        MailContextBuilder(
            mail,
            intelligence,
            planning_timezone=(
                None if config is None or config.planning is None else config.planning.timezone
            ),
        ),
        knowledge if knowledge is not None else grounded_context_builder(clock, database),
        model,
        clock,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )


def grounded_context_builder(
    clock: Clock, database: Database
) -> GroundedContextBuilder:
    """The read-only evidence builder: deterministic search plus the bounded budget."""
    return GroundedContextBuilder(search_service(clock, database))


def grounded_answer_service(
    context_builder: GroundedContextBuilder,
    config: AssistantConfig | None,
    *,
    model: ModelPort | None = None,
) -> GroundedAnswerService:
    """The grounded-answer service over the configured adapter.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    return GroundedAnswerService(
        context_builder,
        StructuredModel(model if model is not None else model_adapter(config)),
        settings,
    )


def interpreter_service(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    model: ModelPort | None = None,
) -> InterpreterService:
    """The natural-language interpreter: bounded context in, typed draft out.

    Only `pw interpret` builds this, and only for the duration of one command: the daemon never
    constructs a model client, so a missing credential can never affect it.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    planning_timezone = (
        None if config is None or config.planning is None else config.planning.timezone
    )
    return InterpreterService(
        StructuredModel(model if model is not None else model_adapter(config)),
        InterpreterContextBuilder(
            commitment_repository(database), clock, planning_timezone=planning_timezone
        ),
        settings,
    )


async def close_model(model: ModelPort) -> None:
    """Release adapter resources, without making `ModelPort` promise a lifecycle."""
    close = getattr(model, "aclose", None)
    if callable(close):
        await close()


__all__ = [
    "MODEL_API_KEY_ENV",
    "AppPaths",
    "VaultManifestFile",
    "action_execution_service",
    "action_repository",
    "action_service",
    "approval_service",
    "calendar_service",
    "case_repository",
    "case_service",
    "catalog_repository",
    "catalog_service",
    "close_model",
    "commitment_repository",
    "config_loader",
    "event_inbox",
    "grounded_answer_service",
    "grounded_context_builder",
    "interpreter_service",
    "knowledge_indexer",
    "mail_analysis_available",
    "mail_context_builder",
    "mail_draft_repository",
    "mail_draft_service",
    "mail_draft_writer",
    "mail_event_handler",
    "mail_event_worker",
    "mail_intelligence_repository",
    "mail_repository",
    "mail_send_action_service",
    "mail_send_reconciliation_service",
    "mail_send_repository",
    "mail_send_status_service",
    "mail_source",
    "mail_sync_service",
    "model_adapter",
    "model_api_key",
    "planner_service",
    "planning_repository",
    "raw_mail_store",
    "registered_action_executors",
    "require_model_config",
    "rolling_replan_requester",
    "runtime_database",
    "scheduler_repository",
    "scheduler_service",
    "search_service",
    "smtp_mail_executor",
    "structured_model",
    "sync_service",
    "system_clock",
    "task_service",
    "work_repository",
    "work_service",
]
