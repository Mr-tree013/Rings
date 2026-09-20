"""Composition helpers shared by the CLI and the daemon (ADR-0013).

This is a composition root: it may import concrete adapters and store implementations, and
its only job is to turn configuration into wired services. Application and domain layers stay
free of these imports — `tests/unit/test_architecture.py` enforces that.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from assistant.adapters.config.toml_config import TomlConfigLoader
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.adapters.system_clock import SystemClock
from assistant.application.calendar_service import CalendarService
from assistant.application.greedy_planner import GreedyPlanner
from assistant.application.index_sync import IndexSyncService
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.paths import AppPaths
from assistant.application.planner_service import PlannerService
from assistant.application.retry import RetryPolicy
from assistant.application.rolling_replan import RollingReplanRequester
from assistant.application.scheduler_service import SchedulerService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.application.task_service import TaskService
from assistant.application.work_service import WorkService
from assistant.domain.config import AssistantConfig, SchedulerConfig
from assistant.ports.clock import Clock
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
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


__all__ = [
    "AppPaths",
    "VaultManifestFile",
    "calendar_service",
    "catalog_repository",
    "catalog_service",
    "commitment_repository",
    "config_loader",
    "knowledge_indexer",
    "planner_service",
    "planning_repository",
    "rolling_replan_requester",
    "runtime_database",
    "scheduler_repository",
    "scheduler_service",
    "search_service",
    "sync_service",
    "system_clock",
    "task_service",
    "work_repository",
    "work_service",
]
