"""`rings` and `pw chat`: one runtime, one conversation, a small set of session controls (§17-19).

Every test here drives the real CLI with scripted stdin against a real migrated database, with only
the provider faked. Nothing touches the network, and no answer is a `pw` command.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from assistant import bootstrap, cli_chat
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.conversation_input import InteractiveConversationInput
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


# ------------------------------------------------- the interactive editor at the CLI level (§10)


class _ScriptedSession:
    """A session double that hands back scripted lines, then behaves like Ctrl-D."""

    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)

    def prompt(self, message: str = "") -> str:
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _as_tty(monkeypatch: pytest.MonkeyPatch, reader: object) -> None:
    """Make the session read through one scripted editor instead of the real terminal."""
    monkeypatch.setattr(cli_chat, "build_input_source", lambda: reader)


def test_arrow_keys_edit_instead_of_reaching_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real transcript's `^[[D`/`^[[C` must be consumed by the editor, not submitted."""
    model = _install_model(monkeypatch, direct_reply("好。"))
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput(), history=InMemoryHistory())
        _as_tty(monkeypatch, InteractiveConversationInput(session=session))
        pipe.send_text("ics的地点是在仙一107" + "\x1b[D\x1b[C" + "\r")
        pipe.send_text("/exit\r")
        result = runner.invoke(app, ["chat"], input="", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "^[[" not in result.output
    payload = model.requests[-1].messages[-1].content
    assert "ics的地点是在仙一107" in payload
    assert "[D" not in payload and "[C" not in payload
    assert "\\u001b" not in payload


def test_ctrl_c_cancels_the_line_and_the_session_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(monkeypatch, direct_reply("我在。"))
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput(), history=InMemoryHistory())
        _as_tty(monkeypatch, InteractiveConversationInput(session=session))
        pipe.send_text("写了一半" + "\x03")
        pipe.send_text("还在吗\r")
        pipe.send_text("/exit\r")
        result = runner.invoke(app, ["chat"], input="", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "我在。" in result.output
    assert len(model.requests) == 1, "the cancelled line must not become a turn"
    assert _task_titles(tmp_path) == []


def test_ctrl_d_on_an_empty_prompt_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(monkeypatch)
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput(), history=InMemoryHistory())
        _as_tty(monkeypatch, InteractiveConversationInput(session=session))
        pipe.send_text("\x04")
        result = runner.invoke(app, ["chat"], input="", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert cli_chat.GOODBYE in result.output
    assert model.requests == []


def test_text_edited_into_a_control_character_never_becomes_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected line costs nothing: no turn, no model call, and the session stays open."""
    model = _install_model(monkeypatch, direct_reply("我在。"))
    _as_tty(
        monkeypatch,
        InteractiveConversationInput(session=_ScriptedSession("a\x00b", "在吗")),
    )

    result = runner.invoke(app, ["chat"], input="", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "没有执行任何操作" in result.output
    assert len(model.requests) == 1
    assert "在吗" in model.requests[0].messages[-1].content


def test_scripted_piped_input_still_uses_the_strict_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12/§23: non-TTY input keeps the Phase-10C path, including its fail-closed behaviour."""
    model = _install_model(monkeypatch, direct_reply("我在。"))
    source = cli_chat.build_input_source()

    assert not isinstance(source, InteractiveConversationInput)

    result = runner.invoke(app, ["chat"], input="在吗\n/exit\n", env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert model.requests
