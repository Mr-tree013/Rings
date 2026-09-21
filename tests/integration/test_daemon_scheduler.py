"""Daemon-level tests: the scheduler is composed, recovers from disk, and stays isolated.

The daemon is always started against temporary XDG directories. Startup itself is the crash
recovery path: a job that became due while the process was down is claimed as soon as the
scheduler's first loop runs.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from assistant import bootstrap
from assistant.daemon import load_config, serve
from assistant.daemon.app import build_services
from assistant.daemon.supervisor import BackoffPolicy, supervise
from assistant.domain.deadline import Deadline
from assistant.domain.notification import NotificationKind
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobKind,
    ScheduledJobStatus,
    canonical_payload_json,
)
from assistant.domain.scheduler_payloads import DeadlineReminderPayload
from assistant.domain.task import Task
from assistant.store.scheduler import SqliteSchedulerRepository

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[indexing]",
        "interval_seconds = 3600",
        "",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "",
        "[reminders]",
        "deadline_offsets_minutes = [120]",
        "",
        "[scheduler]",
        "poll_interval_seconds = 15",
        "replan_debounce_seconds = 60",
        "",
    )
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


async def _wait_for(predicate: object, *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before the timeout")


async def test_the_daemon_composes_index_sync_and_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = await load_config()

    services = build_services(config, clock, bootstrap.runtime_database(clock))

    assert [service.name for service in services] == ["index-sync", "scheduler", "attention"]


async def test_the_daemon_only_supervises_mail_when_accounts_are_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mail accounts means no mail service, and still no event worker."""
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = await load_config()
    database = bootstrap.runtime_database(clock)

    without_mail = build_services(config, clock, database)
    with_mail = build_services(
        _config_with_mail(config), clock, database
    )

    assert [service.name for service in without_mail] == ["index-sync", "scheduler", "attention"]
    assert [service.name for service in with_mail] == [
        "index-sync",
        "scheduler",
        "attention",
        "mail-sync",
    ]


def _config_with_mail(config: object) -> object:
    """The same host config, with one explicitly configured mail account."""
    from dataclasses import replace

    from assistant.domain.config import MailAccountConfig, MailConfig

    return replace(  # type: ignore[type-var]
        config,
        mail=MailConfig(
            accounts=(
                MailAccountConfig(
                    id="smail",
                    host="imap.example.edu",
                    username="student@example.edu",
                    mailbox="INBOX",
                ),
            )
        ),
    )


async def test_a_reminder_that_became_due_while_the_daemon_was_down_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup is crash recovery: scheduling intent lives in SQLite, not in a timer."""
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = await load_config()
    database = bootstrap.runtime_database(clock)
    now = clock.now()
    commitments = bootstrap.commitment_repository(database)
    task = Task(
        title="Write SE lab report",
        estimated_minutes=300,
        created_at=now,
        updated_at=now,
    )
    payload = DeadlineReminderPayload(
        task_id=task.id,
        deadline_id=uuid4(),
        deadline_due_at=now + timedelta(minutes=30),
        reminder_offset_minutes=120,
    )
    await commitments.add_task(
        task,
        deadline=Deadline(
            id=payload.deadline_id,
            task_id=task.id,
            due_at=payload.deadline_due_at,
            created_at=now,
            updated_at=now,
        ),
    )
    scheduler_repository = SqliteSchedulerRepository(database)
    await scheduler_repository.schedule_or_replace(
        ScheduledJob(
            kind=ScheduledJobKind.DEADLINE_REMINDER,
            due_at=now - timedelta(minutes=5),  # became due while nothing was running
            dedup_key=f"deadline-reminder:{payload.deadline_id}:missed",
            payload_json=canonical_payload_json(payload.to_payload()),
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        )
    )
    services = build_services(config, clock, database)
    stop_event = asyncio.Event()
    daemon = asyncio.create_task(serve(stop_event, services))

    async def delivered() -> bool:
        return len(await scheduler_repository.list_notifications(limit=None)) > 0

    try:
        await _wait_for(delivered)
    finally:
        stop_event.set()
        await asyncio.wait_for(daemon, timeout=10)

    notifications = await scheduler_repository.list_notifications(limit=None)
    assert [item.kind for item in notifications] == [NotificationKind.DEADLINE_REMINDER]
    assert notifications[0].title == "Deadline approaching: Write SE lab report"
    assert notifications[0].related_task_id == task.id
    jobs = await scheduler_repository.list_jobs(statuses=None, limit=None)
    reminder = next(job for job in jobs if job.kind is ScheduledJobKind.DEADLINE_REMINDER)
    assert reminder.status is ScheduledJobStatus.COMPLETED


async def test_a_scheduler_failure_never_stops_index_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling isolation: one supervisor's crash loop is not the other's problem."""
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = await load_config()
    database = bootstrap.runtime_database(clock)
    scheduler = bootstrap.scheduler_service(database, clock, config)
    index_sync = bootstrap.sync_service(config, clock, database)
    runs = {"sync": 0, "scheduler": 0}

    async def exploding_scheduler(stop_event: asyncio.Event) -> None:
        del stop_event
        runs["scheduler"] += 1
        raise RuntimeError("scheduler infrastructure exploded")

    original_sync = index_sync.run_forever

    async def counting_sync(stop_event: asyncio.Event) -> None:
        runs["sync"] += 1
        await original_sync(stop_event)

    index_sync.run_forever = counting_sync  # type: ignore[method-assign]
    scheduler.run_forever = exploding_scheduler  # type: ignore[method-assign]
    stop_event = asyncio.Event()
    policy = BackoffPolicy(base_delay_seconds=0.01, max_delay_seconds=0.05)

    async with asyncio.TaskGroup() as group:
        group.create_task(supervise(index_sync, stop_event, policy=policy))
        group.create_task(supervise(scheduler, stop_event, policy=policy))
        await _wait_for(lambda: _both_started(runs))
        stop_event.set()

    assert runs["sync"] == 1  # index-sync was never restarted or cancelled
    assert runs["scheduler"] > 1  # the failing service was restarted


async def _both_started(runs: dict[str, int]) -> bool:
    return runs["sync"] >= 1 and runs["scheduler"] >= 2
