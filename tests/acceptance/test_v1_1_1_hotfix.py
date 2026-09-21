"""The v1.1.1 acceptance transcript: the real one, replayed (ADR-0040).

Two production defects are pinned here together, because they were one user's afternoon:

1. the terminal, driven by real key events (`^[[D`, `^[[C`, Backspace), must produce the text the
   user meant — not `^[[D` in the conversation and not a broken Chinese character;
2. revising a pending three-class proposal must leave exactly one confirmable proposal behind, so
   the eventual `可以` creates the three revised rules and not the obsolete one as well.

The whole flow runs through the real `pw chat` entry point, the real runtime, a real migrated
database and the real line editor; only the provider is scripted.
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
from tests.support.conversation import CONFIG, operation, plan

runner = CliRunner()

LEFT = "\x1b[D"
RIGHT = "\x1b[C"
ENTER = "\r"

NETWORK_COURSE = "生成式软件工程（网课）"
ICS_COURSE = "计算机系统基础（ICS）"
ICS_WITH_ROOM = "计算机系统基础（ICS）（仙一107）"
HISTORY_COURSE = "中国近代史纲要（仙二-404）"

FIRST_SENTENCE = (
    "记录我明天(每周二)的课表："
    f"{NETWORK_COURSE}、{ICS_COURSE}、{HISTORY_COURSE}"
)
REVISION_SENTENCE = "ics的地点是在仙一107"

INTERNAL_LEAKS = ("Traceback", "jsonschema", "sqlite3.", "^[[", "[D", "[C")


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


def _weekly(title: str) -> dict[str, object]:
    return operation(
        "calendar.recurring.create_weekly",
        {
            "title": title,
            "weekday": 2,
            "start_local_time": "14:00",
            "end_local_time": "16:00",
            "timezone": None,
            "starts_on": None,
            "ends_on": None,
        },
    )


def _classes(*, ics_title: str) -> str:
    return plan(
        operation(
            "calendar.recurring.create_weekly",
            {
                "title": NETWORK_COURSE,
                "weekday": 2,
                "start_local_time": "10:00",
                "end_local_time": "12:00",
                "timezone": None,
                "starts_on": None,
                "ends_on": None,
            },
        ),
        _weekly(ics_title),
        operation(
            "calendar.recurring.create_weekly",
            {
                "title": HISTORY_COURSE,
                "weekday": 2,
                "start_local_time": "18:30",
                "end_local_time": "21:20",
                "timezone": None,
                "starts_on": None,
                "ends_on": None,
            },
        ),
    )


def _connect(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(tmp_path / "data" / "growing-assistant" / "assistant.db"))


def _active_rule_titles(tmp_path: Path) -> list[str]:
    connection = _connect(tmp_path)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT title FROM recurring_calendar_rules WHERE status = 'active' "
                "ORDER BY start_local_time"
            )
        ]
    finally:
        connection.close()


def _operation_statuses(tmp_path: Path) -> list[str]:
    connection = _connect(tmp_path)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT status FROM conversation_operations ORDER BY rowid"
            )
        ]
    finally:
        connection.close()


def _external_counts(tmp_path: Path) -> dict[str, int]:
    connection = _connect(tmp_path)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("action_requests", "approvals", "execution_runs")
        }
    finally:
        connection.close()


def test_the_real_transcript_creates_only_the_revised_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    model = FakeModelAdapter()
    model.queue_text(_classes(ics_title=ICS_COURSE))
    model.queue_text(_classes(ics_title=ICS_WITH_ROOM))
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)

    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput(), history=InMemoryHistory())
        monkeypatch.setattr(
            cli_chat,
            "build_input_source",
            lambda: InteractiveConversationInput(session=session),
        )
        # The first message is a statement about the week: Tree asks before writing anything.
        pipe.send_text(FIRST_SENTENCE + ENTER)
        # The revision, typed and then edited with arrow keys exactly as a person corrects a typo:
        # the terminal sends `^[[D`/`^[[C`, and the editor must consume them.
        pipe.send_text("ics的地点是在仙一17" + LEFT + "0" + RIGHT + LEFT + ENTER)
        pipe.send_text("可以" + ENTER)
        pipe.send_text("/exit" + ENTER)
        result = runner.invoke(app, ["chat"], input="", env=env)

    assert result.exit_code == 0, result.output
    output = result.output
    for leak in INTERNAL_LEAKS:
        assert leak not in output, leak

    # Tree asked twice: once about the original schedule, once about the revised one.
    assert output.count("要把这些加入固定安排吗？") == 2
    assert f"- 每周二 14:00–16:00 · {ICS_COURSE}\n" in output
    assert f"- 每周二 14:00–16:00 · {ICS_WITH_ROOM}\n" in output

    # The edited text reached the model exactly, and no terminal control sequence travelled with it.
    revision_payload = model.requests[1].messages[-1].content
    assert REVISION_SENTENCE in revision_payload
    assert "\\u001b" not in revision_payload
    assert "仙一17" not in revision_payload

    # Exactly the three revised rules exist; the obsolete ICS-without-room rule was never created.
    assert _active_rule_titles(tmp_path) == [NETWORK_COURSE, ICS_WITH_ROOM, HISTORY_COURSE]
    assert ICS_COURSE not in _active_rule_titles(tmp_path)
    # Three superseded operations and three applied ones — six rows, nothing pending or unknown.
    assert sorted(_operation_statuses(tmp_path)) == ["applied"] * 3 + ["rejected"] * 3
    # A local confirmation is local: no ActionRequest, no Approval, no ExecutionRun.
    assert _external_counts(tmp_path) == {
        "action_requests": 0,
        "approvals": 0,
        "execution_runs": 0,
    }
    assert "已加入固定安排：" in output
    assert output.count("已加入固定安排") == 1, "one confirmed group is one answer"
