"""CLI tests for the notification inbox and scheduled-job visibility (ADR-0016).

Reminders only reach the user through this inbox, so the commands are checked as the user
sees them: unread by default, nothing but reads mutates anything.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from assistant.cli import app
from assistant.domain.notification import Notification, NotificationKind
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock

runner = CliRunner()
NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
CONFIG = "\n".join(
    (
        "format_version = 1",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "",
        "[reminders]",
        "deadline_offsets_minutes = [1440, 120]",
        "",
        "[scheduler]",
        "poll_interval_seconds = 15",
        "replan_debounce_seconds = 60",
        "",
    )
)


def _env(tmp_path: Path) -> dict[str, str]:
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "COLUMNS": "200",
    }


def _seed_notifications(tmp_path: Path, *titles: str) -> list[str]:
    """Write notifications straight into the runtime database the CLI will read."""
    database = Database.at(tmp_path / "data" / "growing-assistant" / "assistant.db")
    apply_migrations(database, clock=FakeClock(start=NOW))
    scheduler = SqliteSchedulerRepository(database)
    ids: list[str] = []
    for index, title in enumerate(titles):
        notification = Notification(
            kind=NotificationKind.DEADLINE_REMINDER,
            title=title,
            body=f"Due: 2026-09-22T0{index}:00:00+08:00",
            dedup_key=f"notification:deadline:{uuid4()}",
            created_at=NOW + timedelta(minutes=index),
        )
        stored = _run(scheduler.create_notification_idempotent(notification))
        ids.append(str(stored.id))
    return ids


def _run(coroutine: object) -> object:
    import asyncio

    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_notifications_lists_unread_by_default(tmp_path: Path) -> None:
    ids = _seed_notifications(tmp_path, "first reminder", "second reminder")

    listed = runner.invoke(app, ["notifications"], env=_env(tmp_path))

    assert listed.exit_code == 0, listed.output
    assert "first reminder" in listed.output
    assert "second reminder" in listed.output
    assert "unread" in listed.output
    assert ids[0][:8] in listed.output


def test_notification_show_and_read_round_trip(tmp_path: Path) -> None:
    ids = _seed_notifications(tmp_path, "read me")
    notification_id = ids[0]

    shown = runner.invoke(app, ["notification", "show", notification_id[:8]], env=_env(tmp_path))
    read = runner.invoke(app, ["notification", "read", notification_id[:8]], env=_env(tmp_path))
    again = runner.invoke(app, ["notification", "read", notification_id], env=_env(tmp_path))
    unread = runner.invoke(app, ["notifications"], env=_env(tmp_path))
    everything = runner.invoke(app, ["notifications", "--all"], env=_env(tmp_path))

    assert shown.exit_code == 0, shown.output
    assert "read me" in shown.output
    assert "Due: 2026-09-22T00:00:00+08:00" in shown.output
    assert "unread" in shown.output
    assert read.exit_code == 0, read.output
    assert again.exit_code == 0, again.output  # reading twice is idempotent
    assert "no notifications" in unread.output
    assert "read me" in everything.output


def test_notification_commands_report_unknown_ids(tmp_path: Path) -> None:
    _seed_notifications(tmp_path, "present")

    shown = runner.invoke(app, ["notification", "show", "ffffffff"], env=_env(tmp_path))
    read = runner.invoke(app, ["notification", "read", "ffffffff"], env=_env(tmp_path))

    assert shown.exit_code == 1
    assert read.exit_code == 1
    assert "Traceback" not in shown.output


def test_scheduled_lists_active_jobs_and_can_include_history(tmp_path: Path) -> None:
    _seed_notifications(tmp_path, "unrelated")
    created = runner.invoke(
        app,
        ["task", "add", "Write SE lab report", "--deadline", "2026-09-25T23:59:00+08:00"],
        env=_env(tmp_path),
    )
    assert created.exit_code == 0, created.output

    active = runner.invoke(app, ["scheduled"], env=_env(tmp_path))
    everything = runner.invoke(app, ["scheduled", "--all"], env=_env(tmp_path))

    assert active.exit_code == 0, active.output
    assert "deadline_reminder" in active.output
    assert "rolling_replan" in active.output
    assert "pending" in active.output
    assert "Attempts" in active.output
    assert everything.exit_code == 0, everything.output
    assert re.search(r"\b[0-9a-f]{8}\b", active.output) is not None


def test_scheduled_is_empty_without_commitments(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scheduled"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "no scheduled jobs" in result.output


def test_status_and_doctor_describe_the_scheduler_without_running_it(tmp_path: Path) -> None:
    _seed_notifications(tmp_path, "present")

    status = runner.invoke(app, ["status"], env=_env(tmp_path))
    doctor = runner.invoke(app, ["doctor"], env=_env(tmp_path))

    assert status.exit_code == 0, status.output
    assert "scheduler" in status.output
    assert "notification inbox" in status.output
    assert doctor.exit_code == 0, doctor.output
    assert "due jobs + notification inbox" in doctor.output
    assert "1440, 120" in doctor.output
    assert "15s" in doctor.output
