"""The real human transcript, as a regression (ADR-0035 §28-§32, §40-§41).

Every input here is one a person actually typed during manual use, and every failure is one that
was actually observed: a schema error shown to the user, a capability answer that contradicted the
build, a mailbox question answered with a message count, and an input byte sequence that ended the
process. The point is not that the model understands these sentences — it is scripted — but that
the runtime never crashes, never leaks internals, never mutates anything by accident, and never
claims a capability it does not have.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap, cli_chat
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from assistant.domain.action import ActionType
from tests.support.conversation import (
    CONFIG,
    ScriptedMailExecutor,
    direct_reply,
    operation,
    plan,
)

runner = CliRunner()

INTERNAL_LEAKS = (
    "Traceback",
    "jsonschema",
    "ValidationError",
    "required property",
    "is not of type",
    "sqlite3.",
    "KeyError",
    "AssertionError",
)
"""Strings that must never appear in a normal conversation reply (ADR-0035 §32)."""


class _FakeStdin:
    """A stdin that hands over raw bytes, the way a terminal does."""

    def __init__(self, payload: bytes, *, encoding: str = "utf-8") -> None:
        self.buffer = io.BytesIO(payload)
        self.encoding = encoding
        self.errors = "strict"


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


def _install(monkeypatch: pytest.MonkeyPatch, *answers: str) -> FakeModelAdapter:
    model = FakeModelAdapter()
    for answer in answers:
        model.queue_text(answer)
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    monkeypatch.setattr(
        bootstrap,
        "registered_action_executors",
        lambda config=None: {ActionType("mail.send"): ScriptedMailExecutor()},
    )
    return model


def _counts(tmp_path: Path) -> dict[str, int]:
    import sqlite3

    path = tmp_path / "data" / "growing-assistant" / "assistant.db"
    connection = sqlite3.connect(str(path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("action_requests", "approvals", "execution_runs", "tasks")
        }
    finally:
        connection.close()


def _assert_clean(output: str) -> None:
    for leak in INTERNAL_LEAKS:
        assert leak not in output, leak


# ------------------------------------------------- the transcript, line by line (§28, §41)


def test_the_observed_transcript_never_leaks_an_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact inputs from manual use, including the two that produced schema errors."""
    env = _env(tmp_path)
    _install(
        monkeypatch,
        direct_reply("你好！我在。"),
        plan(operation("task.list", {"include_terminal": False})),
        plan(operation("system.capabilities", {})),
        plan(operation("mail.accounts", {})),
        direct_reply("现在只支持回复已经收到的邮件，我不能新建一封任意收件人的邮件。"),
        plan(operation("system.capabilities", {})),
        direct_reply("你好！需要我做什么？"),
        plan(operation("task.list", {"include_terminal": False})),
        plan(operation("task.list", {"include_terminal": False})),
    )

    transcript = (
        "你能作什么\n"
        "你好\n"
        "你能做什么\n"
        "你能查找哪个邮箱?QQ还是学校邮箱?\n"
        "你能查找哪个邮箱？QQ还是学校邮箱？\n"
        "发个打招呼的邮件给我自己\n"
        "我的本地邮箱是什么\n"
        "你 好\n"
        "任务有 什么\n"
        "任务有什么\n"
        "/exit\n"
    )

    result = runner.invoke(app, ["chat"], input=transcript, env=env)

    assert result.exit_code == 0, result.output
    _assert_clean(result.output)
    counts = _counts(tmp_path)
    assert counts["action_requests"] == 0
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    # Nothing pretends the mail surface does not exist (ADR-0035 §14, §20).
    assert "本版本未启用" not in result.output
    assert "无法执行任何外部操作" not in result.output


def test_a_benign_two_field_direct_reply_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact production failure: `{"mode": "direct_reply", "reply": "…"}` with no other keys."""
    env = _env(tmp_path)
    model = _install(
        monkeypatch,
        json.dumps({"mode": "direct_reply", "reply": "你好！需要我做什么？"}),
    )

    result = runner.invoke(app, ["chat"], input="你好\n/exit\n", env=env)

    assert result.exit_code == 0, result.output
    assert "你好！需要我做什么？" in result.output
    assert len(model.requests) == 1  # no repair was needed
    _assert_clean(result.output)


def test_a_null_operations_field_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second production failure: `operations: null`."""
    env = _env(tmp_path)
    _install(
        monkeypatch,
        json.dumps(
            {
                "mode": "operations",
                "reply": None,
                "clarification": None,
                "operations": None,
            }
        ),
        json.dumps({"mode": "direct_reply", "reply": "好。", "operations": None}),
    )

    result = runner.invoke(app, ["chat"], input="任务有什么\n/exit\n", env=env)

    assert result.exit_code == 0, result.output
    _assert_clean(result.output)


# --------------------------------------------------------------- capabilities (§14-§19)


def test_capability_answers_come_from_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _install(monkeypatch, plan(operation("system.capabilities", {})))

    result = runner.invoke(app, ["chat"], input="你能做什么\n/exit\n", env=env)

    assert result.exit_code == 0, result.output
    assert "回复邮件" in result.output
    assert "确认发送" in result.output
    # Outbound compose is described truthfully, including what it still cannot do.
    assert "新建邮件" in result.output
    assert "联系人" in result.output
    assert "附件、定时发送、自动发送、通讯录/网络查询收件人" in result.output
    assert "本版本未启用" not in result.output
    # Weekly recurring schedules are described truthfully, including what is not supported.
    assert "固定安排（每周重复）" in result.output
    assert "自动避开固定安排占用的时间" in result.output
    assert "单双周" in result.output
    assert "每两周一次" in result.output
    assert "节假日或考试周除外" in result.output
    _assert_clean(result.output)


def test_mail_account_questions_are_about_configuration_not_message_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _install(monkeypatch, plan(operation("mail.accounts", {})))

    result = runner.invoke(
        app, ["chat"], input="你能查找哪个邮箱？QQ还是学校邮箱？\n/exit\n", env=env
    )

    assert result.exit_code == 0, result.output
    assert "当前配置了 1 个邮箱" in result.output
    assert "student@example.edu" in result.output
    assert "本地目前缓存了 0 封邮件" in result.output  # a separate fact, stated separately
    _assert_clean(result.output)


def test_help_uses_the_same_capability_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _install(monkeypatch)

    result = runner.invoke(app, ["chat"], input="/help\n/exit\n", env=env)

    assert result.exit_code == 0, result.output
    assert "回复邮件" in result.output
    assert "确认发送" in result.output
    assert "固定安排（每周重复）" in result.output
    assert "每周一十点到十二点有课，记下来" in result.output
    assert "本版本未启用" not in result.output


# --------------------------------------------------------------- terminal decoding (§31)


def test_invalid_input_bytes_are_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case A/B/C: valid, invalid, then valid again — the session survives."""
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    model = _install(monkeypatch, direct_reply("你好！"), direct_reply("还在。"))
    monkeypatch.setattr(
        sys,
        "stdin",
        _FakeStdin("你好\n".encode() + b"\xff\xfe\n" + "还在吗\n".encode() + b"/exit\n"),
    )

    exit_code = cli_chat.run_conversation()

    assert exit_code == 0
    assert len(model.requests) == 2  # only the two valid lines reached the model


def test_an_invalid_byte_never_becomes_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    model = _install(monkeypatch, direct_reply("好。"))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"\xff\xfe\xfd\n/exit\n"))

    exit_code = cli_chat.run_conversation()

    assert exit_code == 0
    assert model.requests == []


def test_a_missing_credential_does_not_produce_a_traceback(tmp_path: Path) -> None:
    """§2: even the "nothing is configured" path is a sentence and an exit code."""
    completed = subprocess.run(
        [sys.executable, "-c", "from assistant.cli_chat import main; main()"],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        input=b"/exit\n",
        capture_output=True,
        timeout=120,
    )
    output = completed.stdout.decode("utf-8", errors="replace") + completed.stderr.decode(
        "utf-8", errors="replace"
    )

    assert completed.returncode != 0  # there is no configuration at all
    assert "Traceback" not in output
