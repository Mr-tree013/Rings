"""A real Tree conversation runtime on a real database, with a scripted model (ADR-0033).

Nothing here fakes the runtime: the harness builds the same `ConversationService` the CLI builds
(through `bootstrap.conversation_service`), against a real migrated SQLite database, with only the
provider replaced — `FakeModelAdapter` — so a test can say exactly what the model "answered"
without a network, a credential or a cent.

The JSON builders below mirror the schema in `conversation_schema.py`; a test that writes raw
dicts instead is testing what happens when the provider misbehaves, which is also supported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.conversation_service import ConversationService
from assistant.application.planner_service import PlannerService
from assistant.application.task_service import TaskService
from assistant.domain.config import AssistantConfig
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.planning import SqlitePlanningRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
"""Monday 08:00 Asia/Shanghai — the fixed instant every conversation test runs at."""

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
            CreateTask(
                title=title, due_at=due_at, estimated_minutes=estimated_minutes
            )
        )

    async def task_titles(self) -> list[str]:
        """Every task title in the database, ordered by creation."""
        entries = await self.commitments.list_tasks(statuses=None)
        return [task.title for task in sorted(entries, key=lambda item: item.created_at)]


async def build_harness(
    tmp_path: Path,
    *,
    config_body: str = CONFIG,
    start: datetime = NOW,
) -> ConversationHarness:
    """Build the real conversation runtime over a fresh migrated database."""
    clock = FakeClock(start=start)
    database = Database.at(tmp_path / "data" / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await bootstrap.config_loader(write_config(tmp_path, config_body)).load()
    model = FakeModelAdapter()
    service = bootstrap.conversation_service(database, clock, config, model=model)
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
    )


def utc(hours: int = 0, minutes: int = 0, days: int = 0) -> datetime:
    """A convenience instant relative to the harness's `NOW`."""
    return NOW + timedelta(days=days, hours=hours, minutes=minutes)


__all__ = [
    "CONFIG",
    "CONFIG_WITHOUT_TIMEZONE",
    "NOW",
    "ConversationHarness",
    "build_harness",
    "clarification",
    "direct_reply",
    "operation",
    "plan",
    "utc",
    "write_config",
]
