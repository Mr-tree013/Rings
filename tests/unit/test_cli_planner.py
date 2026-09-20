"""CLI tests for the deterministic planner workflow (ADR-0015).

The commands are structured input only: `pw plan week` proposes, `pw plan apply` changes
plan blocks, and nothing here ever asks an interactive question. Tests run against a
temporary XDG tree, so the real database is never touched.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from assistant.cli import app

runner = CliRunner()

NEXT_WEEK_MONDAY = "2026-09-21T09:00:00+08:00"
DEADLINE = "2026-09-25T23:59:00+08:00"
CONFIG = "\n".join(
    (
        "format_version = 1",
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
    )
)


def _env(tmp_path: Path, *, configure: bool = True) -> dict[str, str]:
    config_home = tmp_path / ("config" if configure else "config-without-planning")
    if configure:
        directory = config_home / "growing-assistant"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "COLUMNS": "200",
    }


def _add_task(tmp_path: Path, *extra: str) -> str:
    result = runner.invoke(
        app,
        ["task", "add", "Write SE lab report", "--estimate", "120", *extra],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"id: ([0-9a-f-]{36})", result.output)
    assert match is not None, result.output
    return match.group(1)


def _proposal_id(output: str) -> str:
    match = re.search(r"Proposal: ([0-9a-f-]{8})", output)
    assert match is not None, output
    return match.group(1)


def test_plan_week_proposes_without_touching_plan_blocks(
    tmp_path: Path, shanghai_local_timezone: None
) -> None:
    """The proposal window is rendered in the machine-local timezone (see `format_local`)."""
    _add_task(tmp_path, "--deadline", DEADLINE)

    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    calendar = runner.invoke(app, ["calendar", "--days", "60"], env=_env(tmp_path))

    assert proposed.exit_code == 0, proposed.output
    assert "status: pending" in proposed.output
    assert "timezone: Asia/Shanghai" in proposed.output
    assert "pw plan show" in proposed.output
    assert "pw plan apply" in proposed.output
    assert "2026-09-21T09:00+08:00" in proposed.output
    assert "nothing scheduled" in calendar.output  # proposing is not applying


def test_plan_week_without_configuration_reports_a_missing_timezone(tmp_path: Path) -> None:
    _add_task(tmp_path)

    result = runner.invoke(app, ["plan", "week"], env=_env(tmp_path, configure=False))

    assert result.exit_code == 1
    assert "no [planning] section" in result.output


def test_plan_show_renders_the_stored_proposal(tmp_path: Path) -> None:
    _add_task(tmp_path, "--deadline", DEADLINE)
    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)

    shown = runner.invoke(app, ["plan", "show", proposal_id], env=_env(tmp_path))

    assert shown.exit_code == 0, shown.output
    assert proposal_id in shown.output
    assert "input revision:" in shown.output
    assert "fingerprint:" in shown.output
    assert "Write SE lab report" in shown.output


def test_plan_proposals_lists_stored_proposals(tmp_path: Path) -> None:
    _add_task(tmp_path)
    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)

    listed = runner.invoke(app, ["plan", "proposals"], env=_env(tmp_path))

    assert listed.exit_code == 0, listed.output
    assert proposal_id in listed.output
    assert "pending" in listed.output
    assert "Asia/Shanghai" in listed.output
    assert "Blocks" in listed.output
    assert "Issues" in listed.output


def test_plan_apply_creates_planner_blocks_that_calendar_can_attribute(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path, "--deadline", DEADLINE)
    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)

    applied = runner.invoke(app, ["plan", "apply", proposal_id], env=_env(tmp_path))
    calendar = runner.invoke(app, ["calendar", "--days", "60"], env=_env(tmp_path))
    shown = runner.invoke(app, ["task", "show", task_id], env=_env(tmp_path))

    assert applied.exit_code == 0, applied.output
    assert "Applied proposal" in applied.output
    assert "Created: 1 planner blocks" in applied.output
    assert "Replaced: 0 planner blocks" in applied.output
    assert "planner" in calendar.output
    assert proposal_id in calendar.output
    assert "plan_block" in calendar.output
    assert shown.exit_code == 0, shown.output


def test_manual_plan_blocks_are_shown_as_manual_and_never_replaced(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    manual = runner.invoke(
        app,
        [
            "plan",
            "add",
            task_id,
            "--start",
            NEXT_WEEK_MONDAY,
            "--end",
            "2026-09-21T11:00:00+08:00",
        ],
        env=_env(tmp_path),
    )
    assert manual.exit_code == 0, manual.output

    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)
    applied = runner.invoke(app, ["plan", "apply", proposal_id], env=_env(tmp_path))
    calendar = runner.invoke(app, ["calendar", "--days", "60"], env=_env(tmp_path))

    assert applied.exit_code == 0, applied.output
    assert "manual" in calendar.output
    assert "planner" in calendar.output
    assert calendar.output.count("plan_block") >= 2


def test_applying_a_stale_proposal_fails_with_guidance(tmp_path: Path) -> None:
    task_id = _add_task(tmp_path)
    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)
    runner.invoke(
        app, ["task", "deadline", task_id, DEADLINE], env=_env(tmp_path)
    )

    applied = runner.invoke(app, ["plan", "apply", proposal_id], env=_env(tmp_path))
    calendar = runner.invoke(app, ["calendar", "--days", "60"], env=_env(tmp_path))
    shown = runner.invoke(app, ["plan", "show", proposal_id], env=_env(tmp_path))

    assert applied.exit_code == 1
    assert "stale" in applied.output
    assert "pw plan week" in applied.output
    assert "nothing scheduled" in calendar.output
    assert "status: stale" in shown.output


def test_applying_a_proposal_twice_is_refused(tmp_path: Path) -> None:
    _add_task(tmp_path)
    proposed = runner.invoke(app, ["plan", "week", "--next"], env=_env(tmp_path))
    proposal_id = _proposal_id(proposed.output)
    first = runner.invoke(app, ["plan", "apply", proposal_id], env=_env(tmp_path))
    second = runner.invoke(app, ["plan", "apply", proposal_id], env=_env(tmp_path))

    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "not pending" in second.output


def test_unknown_proposal_fails_cleanly(tmp_path: Path) -> None:
    _add_task(tmp_path)

    result = runner.invoke(app, ["plan", "show", "ffffffff"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_planner_reads_host_config_but_task_commands_do_not(tmp_path: Path) -> None:
    _add_task(tmp_path)
    directory = tmp_path / "broken-config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text("not = a = valid = toml\n", encoding="utf-8")
    broken = dict(_env(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "broken-config"))

    listing = runner.invoke(app, ["tasks"], env=broken)
    planning = runner.invoke(app, ["plan", "week", "--next"], env=broken)

    assert listing.exit_code == 0, listing.output  # a broken config cannot break `pw tasks`
    assert planning.exit_code == 2
    assert "invalid configuration" in planning.output
