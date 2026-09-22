"""Composition helpers shared by the CLI and the daemon (ADR-0013).

This is a composition root: it may import concrete adapters and store implementations, and
its only job is to turn configuration into wired services. Application and domain layers stay
free of these imports — `tests/unit/test_architecture.py` enforces that.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx

from assistant.adapters.backup.archive import ZipBackupArchive
from assistant.adapters.config.mail_overlay import OverlayMailSettingsStore, overlay_path
from assistant.adapters.config.toml_config import TomlConfigLoader, default_config_path
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.ehall.executor import EHallCertificateExecutor
from assistant.adapters.ehall.nju_certificate import NjuCertificateGateway
from assistant.adapters.ehall.page import PlaywrightEHallPage
from assistant.adapters.ehall.session import EHallBrowserSession
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.adapters.mail.connection_test import MailConnectionProbes
from assistant.adapters.mail.credentials import (
    EnvironmentMailCredentials,
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
from assistant.adapters.ops.content_objects import RuntimeContentObjects
from assistant.adapters.runtime.permissions import LocalFileModes
from assistant.adapters.security.tokens import (
    secure_approval_token_factory,
    secure_mobile_token_factory,
)
from assistant.adapters.system_clock import SystemClock
from assistant.adapters.web.app import WebDependencies
from assistant.adapters.web.server import MobileWebService
from assistant.adapters.web_watch.http_source import HttpWebSource
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.application.action_execution import ActionExecutionService
from assistant.application.action_service import ActionService
from assistant.application.approval_service import ApprovalService
from assistant.application.attention import AttentionProjector, AttentionService
from assistant.application.backup_service import MIGRATION_DIRECTORY, BackupService
from assistant.application.calendar_service import CalendarService
from assistant.application.case_service import CaseService
from assistant.application.contacts import ContactService
from assistant.application.conversation_capabilities import (
    ConversationCapabilityRegistry,
    ConversationHandlers,
    build_phase_10a_registry,
)
from assistant.application.conversation_capabilities.introspection import (
    build_capability_snapshot,
)
from assistant.application.conversation_cards import ConversationCardService
from assistant.application.conversation_chat import ConversationChatService
from assistant.application.conversation_context import ConversationContextBuilder
from assistant.application.conversation_event_broker import ConversationEventBroker
from assistant.application.conversation_external_review import (
    ConversationExternalReviewService,
)
from assistant.application.conversation_interpreter import ConversationInterpreterService
from assistant.application.conversation_request_coordinator import (
    ConversationRequestCoordinator,
)
from assistant.application.conversation_service import ConversationService
from assistant.application.conversational_facts import ConversationalFactService
from assistant.application.ehall_certificate import EHallCertificateService
from assistant.application.event_inbox import EventInbox
from assistant.application.event_worker import EventWorker
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.grounded_answer import GroundedAnswerService
from assistant.application.grounded_context import GroundedContextBuilder
from assistant.application.index_sync import IndexSyncService
from assistant.application.integrity_service import IntegrityService
from assistant.application.interpreter import InterpreterService
from assistant.application.interpreter_context import InterpreterContextBuilder
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.learning_service import LearningService
from assistant.application.mail_account_settings import MailAccountSettingsService
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
from assistant.application.manual_input_service import ManualInputService
from assistant.application.mcp_facade import McpFacade
from assistant.application.mobile_auth import MobileAuthService
from assistant.application.new_mail_drafts import NewMailDraftService
from assistant.application.observation_context import ObservationContextBuilder
from assistant.application.observation_event_handler import (
    MANUAL_EVENT_TYPE,
    ObservationInboundEventHandler,
)
from assistant.application.observation_event_handler import (
    WEB_EVENT_TYPE as OBSERVATION_WEB_EVENT_TYPE,
)
from assistant.application.paths import AppPaths
from assistant.application.planner_service import PlannerService
from assistant.application.planning_preferences_service import PlanningPreferencesService
from assistant.application.playbook_replay import PlaybookReplayRegistry
from assistant.application.playbook_service import PlaybookService
from assistant.application.recipient_resolution import RecipientResolver
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.application.retry import RetryPolicy
from assistant.application.rolling_replan import RollingReplanRequester
from assistant.application.scheduler_service import SchedulerService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.application.structured_model import StructuredModel
from assistant.application.task_service import TaskService
from assistant.application.today_brief import TodayBriefService
from assistant.application.web_watch import WebWatchService
from assistant.application.work_service import WorkService
from assistant.domain.action import ActionType
from assistant.domain.config import (
    DEFAULT_EHALL_TIMEOUT_SECONDS,
    DEFAULT_MAIL_TIMEOUT_SECONDS,
    AssistantConfig,
    MailAccountConfig,
    MobileConfig,
    ModelConfig,
    SchedulerConfig,
)
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    InvalidAssistantConfig,
    MailConfigurationError,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
from assistant.ports.action_executor import ActionExecutor
from assistant.ports.clock import Clock
from assistant.ports.event_handler import EventHandler
from assistant.ports.interval_waiter import IntervalWaiter
from assistant.ports.mail_source import MailSource
from assistant.ports.model import ModelPort
from assistant.ports.web_source import WebSource
from assistant.store.actions import SqliteActionRepository
from assistant.store.attention import SqliteAttentionRepository
from assistant.store.backup import SqliteRuntimeBackup
from assistant.store.cases import SqliteCaseRepository
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.contacts import SqliteContactRepository
from assistant.store.conversation_requests import SqliteConversationRequestRepository
from assistant.store.conversation_reviews import SqliteConversationReviewRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.integrity import SqliteIntegrityRepository
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.learning import SqliteLearningRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.mail_send import SqliteMailSendRepository
from assistant.store.manual_inputs import SqliteManualInputRepository
from assistant.store.migrations import apply_migrations, require_compatible_history
from assistant.store.mobile_sessions import SqliteMobileSessionRepository
from assistant.store.new_mail_drafts import SqliteNewMailDraftRepository
from assistant.store.observation_analyses import SqliteObservationAnalysisRepository
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.planning_preferences import SqlitePlanningPreferencesRepository
from assistant.store.playbooks import SqlitePlaybookRepository
from assistant.store.recurring_calendar import SqliteRecurringCalendarRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from assistant.store.web_watch import SqliteWebWatchRepository
from assistant.store.work import SqliteWorkRepository


def config_loader(path: Path | None = None) -> TomlConfigLoader:
    """Load and validate the host configuration."""
    return TomlConfigLoader(path)


def runtime_database(clock: Clock) -> Database:
    """Open the host runtime database, refuse a newer schema, apply pending migrations.

    The compatibility check comes first: a runtime written by a newer binary must fail closed here,
    before any command mutates it (ADR-0032). Forward migrations for a database this build *does*
    understand are applied synchronously, exactly as before.
    """
    database = Database.at(AppPaths.resolve().database_file)
    require_compatible_history(database)
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


def learning_repository(database: Database) -> SqliteLearningRepository:
    """Durable corrections, candidate facts and confirmed personal facts."""
    return SqliteLearningRepository(database)


def learning_service(clock: Clock, database: Database) -> LearningService:
    """Personal facts a human proposed and confirmed.

    Composed by the explicit `pw fact` commands and, since Phase 10F, by the conversation's own
    fact orchestration — where the model may still only *propose*, and the promotion itself is a
    deterministic response to the human's own words. No worker, no daemon service and no web route
    reaches it, which is what keeps promotion a human act.
    """
    return LearningService(learning_repository(database), clock)


def playbook_repository(database: Database) -> SqlitePlaybookRepository:
    """Durable playbook candidates, replay tests and playbooks."""
    return SqlitePlaybookRepository(database)


def playbook_replay_registry() -> PlaybookReplayRegistry:
    """The two dry-run validators this project has, registered explicitly.

    A registry rather than a lookup by convention: a capability can only be dry-run here if a
    validator was written and reviewed for it, which is what makes "no candidate without a
    possible test" true.
    """
    return PlaybookReplayRegistry.default()


def playbook_service(clock: Clock, database: Database) -> PlaybookService:
    """Candidate review, side-effect-free dry runs and human promotion.

    Composed for the explicit `pw playbook` commands and nothing else: no worker, no daemon
    service and no web route can create, test or promote a playbook.
    """
    return PlaybookService(
        playbook_repository(database),
        action_repository(database),
        clock,
        replay=playbook_replay_registry(),
    )


def action_service(clock: Clock, database: Database) -> ActionService:
    """Read-only views of actions, their approvals and their executions."""
    return ActionService(action_repository(database), clock)


def web_snapshot_store() -> WebSnapshotStore:
    """Content-addressed storage for normalized watcher text (ADR-0029)."""
    return WebSnapshotStore(AppPaths.resolve().runtime)


def web_source() -> HttpWebSource:
    """The one implementation of `WebSource`: no cookies, no redirects, public hosts only.

    `trust_env=False` is deliberate: an ambient `HTTPS_PROXY` or a CA override in the environment
    would silently send a watcher somewhere else, and a watcher is supposed to talk to exactly the
    page the host configured.
    """
    return HttpWebSource(
        lambda: httpx.AsyncClient(trust_env=False),
        web_snapshot_store(),
    )


def web_watch_repository(database: Database) -> SqliteWebWatchRepository:
    """Durable watcher state, versioned observations and their event links."""
    return SqliteWebWatchRepository(database)


def web_watch_service(
    config: AssistantConfig,
    clock: Clock,
    database: Database,
    *,
    source: WebSource | None = None,
    waiter: IntervalWaiter | None = None,
) -> WebWatchService:
    """The daemon's watcher service over the configured targets.

    Raises:
        InvalidAssistantConfig: the host configured no enabled watcher targets.
    """
    if not config.watchers.enabled_targets:
        raise InvalidAssistantConfig("no web watcher targets are configured")
    return WebWatchService(
        source if source is not None else web_source(),
        web_watch_repository(database),
        SqliteEventRepository(database, clock),
        clock,
        config.watchers,
        waiter=waiter if waiter is not None else AsyncioIntervalWaiter(),
    )


def manual_input_repository(database: Database) -> SqliteManualInputRepository:
    """Durable manual input and its links to the event inbox."""
    return SqliteManualInputRepository(database)


def manual_input_service(clock: Clock, database: Database) -> ManualInputService:
    """Storing pasted text and queueing it for analysis.

    Composed for the `pw ingest` commands and for nothing else: no worker, no daemon service and no
    web route can create a manual input on someone's behalf.
    """
    return ManualInputService(
        manual_input_repository(database),
        SqliteEventRepository(database, clock),
        clock,
    )


def observation_analysis_repository(
    database: Database,
) -> SqliteObservationAnalysisRepository:
    """Durable analyses of web changes and manual input."""
    return SqliteObservationAnalysisRepository(database)


def observation_context_builder(database: Database) -> ObservationContextBuilder:
    """The bounded untrusted context a web/manual analysis may see."""
    return ObservationContextBuilder(
        web_snapshot_store(), web_watch_repository(database)
    )


def observation_event_handler(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
) -> ObservationInboundEventHandler:
    """The handler that classifies a web change or a manual input. Nothing else.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    settings = require_model_config(config)
    return ObservationInboundEventHandler(
        web_watch_repository(database),
        manual_input_repository(database),
        observation_analysis_repository(database),
        observation_context_builder(database),
        StructuredModel(model if model is not None else model_adapter(config)),
        clock,
        reasoning_effort=settings.reasoning_effort,
        max_output_tokens=settings.max_output_tokens,
    )


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
    executors: dict[ActionType, ActionExecutor] = {}
    smtp = smtp_mail_executor(config)
    if any(account.smtp_configured for account in accounts_of(config)):
        executors[smtp.action_type] = smtp
    if config is not None and config.ehall.enabled:
        ehall = ehall_certificate_executor(config)
        executors[ehall.action_type] = ehall
    return executors


def accounts_of(config: AssistantConfig | None) -> tuple[MailAccountConfig, ...]:
    """The configured mail accounts, or none."""
    return () if config is None else config.mail.accounts


def smtp_mail_executor(config: AssistantConfig | None) -> SmtpMailExecutor:
    """The `mail.send` executor over this host's configured SMTP accounts.

    `supports` asks for a credential; `execute` uses it. Neither ever puts the secret in a
    payload, a log line or an error message.
    """
    return SmtpMailExecutor(
        {account.id: account for account in accounts_of(config)},
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


def ehall_certificate_gateway(config: AssistantConfig | None) -> NjuCertificateGateway:
    """The whitelisted certificate pipeline over a headed, manually-logged-in browser.

    The session is created lazily, and only when the pipeline actually runs: `pw ehall status` and
    `pw doctor` must be able to describe this capability without opening a browser.
    """
    timeout = DEFAULT_EHALL_TIMEOUT_SECONDS if config is None else config.ehall.timeout_seconds
    return NjuCertificateGateway(
        lambda: EHallBrowserSession(timeout_seconds=timeout),
        lambda session: _open_ehall_page(session, timeout_seconds=timeout),
        enabled=config is not None and config.ehall.enabled,
        timeout_seconds=timeout,
    )


async def _open_ehall_page(
    session: EHallBrowserSession, *, timeout_seconds: int
) -> PlaywrightEHallPage:
    """Open one page in the session, with the adapter's own default timeout."""
    del timeout_seconds
    return PlaywrightEHallPage(await session.new_page())


def ehall_certificate_executor(
    config: AssistantConfig | None,
) -> EHallCertificateExecutor:
    """The `ehall.submit-certificate` executor, when the pipeline is enabled."""
    return EHallCertificateExecutor(ehall_certificate_gateway(config))


def ehall_certificate_service(
    config: AssistantConfig | None, clock: Clock, database: Database
) -> EHallCertificateService:
    """Inspecting the certificate form and preparing an approvable submission."""
    return EHallCertificateService(
        ehall_certificate_gateway(config),
        case_repository(database),
        action_repository(database),
        clock,
        enabled=config is not None and config.ehall.enabled,
    )


def mobile_session_repository(database: Database) -> SqliteMobileSessionRepository:
    """Durable pairing tokens and web sessions, stored as hashes."""
    return SqliteMobileSessionRepository(database)


def mobile_auth_service(
    config: AssistantConfig | None, clock: Clock, database: Database
) -> MobileAuthService:
    """Pairing, sessions and CSRF for the same-LAN control plane.

    The token factory is injected here, at the composition root, for the same reason the approval
    token is: the application layer may not decide how a secret is minted.
    """
    return MobileAuthService(
        mobile_session_repository(database),
        clock,
        token_factory=secure_mobile_token_factory,
        enabled=config is not None and config.mobile.enabled,
    )


def _deadline_lookup(
    database: Database,
) -> Callable[[Sequence[object]], Awaitable[dict[UUID, Deadline]]]:
    """`async (tasks) -> {task_id: Deadline}` over the commitment store."""
    commitments = commitment_repository(database)

    async def _lookup(tasks: Sequence[object]) -> dict[UUID, Deadline]:
        identifiers = [task.id for task in tasks]  # type: ignore[attr-defined]
        if not identifiers:
            return {}
        return await commitments.list_deadlines(identifiers)

    return _lookup


def mobile_web_dependencies(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    chat: ConversationChatService | None = None,
    config_path: Path | None = None,
) -> WebDependencies:
    """The application services the control plane may speak to, and nothing else.

    Read it as a permission list: tasks, cases, drafts, actions, approvals, notifications, auth —
    and, when this host has a conversation runtime, the Tree chat surface. There is no execution
    service here, no SMTP executor and no eHall gateway.
    """
    return WebDependencies(
        auth=mobile_auth_service(config, clock, database),
        tasks=task_service(database, clock, config),
        cases=case_service(clock, database),
        drafts=mail_draft_service(clock, database),
        actions=action_service(clock, database),
        approvals=approval_service(clock, database),
        notifications=scheduler_repository(database),
        deadlines=_deadline_lookup(database),
        clock=clock,
        chat=chat,
        attention=attention_service(database, clock, config),
        settings=mail_account_settings_service(config, clock, config_path=config_path),
        planning_preferences=planning_preferences_service(database, clock, config),
        planning_timezone=_planning_timezone_of(config),
    )


def mobile_web_service(
    config: AssistantConfig | None,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
    chat: ConversationChatService | None = None,
    config_path: Path | None = None,
) -> MobileWebService:
    """The supervised mobile web service, when the control plane is enabled.

    The Tree chat surface is mounted when — and only when — this host can actually hold a
    conversation. A host without `[model]` keeps the existing control plane and gets no `/chat`,
    which is more honest than a shell that cannot answer anything.
    """
    mobile = MobileConfig() if config is None else config.mobile
    surface = chat
    if surface is None:
        with contextlib.suppress(ModelNotConfigured, ModelCredentialsMissing):
            surface = conversation_chat_service(database, clock, config, model=model)
    return MobileWebService(
        mobile_web_dependencies(config, clock, database, chat=surface, config_path=config_path),
        bind=mobile.bind_mode,
        port=mobile.port,
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
        new_drafts=new_mail_draft_repository(database),
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
        new_drafts=new_mail_draft_repository(database),
    )


def contact_repository(database: Database) -> SqliteContactRepository:
    """Durable local contacts (ADR-0037 §9)."""
    return SqliteContactRepository(database)


def conversational_fact_service(
    database: Database, clock: Clock
) -> ConversationalFactService:
    """Fact proposals a conversation may make, over the existing learning service (ADR-0038)."""
    return ConversationalFactService(learning_service(clock, database))


def mail_account_settings_service(
    config: AssistantConfig | None, clock: Clock, *, config_path: Path | None = None
) -> MailAccountSettingsService:
    """Mail account metadata, its safe views and its connectivity diagnostics (ADR-0043).

    The accounts handed in are already the *effective* list — the loader merges the managed overlay
    — so a write that replaces the overlay with them loses nothing. `credentials` is the
    environment, which is the only secret source this project has, and it exposes existence and
    reference names rather than values.
    """
    path = Path(config_path) if config_path is not None else default_config_path()
    return MailAccountSettingsService(
        store=OverlayMailSettingsStore(overlay_path(path)),
        accounts=() if config is None else config.mail.accounts,
        credentials=EnvironmentMailCredentials(),
        clock=clock,
        probe=MailConnectionProbes(),
    )


def attention_repository(database: Database) -> SqliteAttentionRepository:
    """Durable, derived user-facing attention items (ADR-0042)."""
    return SqliteAttentionRepository(database)


def attention_service(
    database: Database, clock: Clock, config: AssistantConfig | None = None
) -> AttentionService:
    """The bounded read/settle surface over the attention inbox.

    With a `config`, the service also owns the projector and reconciles before a caller reads, which
    is what makes "有什么需要我处理的" answer for this moment. Without one (an integrity or CLI path
    that only needs to read rows) it is a pure reader.
    """
    return AttentionService(
        items=attention_repository(database),
        clock=clock,
        projector=None if config is None else attention_projector(database, clock, config),
    )


def attention_projector(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> AttentionProjector:
    """Deterministic reconciliation of the inbox with the durable sources behind it (ADR-0042).

    The projector is purely local: no model, no socket, no executor. It reads bounded source
    collections and writes at most one derived row per pending situation.
    """
    return AttentionProjector(
        items=attention_repository(database),
        commitments=commitment_repository(database),
        planning=planning_repository(database),
        scheduler=scheduler_repository(database),
        conversations=conversation_repository(database),
        reviews=conversation_review_repository(database),
        clock=clock,
        mail=mail_repository(database),
        mail_intelligence=mail_intelligence_repository(database),
        learning=learning_repository(database),
        sends=mail_send_status_service(clock, database),
        observations=web_watch_repository(database),
        analyses=observation_analysis_repository(database),
        planning_timezone=_planning_timezone_of(config),
    )


def today_brief_service(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    refresh_attention: bool = True,
) -> TodayBriefService:
    """The deterministic today brief over existing local state (ADR-0039).

    `config=None` means "this command did not need host configuration", so there is no planning
    timezone — and the service then refuses to guess which day "today" is.

    `refresh_attention=False` is for the one caller that already refreshes on its own schedule: the
    chat home surface, which is rebuilt after every streamed event and would otherwise reconcile the
    inbox again each time.
    """
    return TodayBriefService(
        commitments=commitment_repository(database),
        planning=planning_repository(database),
        scheduler=scheduler_repository(database),
        conversations=conversation_repository(database),
        attention=attention_service(database, clock, config if refresh_attention else None),
        mail=mail_repository(database),
        mail_intelligence=mail_intelligence_repository(database),
        sends=mail_send_status_service(clock, database),
        recurring=recurring_calendar_service(database, clock, config),
        facts=conversational_fact_service(database, clock),
        clock=clock,
        planning_timezone=_planning_timezone_of(config),
    )


def contact_service(database: Database, clock: Clock) -> ContactService:
    """Contacts a name may resolve to."""
    return ContactService(contact_repository(database), clock)


def new_mail_draft_repository(database: Database) -> SqliteNewMailDraftRepository:
    """Durable new-mail drafts (ADR-0037 §17)."""
    return SqliteNewMailDraftRepository(database)


def new_mail_draft_service(database: Database, clock: Clock) -> NewMailDraftService:
    """Writing and revising new-mail drafts. It cannot send anything."""
    return NewMailDraftService(new_mail_draft_repository(database), clock)


def recipient_resolver(
    database: Database, config: AssistantConfig | None
) -> RecipientResolver:
    """The deterministic recipient and sender-account resolver (ADR-0037 §10-§14).

    `config=None` means "this command did not need host configuration", so there are no accounts —
    and the resolver then says so instead of guessing one.
    """
    return RecipientResolver(
        contact_repository(database),
        accounts=() if config is None else config.mail.accounts,
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


def assistant_version() -> str:
    """The installed application version, as reported to an MCP client."""
    from assistant import __version__

    return __version__


def runtime_backup(clock: Clock) -> SqliteRuntimeBackup:
    """The SQLite-backed snapshotter for the host runtime database."""
    return SqliteRuntimeBackup(AppPaths.resolve().database_file)


def backup_service(clock: Clock) -> BackupService:
    """Operational backup, verification and staging restore over the runtime directory."""
    return BackupService(
        AppPaths.resolve().runtime,
        runtime_backup(clock),
        clock,
        ZipBackupArchive(),
        application_version=assistant_version(),
    )


def runtime_content_objects() -> RuntimeContentObjects:
    """Reads referenced content objects (raw mail, web snapshots) out of the runtime directory."""
    return RuntimeContentObjects(AppPaths.resolve().runtime)


def integrity_service(clock: Clock, database: Database) -> IntegrityService:
    """The offline, read-only integrity check over the runtime database and its objects."""
    return IntegrityService(
        SqliteIntegrityRepository(database),
        runtime_content_objects(),
        migrations_directory=MIGRATION_DIRECTORY,
        runtime_root=AppPaths.resolve().runtime,
        file_modes=LocalFileModes(),
    )


def mcp_facade(
    config: AssistantConfig,
    *,
    clock: Clock | None = None,
    database: Database | None = None,
) -> McpFacade:
    """The bounded application surface the local MCP server is allowed to speak to.

    Every capability the surface has comes from a service that already existed — `TaskService`,
    `CaseService`, the commitment read model, the deterministic planner window, the notification
    inbox and (only when the host opts in) the local knowledge search. Nothing that can approve,
    execute, send, submit, confirm, promote or reach a network is constructed here.
    """
    chosen_clock = clock if clock is not None else system_clock()
    chosen_database = database if database is not None else runtime_database(chosen_clock)
    knowledge = search_service(chosen_clock, chosen_database)
    return McpFacade(
        task_service(chosen_database, chosen_clock, config),
        case_service(chosen_clock, chosen_database),
        commitment_repository(chosen_database),
        planner_service(chosen_database, chosen_clock, config),
        scheduler_repository(chosen_database),
        chosen_clock,
        version=assistant_version(),
        write_scope=config.mcp.write_scope,
        expose_knowledge=config.mcp.expose_knowledge,
        knowledge=knowledge,
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


def planning_preferences_service(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> PlanningPreferencesService:
    """Durable capacity rules for the deterministic planner (ADR-0044)."""
    return PlanningPreferencesService(
        SqlitePlanningPreferencesRepository(database, clock),
        clock,
        config=None if config is None else config.planning,
    )


def planner_service(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> PlannerService:
    """The deterministic weekly planner over the configured planning preferences.

    `config=None` means "this command did not need host configuration"; the planner then
    reports `PlanningNotConfigured` instead of guessing a timezone. Weekly commitments are handed
    in as busy time, so a plan block is never proposed over a class (ADR-0036 §13).
    """
    return PlannerService(
        planning_repository(database),
        GreedyPlanner(),
        None if config is None else config.planning,
        clock,
        recurring=recurring_calendar_service(database, clock, config),
        preferences=planning_preferences_service(database, clock, config),
    )


def recurring_calendar_repository(database: Database) -> SqliteRecurringCalendarRepository:
    """Durable weekly recurring rules (ADR-0036 §3)."""
    return SqliteRecurringCalendarRepository(database)


def recurring_calendar_service(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> RecurringCalendarService:
    """Weekly commitments, interpreted in the configured planning timezone by default.

    `default_timezone=None` means "this host has not chosen one"; the service then refuses to
    interpret a bare weekday and time rather than reaching for the machine's timezone.
    """
    return RecurringCalendarService(
        recurring_calendar_repository(database),
        clock,
        default_timezone=_planning_timezone_of(config),
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
    """The durable worker that turns received events into analyses.

    Three event types are dispatched here, one handler each: inbound mail, a change on a watched
    page, and text a person pasted in. The map is explicit — an unknown event type is dead-lettered
    rather than silently accepted — and every handler can write exactly one kind of durable state
    (an analysis), because nothing they import can write another.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    handlers: dict[str, EventHandler] = {
        MAIL_EVENT_TYPE: mail_event_handler(config, clock, database, model=model),
        OBSERVATION_WEB_EVENT_TYPE: observation_event_handler(
            config, clock, database, model=model
        ),
        MANUAL_EVENT_TYPE: observation_event_handler(
            config, clock, database, model=model
        ),
    }
    return EventWorker(
        SqliteEventRepository(database, clock),
        InboundEventDispatcher(handlers),
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


def _planning_timezone_of(config: AssistantConfig | None) -> str | None:
    """The user's planning timezone, or `None` when the host has not chosen one."""
    return None if config is None or config.planning is None else config.planning.timezone


def conversation_repository(database: Database) -> SqliteConversationRepository:
    """Durable conversation threads, messages, turns and operation outcomes."""
    return SqliteConversationRepository(database)


def conversation_request_repository(database: Database) -> SqliteConversationRequestRepository:
    """The durable queue of accepted browser input (ADR-0041 §4-§10)."""
    return SqliteConversationRequestRepository(database)


def conversation_event_broker(*, queue_size: int | None = None) -> ConversationEventBroker:
    """The ephemeral, in-process notification channel the chat surface publishes to."""
    if queue_size is None:
        return ConversationEventBroker()
    return ConversationEventBroker(queue_size=queue_size)


def conversation_request_coordinator(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    conversation: ConversationService,
    broker: ConversationEventBroker,
) -> ConversationRequestCoordinator:
    """Serialize browser input per thread and feed it to the one conversation runtime."""
    del config
    return ConversationRequestCoordinator(
        requests=conversation_request_repository(database),
        conversation=conversation,
        threads=conversation_repository(database),
        broker=broker,
        clock=clock,
    )


def conversation_chat_service(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    model: ModelPort | None = None,
    executors: Mapping[ActionType, ActionExecutor] | None = None,
    broker: ConversationEventBroker | None = None,
) -> ConversationChatService:
    """The application surface the browser adapter speaks to.

    One composition, shared by the chat routes and by the daemon's queue service, so the durable
    queue, the notification channel and the runtime are the same objects in both places.

    Raises:
        ModelNotConfigured: no `[model]` section, so there is no conversation to serve.
        ModelCredentialsMissing: the environment holds no credential.
    """
    conversation = conversation_service(
        database, clock, config, model=model, executors=executors
    )
    channel = broker if broker is not None else conversation_event_broker()
    coordinator = conversation_request_coordinator(
        database, clock, config, conversation=conversation, broker=channel
    )
    return ConversationChatService(
        conversation=conversation,
        coordinator=coordinator,
        cards=ConversationCardService(
            conversation=conversation,
            reviews=conversation_external_review_service(
                database, clock, config, executors=executors
            ),
            conversations=conversation_repository(database),
            planning=planning_repository(database),
            commitments=commitment_repository(database),
            facts=conversational_fact_service(database, clock),
            clock=clock,
        ),
        conversations=conversation_repository(database),
        requests=conversation_request_repository(database),
        broker=channel,
        brief=today_brief_service(database, clock, config, refresh_attention=False),
        clock=clock,
    )


def conversation_context_builder(
    database: Database, clock: Clock, config: AssistantConfig | None
) -> ConversationContextBuilder:
    """The bounded conversation context: recent messages, recent entities, planning timezone."""
    return ConversationContextBuilder(
        conversation_repository(database),
        commitment_repository(database),
        planning_repository(database),
        scheduler_repository(database),
        clock,
        planning_timezone=_planning_timezone_of(config),
        capability_snapshot=build_capability_snapshot(config).to_payload(),
        mail=mail_repository(database),
        mail_intelligence=mail_intelligence_repository(database),
        mail_drafts=mail_draft_repository(database),
        recurring=recurring_calendar_repository(database),
        contacts=contact_repository(database),
        new_mail_drafts=new_mail_draft_repository(database),
        learning=learning_repository(database),
        attention=attention_repository(database),
    )


def conversation_review_repository(database: Database) -> SqliteConversationReviewRepository:
    """Durable reviews of conversational external actions (ADR-0034)."""
    return SqliteConversationReviewRepository(database)


def conversation_external_review_service(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    executors: Mapping[ActionType, ActionExecutor] | None = None,
) -> ConversationExternalReviewService:
    """The deterministic controller that settles one reviewed external action.

    It is built here, at the composition root, and handed to the conversation service as a whole
    object: the model-facing path never constructs it and never reaches into it (ADR-0034 §20).
    """
    return ConversationExternalReviewService(
        reviews=conversation_review_repository(database),
        actions=action_repository(database),
        approvals=approval_service(clock, database),
        execution=action_execution_service(config, clock, database, executors=executors),
        sends=mail_send_status_service(clock, database),
        clock=clock,
    )


def conversation_interpreter(
    config: AssistantConfig | None, *, model: ModelPort
) -> ConversationInterpreterService:
    """The typed conversation interpreter over an already-built adapter.

    Raises:
        ModelNotConfigured: no `[model]` section.
    """
    return ConversationInterpreterService(model, require_model_config(config))


def conversation_capabilities(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    model: ModelPort,
) -> ConversationCapabilityRegistry:
    """The frozen Phase 10A capability set, over the existing application services."""
    knowledge = grounded_context_builder(clock, database)
    snapshot = build_capability_snapshot(config)
    handlers = ConversationHandlers(
        tasks=task_service(database, clock, config),
        calendar=calendar_service(database, clock, config),
        work=work_service(database, clock, config),
        planner=planner_service(database, clock, config),
        scheduler=scheduler_repository(database),
        knowledge=grounded_answer_service(knowledge, config, model=model),
        commitments=commitment_repository(database),
        clock=clock,
        recurring=recurring_calendar_service(database, clock, config),
        contacts=contact_service(database, clock),
        recipients=recipient_resolver(database, config),
        new_mail_drafts=new_mail_draft_service(database, clock),
        facts=conversational_fact_service(database, clock),
        today=today_brief_service(database, clock, config),
        mail=mail_repository(database),
        mail_intelligence=mail_intelligence_repository(database),
        mail_sync=(
            mail_sync_service(config, clock, database)
            if config is not None and config.mail.accounts
            else None
        ),
        mail_drafts=mail_draft_writer(
            config, clock, database, model=model, knowledge=knowledge
        ),
        mail_sends=mail_send_action_service(config, clock, database),
        mail_send_status=mail_send_status_service(clock, database),
        mail_reconciliation=mail_send_reconciliation_service(config, clock, database),
        cases=case_service(clock, database),
        mail_drafts_repository=mail_draft_repository(database),
        capability_snapshot=snapshot,
        mail_accounts=() if config is None else config.mail.accounts,
        attention=attention_service(database, clock, config),
        planning_preferences=planning_preferences_service(database, clock, config),
    )
    return build_phase_10a_registry(handlers)


def conversation_service(
    database: Database,
    clock: Clock,
    config: AssistantConfig | None,
    *,
    model: ModelPort | None = None,
    executors: Mapping[ActionType, ActionExecutor] | None = None,
) -> ConversationService:
    """The Tree conversation runtime — one service, used by `rings` and by `pw chat`.

    The adapter is built once and shared by the interpreter and the grounded-answer handler, so a
    conversation holds one HTTP client rather than one per capability.

    Raises:
        ModelNotConfigured: no `[model]` section.
        ModelCredentialsMissing: the environment holds no credential.
    """
    adapter = model if model is not None else model_adapter(config)
    return ConversationService(
        repository=conversation_repository(database),
        interpreter=conversation_interpreter(config, model=adapter),
        capabilities=conversation_capabilities(database, clock, config, model=adapter),
        context_builder=conversation_context_builder(database, clock, config),
        clock=clock,
        planning_timezone=_planning_timezone_of(config),
        external=conversation_external_review_service(
            database, clock, config, executors=executors
        ),
        facts=conversational_fact_service(database, clock),
    )


__all__ = [
    "MODEL_API_KEY_ENV",
    "AppPaths",
    "VaultManifestFile",
    "action_execution_service",
    "action_repository",
    "action_service",
    "approval_service",
    "assistant_version",
    "backup_service",
    "calendar_service",
    "case_repository",
    "case_service",
    "catalog_repository",
    "catalog_service",
    "close_model",
    "commitment_repository",
    "config_loader",
    "contact_repository",
    "contact_service",
    "conversation_capabilities",
    "conversation_context_builder",
    "conversation_external_review_service",
    "conversation_interpreter",
    "conversation_repository",
    "conversation_review_repository",
    "conversation_service",
    "conversational_fact_service",
    "ehall_certificate_executor",
    "ehall_certificate_gateway",
    "ehall_certificate_service",
    "event_inbox",
    "grounded_answer_service",
    "grounded_context_builder",
    "integrity_service",
    "interpreter_service",
    "knowledge_indexer",
    "learning_repository",
    "learning_service",
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
    "manual_input_repository",
    "manual_input_service",
    "mcp_facade",
    "mobile_auth_service",
    "mobile_session_repository",
    "mobile_web_dependencies",
    "mobile_web_service",
    "model_adapter",
    "model_api_key",
    "new_mail_draft_repository",
    "new_mail_draft_service",
    "observation_analysis_repository",
    "observation_context_builder",
    "observation_event_handler",
    "planner_service",
    "planning_repository",
    "playbook_replay_registry",
    "playbook_repository",
    "playbook_service",
    "raw_mail_store",
    "recipient_resolver",
    "recurring_calendar_repository",
    "recurring_calendar_service",
    "registered_action_executors",
    "require_model_config",
    "rolling_replan_requester",
    "runtime_backup",
    "runtime_content_objects",
    "runtime_database",
    "scheduler_repository",
    "scheduler_service",
    "search_service",
    "smtp_mail_executor",
    "structured_model",
    "sync_service",
    "system_clock",
    "task_service",
    "today_brief_service",
    "web_snapshot_store",
    "web_source",
    "web_watch_repository",
    "web_watch_service",
    "work_repository",
    "work_service",
]
