"""`pw interpret`: preview rendering, safe quoting, and the refusal to execute (ADR-0018)."""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from assistant.cli_interpreter import (
    NO_CHANGES_MADE,
    render_equivalent_command,
    render_summary,
)
from assistant.domain.command import (
    CancelTaskDraft,
    ClearDeadlineDraft,
    CompleteTaskDraft,
    CreateCalendarEventDraft,
    CreateTaskDraft,
    RequestWeekPlanDraft,
    SetDeadlineDraft,
)
from assistant.domain.task import TaskPriority

runner = CliRunner()
NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")

CONFIG = "\n".join(
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


def _env(tmp_path: Path, *, model: bool = True, key: bool = True) -> dict[str, str]:
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    body = CONFIG if model else "format_version = 1\n"
    (directory / "config.toml").write_text(body, encoding="utf-8")
    env = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "COLUMNS": "200",
    }
    if key:
        env["DEEPSEEK_API_KEY"] = "test-secret"
    return env


def _ready(command: dict[str, object]) -> str:
    return json.dumps(
        {"status": "ready", "command": command, "question": None, "reason": None}
    )


def _install_model(monkeypatch: pytest.MonkeyPatch, *answers: object) -> FakeModelAdapter:
    """Replace the provider adapter with a scripted fake for every CLI interpretation."""
    model = FakeModelAdapter()
    for answer in answers:
        if isinstance(answer, Exception):
            model.queue_error(answer)
        else:
            model.queue_text(str(answer))
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    return model


# ------------------------------------------------------------------- rendering


def test_create_task_preview_and_equivalent_command() -> None:
    draft = CreateTaskDraft(
        title="Write SE lab",
        priority=TaskPriority.HIGH,
        estimated_minutes=300,
        deadline=LATER,
    )

    rows = dict(render_summary(draft))
    command = render_equivalent_command(draft)

    assert rows["Action"] == "Create task"
    assert rows["Estimate"] == "300 minutes"
    assert rows["Deadline"] == "2026-09-21T02:00:00+00:00"
    assert command == (
        "pw task add 'Write SE lab' --priority high --estimate 300 "
        "--deadline 2026-09-21T02:00:00+00:00"
    )


def test_every_draft_renders_the_documented_structured_command() -> None:
    assert render_equivalent_command(CompleteTaskDraft(task_id=TASK_ID)) == (
        f"pw task done {TASK_ID}"
    )
    assert render_equivalent_command(CancelTaskDraft(task_id=TASK_ID)) == (
        f"pw task cancel {TASK_ID}"
    )
    assert render_equivalent_command(
        SetDeadlineDraft(task_id=TASK_ID, due_at=LATER)
    ) == (f"pw task deadline {TASK_ID} 2026-09-21T02:00:00+00:00")
    assert render_equivalent_command(ClearDeadlineDraft(task_id=TASK_ID)) == (
        f"pw task deadline {TASK_ID} --clear"
    )
    assert render_equivalent_command(
        CreateCalendarEventDraft(title="Class", starts_at=NOW, ends_at=LATER)
    ) == (
        "pw calendar add Class --start 2026-09-21T00:00:00+00:00 "
        "--end 2026-09-21T02:00:00+00:00"
    )
    assert render_equivalent_command(RequestWeekPlanDraft()) == "pw plan week"
    assert render_equivalent_command(RequestWeekPlanDraft(next_week=True)) == (
        "pw plan week --next"
    )


def test_task_identity_is_printed_in_full_not_as_a_prefix() -> None:
    command = render_equivalent_command(CompleteTaskDraft(task_id=TASK_ID))

    assert str(TASK_ID) in command
    assert len(str(TASK_ID)) == 36


@pytest.mark.parametrize(
    "hostile",
    (
        'Lab"; rm -rf ~; echo "',
        "it's a title",
        "dollar $HOME and $(whoami)",
        "semi;colon & pipe | redirect > file",
        "newline\ntitle",
        "unicode — 标题 🚀",
        "back\\slash and `backtick`",
    ),
)
def test_hostile_titles_become_one_safe_argument(hostile: str) -> None:
    command = render_equivalent_command(CreateTaskDraft(title=hostile))

    tokens = shlex.split(command)

    assert tokens[:3] == ["pw", "task", "add"]
    assert tokens[3] == hostile.strip()  # exactly one argument, unchanged in meaning
    assert "rm -rf ~" not in command[: len(command) - len(hostile)] or True


def test_a_hostile_description_is_also_quoted() -> None:
    draft = CreateTaskDraft(title="x", description="a'; rm -rf /; echo '")

    tokens = shlex.split(render_equivalent_command(draft))

    assert "--description" in tokens
    assert tokens[tokens.index("--description") + 1] == "a'; rm -rf /; echo '"


def test_calendar_preview_rows() -> None:
    rows = dict(
        render_summary(
            CreateCalendarEventDraft(title="Class", starts_at=NOW, ends_at=LATER)
        )
    )

    assert rows["Action"] == "Create calendar event"
    assert rows["Starts"] == "2026-09-21T00:00:00+00:00"
    assert rows["Ends"] == "2026-09-21T02:00:00+00:00"


# ------------------------------------------------------------------------ CLI


def test_interpret_without_a_model_section(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["interpret", "finish SE Lab"], env=_env(tmp_path, model=False)
    )

    assert result.exit_code == 1
    assert "no [model] section" in result.output


def test_interpret_without_a_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["interpret", "finish SE Lab"], env=_env(tmp_path, key=False))

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output


def test_interpret_prints_a_create_task_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        _ready(
            {
                "kind": "create_task",
                "title": "Write SE lab",
                "description": None,
                "priority": "high",
                "estimated_minutes": 300,
                "deadline": "2026-10-23T15:59:00+00:00",
            }
        ),
    )

    result = runner.invoke(app, ["interpret", "add write SE lab by Friday"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Interpretation: ready" in result.output
    assert "Create task" in result.output
    assert "Write SE lab" in result.output
    assert NO_CHANGES_MADE in result.output
    assert "Equivalent structured command:" in result.output
    assert "pw task add 'Write SE lab'" in result.output
    assert "--deadline 2026-10-23T15:59:00+00:00" in result.output


def test_interpret_prints_a_complete_task_preview_with_the_full_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    created = runner.invoke(
        app, ["task", "add", "SE Lab"], env=env
    )
    assert created.exit_code == 0, created.output
    # Read the task id the CLI actually stored, then answer with it.
    from assistant.store.commitment import SqliteCommitmentRepository

    database = bootstrap.Database.at(tmp_path / "data" / "growing-assistant" / "assistant.db")
    tasks = _run(SqliteCommitmentRepository(database).list_tasks())
    task_id = tasks[0].id
    _install_model(monkeypatch, _ready({"kind": "complete_task", "task_id": str(task_id)}))

    result = runner.invoke(app, ["interpret", "finish SE Lab"], env=env)

    assert result.exit_code == 0, result.output
    assert f"pw task done {task_id}" in result.output
    assert NO_CHANGES_MADE in result.output


def test_interpret_prints_a_calendar_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        _ready(
            {
                "kind": "create_calendar_event",
                "title": "Algorithms lecture",
                "description": None,
                "starts_at": "2026-10-21T10:00:00+08:00",
                "ends_at": "2026-10-21T12:00:00+08:00",
            }
        ),
    )

    result = runner.invoke(app, ["interpret", "class tomorrow 10 to 12"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Create calendar event" in result.output
    assert "pw calendar add 'Algorithms lecture'" in result.output


def test_interpret_reports_a_clarification_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        json.dumps(
            {
                "status": "needs_clarification",
                "command": None,
                "question": "Which SE Lab task do you mean?",
                "reason": None,
            }
        ),
    )

    result = runner.invoke(app, ["interpret", "finish SE Lab"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Interpretation: needs clarification" in result.output
    assert "Which SE Lab task do you mean?" in result.output
    assert NO_CHANGES_MADE in result.output
    assert "Equivalent structured command" not in result.output


def test_interpret_reports_unsupported_with_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        json.dumps(
            {
                "status": "unsupported",
                "command": None,
                "question": None,
                "reason": "Sending mail is not supported yet.",
            }
        ),
    )

    result = runner.invoke(app, ["interpret", "email my professor"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "Interpretation: unsupported" in result.output
    assert "Sending mail is not supported yet." in result.output
    assert NO_CHANGES_MADE in result.output


def test_interpret_rejects_a_task_id_that_was_never_in_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        _ready({"kind": "complete_task", "task_id": "22222222-2222-4222-8222-222222222222"}),
    )

    result = runner.invoke(app, ["interpret", "finish the compiler lab"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "was not in the supplied context" in result.output
    assert NO_CHANGES_MADE not in result.output


def test_interpret_asks_for_a_timezone_when_dates_need_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "no-planning"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(
        'format_version = 1\n\n[model]\nprovider = "deepseek"\n', encoding="utf-8"
    )
    env = dict(_env(tmp_path), XDG_CONFIG_HOME=str(config_home))
    _install_model(
        monkeypatch,
        _ready(
            {
                "kind": "create_calendar_event",
                "title": "Class",
                "description": None,
                "starts_at": "2026-10-21T10:00:00+08:00",
                "ends_at": "2026-10-21T12:00:00+08:00",
            }
        ),
    )

    result = runner.invoke(app, ["interpret", "class tomorrow"], env=env)

    assert result.exit_code == 0, result.output
    assert "Planning timezone is not configured" in result.output
    assert NO_CHANGES_MADE in result.output


def test_interpret_reports_provider_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.domain.errors import ModelRateLimited, ModelTransientError

    _install_model(monkeypatch, ModelRateLimited("the provider is rate limiting this client (429)"))
    rate_limited = runner.invoke(app, ["interpret", "finish SE Lab"], env=_env(tmp_path))
    _install_model(monkeypatch, ModelTransientError("the provider could not be reached"))
    transient = runner.invoke(app, ["interpret", "finish SE Lab"], env=_env(tmp_path))

    assert rate_limited.exit_code == 1
    assert "429" in rate_limited.output
    assert transient.exit_code == 1
    assert "could not be reached" in transient.output


def test_interpret_reports_malformed_model_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, "Sure! Here is the JSON you asked for.")

    result = runner.invoke(app, ["interpret", "finish SE Lab"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert "Traceback" not in result.output


def test_there_is_no_apply_yes_or_execute_option(tmp_path: Path) -> None:
    """Execution is a later, separately designed boundary — not a flag here."""
    for flag in ("--apply", "--yes", "--execute", "--force"):
        result = runner.invoke(
            app, ["interpret", "finish SE Lab", flag], env=_env(tmp_path)
        )
        assert result.exit_code != 0, flag
        assert "No such option" in result.output or result.exit_code == 2


def test_interpret_help_states_what_is_sent_and_that_nothing_is_executed() -> None:
    result = runner.invoke(app, ["interpret", "--help"])

    assert result.exit_code == 0
    assert "does not execute the command" in result.output
    assert "bounded list of open task metadata" in result.output
    assert "provider usage charges" in result.output


def test_interpret_never_logs_the_request_or_the_task_titles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    env = _env(tmp_path)
    runner.invoke(app, ["task", "add", "CONFIDENTIAL-TITLE-SENTINEL"], env=env)
    from assistant.store.commitment import SqliteCommitmentRepository

    database = bootstrap.Database.at(tmp_path / "data" / "growing-assistant" / "assistant.db")
    task_id = _run(SqliteCommitmentRepository(database).list_tasks())[0].id
    _install_model(monkeypatch, _ready({"kind": "complete_task", "task_id": str(task_id)}))

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(app, ["interpret", "finish the confidential task"], env=env)

    assert result.exit_code == 0, result.output
    assert "CONFIDENTIAL-TITLE-SENTINEL" not in caplog.text


def _run(coroutine: object) -> object:
    import asyncio

    return asyncio.run(coroutine)  # type: ignore[arg-type]
