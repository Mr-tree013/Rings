"""`rings` and `pw chat`: one runtime, one conversation, a small set of session controls (§17-19).

Every test here drives the real CLI with scripted stdin against a real migrated database, with only
the provider faked. Nothing touches the network, and no answer is a `pw` command.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap, cli_chat
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from tests.support.conversation import CONFIG, direct_reply, operation, plan

runner = CliRunner()


def _env(tmp_path: Path) -> dict[str, str]:
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "DEEPSEEK_API_KEY": "test-secret",
        "COLUMNS": "200",
    }


def _install_model(monkeypatch: pytest.MonkeyPatch, *answers: str) -> FakeModelAdapter:
    model = FakeModelAdapter()
    model.queue(*answers) if hasattr(model, "queue") else None
    for answer in answers:
        model.queue_text(answer)
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    return model


def _task_titles(tmp_path: Path) -> list[str]:
    database = tmp_path / "data" / "growing-assistant" / "assistant.db"
    connection = sqlite3.connect(str(database))
    try:
        return [
            row[0]
            for row in connection.execute("SELECT title FROM tasks ORDER BY rowid")
        ]
    finally:
        connection.close()


def test_pw_chat_runs_a_conversation_and_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(
        monkeypatch,
        plan(
            operation(
                "task.create",
                {
                    "title": "写报告",
                    "description": None,
                    "priority": "normal",
                    "estimated_minutes": None,
                    "due_at": None,
                },
            )
        ),
    )

    result = runner.invoke(
        app, ["chat"], input="加个任务，写报告\n/exit\n", env=_env(tmp_path)
    )

    assert result.exit_code == 0, result.output
    assert "你好，我是 Tree" in result.output
    assert "已创建任务" in result.output
    assert _task_titles(tmp_path) == ["写报告"]
    # The runtime performs the work; it never sends the user back to the CLI (§39).
    assert "pw task add" not in result.output


def test_a_second_session_resumes_the_active_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, direct_reply("记下了。"))
    first = runner.invoke(app, ["chat"], input="在吗\n/exit\n", env=_env(tmp_path))
    assert first.exit_code == 0, first.output

    _install_model(monkeypatch, direct_reply("还在。"))
    second = runner.invoke(app, ["chat"], input="还在吗\n/exit\n", env=_env(tmp_path))

    assert second.exit_code == 0, second.output
    assert "已继续上次对话" in second.output


def test_end_of_input_is_a_clean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model(monkeypatch)

    result = runner.invoke(app, ["chat"], input="", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert cli_chat.GOODBYE in result.output


def test_help_shows_natural_language_examples_not_cli_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch)

    result = runner.invoke(app, ["chat"], input="/help\n/exit\n", env=_env(tmp_path))

    assert "明天下午三点提醒我交软件工程报告" in result.output
    assert "/new" in result.output
    # The 100+ domain commands are deliberately absent from the conversation's help.
    assert "pw task" not in result.output
    assert "pw plan" not in result.output


def test_new_threads_and_use_are_session_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch)

    result = runner.invoke(
        app,
        ["chat"],
        input="/threads\n/new\n/threads\n/use nope\n/exit\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "已继续上次对话" not in result.output
    assert "没有切换" in result.output


def test_rings_and_pw_chat_use_the_same_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []

    def spy(*, input_fn=None, announce=None) -> int:  # test double
        calls.append(announce)
        return 0

    monkeypatch.setattr(cli_chat, "run_conversation", spy)

    with pytest.raises(SystemExit) as exit_info:
        cli_chat.main()
    result = runner.invoke(app, ["chat"])

    assert exit_info.value.code == 0
    assert result.exit_code == 0
    assert len(calls) == 2
    assert any(entry is not None for entry in calls)
