"""CLI tests for the structured commitment commands (ADR-0014)."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from assistant.cli import app

runner = CliRunner()

MONDAY = "2026-09-21T10:00:00+08:00"
WEDNESDAY = "2026-09-23T19:00:00+08:00"
WEDNESDAY_END = "2026-09-23T21:00:00+08:00"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        # Keep table rendering deterministic so assertions never depend on the tty size.
        "COLUMNS": "200",
    }


def _add_task(tmp_path: Path, *extra: str) -> str:
    result = runner.invoke(
        app,
        ["task", "add", "Write SE lab report", "--estimate", "300", *extra],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"id: ([0-9a-f-]{36})", result.output)
    assert match is not None, result.output
    return match.group(1)


def test_task_add_list_and_show(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path, "--priority", "high", "--deadline", "2026-10-20T23:59:00+08:00")

    listing = runner.invoke(app, ["tasks"], env=_env(tmp_path))
    shown = runner.invoke(app, ["task", "show", task_id[:8]], env=_env(tmp_path))

    assert listing.exit_code == 0, listing.output
    assert task_id[:8] in listing.output
    assert "high" in listing.output
    assert shown.exit_code == 0, shown.output
    assert "Write SE lab report" in shown.output
    assert "300m" in shown.output
    assert "open" in shown.output


def test_task_add_rejects_a_naive_deadline(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["task", "add", "Bad task", "--deadline", "2026-10-20 23:59"],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "timezone offset" in result.output


def test_task_add_rejects_an_invalid_priority_and_estimate(tmp_path: Path) -> None:
    priority = runner.invoke(
        app, ["task", "add", "Bad", "--priority", "urgent"], env=_env(tmp_path)
    )
    estimate = runner.invoke(
        app, ["task", "add", "Bad", "--estimate", "0"], env=_env(tmp_path)
    )

    assert priority.exit_code == 1
    assert "priority must be one of" in priority.output
    assert estimate.exit_code == 1
    assert "estimated_minutes" in estimate.output


def test_unknown_task_is_reported_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["task", "show", "ffffffff"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_deadline_can_be_set_updated_and_cleared(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)

    first = runner.invoke(
        app, ["task", "deadline", task_id, MONDAY], env=_env(tmp_path)
    )
    updated = runner.invoke(
        app, ["task", "deadline", task_id, WEDNESDAY], env=_env(tmp_path)
    )
    shown = runner.invoke(app, ["task", "show", task_id], env=_env(tmp_path))
    cleared = runner.invoke(
        app, ["task", "deadline", task_id, "--clear"], env=_env(tmp_path)
    )

    assert first.exit_code == 0, first.output
    assert updated.exit_code == 0, updated.output
    assert "2026-09-23T19:00" in shown.output
    assert cleared.exit_code == 0, cleared.output
    assert "deadline cleared" in cleared.output


def test_deadline_requires_a_time_or_clear_flag(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)

    result = runner.invoke(app, ["task", "deadline", task_id], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "--clear" in result.output


def test_completion_cancels_future_plan_blocks(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    assert (
        runner.invoke(
            app,
            ["plan", "add", task_id, "--start", WEDNESDAY, "--end", WEDNESDAY_END],
            env=_env(tmp_path),
        ).exit_code
        == 0
    )

    done = runner.invoke(app, ["task", "done", task_id], env=_env(tmp_path))
    shown = runner.invoke(app, ["task", "show", task_id], env=_env(tmp_path))
    listing = runner.invoke(app, ["tasks"], env=_env(tmp_path))
    with_all = runner.invoke(app, ["tasks", "--all"], env=_env(tmp_path))

    assert done.exit_code == 0, done.output
    assert "cancelled plan blocks: 1" in done.output
    assert "cancelled" in shown.output
    assert "no tasks" in listing.output  # terminal tasks are hidden by default
    assert "completed" in with_all.output


def test_terminal_tasks_cannot_be_planned(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    runner.invoke(app, ["task", "done", task_id], env=_env(tmp_path))

    result = runner.invoke(
        app,
        ["plan", "add", task_id, "--start", WEDNESDAY, "--end", WEDNESDAY_END],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "terminal tasks cannot be" in result.output  # wrapped by the rich console


def test_plan_and_work_commands(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    planned = runner.invoke(
        app,
        ["plan", "add", task_id, "--start", WEDNESDAY, "--end", WEDNESDAY_END],
        env=_env(tmp_path),
    )
    block_id = re.search(r"planned ([0-9a-f]{8})", planned.output)
    assert block_id is not None, planned.output
    logged = runner.invoke(
        app,
        ["work", "add", task_id, "--start", MONDAY, "--end", "2026-09-21T11:00:00+08:00"],
        env=_env(tmp_path),
    )
    listed = runner.invoke(app, ["work", "list", task_id], env=_env(tmp_path))
    cancelled = runner.invoke(
        app, ["plan", "cancel", block_id.group(1)], env=_env(tmp_path)
    )
    after = runner.invoke(app, ["calendar", "--days", "30"], env=_env(tmp_path))

    assert planned.exit_code == 0, planned.output
    assert logged.exit_code == 0, logged.output
    assert "3600s" in logged.output
    assert "total actual work: 60m00s (3600s)" in listed.output
    assert cancelled.exit_code == 0, cancelled.output
    assert "nothing scheduled" in after.output


def test_calendar_shows_events_and_plans_with_their_kind(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    event = runner.invoke(
        app,
        ["calendar", "add", "SE lecture", "--start", MONDAY, "--end", "2026-09-21T12:00:00+08:00"],
        env=_env(tmp_path),
    )
    runner.invoke(
        app,
        ["plan", "add", task_id, "--start", WEDNESDAY, "--end", WEDNESDAY_END],
        env=_env(tmp_path),
    )

    listing = runner.invoke(app, ["calendar", "--days", "30"], env=_env(tmp_path))

    assert event.exit_code == 0, event.output
    assert "calendar_event" in listing.output
    assert "plan_block" in listing.output
    assert "SE lecture" in listing.output


def test_calendar_add_rejects_a_naive_timestamp(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["calendar", "add", "Bad", "--start", "2026-09-21 10:00", "--end", MONDAY],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    assert "timezone offset" in result.output


def test_task_edit_changes_estimate_and_priority(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)

    edited = runner.invoke(
        app,
        ["task", "edit", task_id, "--estimate", "240", "--priority", "high"],
        env=_env(tmp_path),
    )
    shown = runner.invoke(app, ["task", "show", task_id], env=_env(tmp_path))

    assert edited.exit_code == 0, edited.output
    assert "estimate: 240m" in edited.output
    assert "priority: high" in edited.output
    assert "240m" in shown.output
    assert "high" in shown.output


def test_task_edit_can_clear_the_estimate(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)

    cleared = runner.invoke(
        app, ["task", "edit", task_id, "--clear-estimate"], env=_env(tmp_path)
    )
    shown = runner.invoke(app, ["task", "show", task_id], env=_env(tmp_path))

    assert cleared.exit_code == 0, cleared.output
    assert "estimate: -" in cleared.output
    assert "Estimate    │ -" in shown.output


def test_task_edit_requires_exactly_one_kind_of_change(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)

    conflicting = runner.invoke(
        app,
        ["task", "edit", task_id, "--estimate", "60", "--clear-estimate"],
        env=_env(tmp_path),
    )
    empty = runner.invoke(app, ["task", "edit", task_id], env=_env(tmp_path))

    assert conflicting.exit_code == 1
    assert "either --estimate or --clear-estimate" in conflicting.output
    assert empty.exit_code == 1
    assert "nothing to edit" in empty.output


def test_task_edit_rejects_terminal_tasks_and_unknown_ids(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    runner.invoke(app, ["task", "done", task_id], env=_env(tmp_path))

    terminal = runner.invoke(
        app, ["task", "edit", task_id, "--estimate", "60"], env=_env(tmp_path)
    )
    unknown = runner.invoke(
        app, ["task", "edit", "ffffffff", "--estimate", "60"], env=_env(tmp_path)
    )

    assert terminal.exit_code == 1
    assert "completed" in terminal.output
    assert unknown.exit_code == 1
    assert "does not exist" in unknown.output
