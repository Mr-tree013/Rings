"""The conversational mail path through the real CLI (ADR-0034 §42).

This drives `pw chat` exactly as a user would — scripted stdin, the real runtime, the real mail
store, the real approval and execution services — with only the provider and the SMTP transport
replaced. The point is that nothing about the safety chain depends on the test harness calling the
services directly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap, cli_chat
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from assistant.domain.action import ActionType
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.conversation import (
    CONFIG,
    ScriptedMailExecutor,
    draft_answer,
    operation,
    plan,
)
from tests.support.fakes import FakeClock
from tests.support.mail_fakes import FakeMailSource

runner = CliRunner()
NOW = __import__("datetime").datetime(2026, 9, 21, 0, 0, tzinfo=__import__("datetime").UTC)


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


def _database(tmp_path: Path) -> Database:
    database = Database.at(tmp_path / "data" / "growing-assistant" / "assistant.db")
    apply_migrations(database, clock=FakeClock(start=NOW))
    return database


async def _seed_mail(tmp_path: Path, message_id: str = "seed-cli@example.edu") -> str:
    """Deliver one message through the real ingestion path, and return its stored id."""
    database = _database(tmp_path)
    clock = FakeClock(start=NOW)
    config = await bootstrap.config_loader(
        tmp_path / "config" / "growing-assistant" / "config.toml"
    ).load()
    source = FakeMailSource()
    source.add(
        1,
        (
            "From: teacher@example.edu\r\n"
            "To: student@example.edu\r\n"
            "Subject: SE 实验三\r\n"
            f"Message-ID: <{message_id}>\r\n"
            "Date: Mon, 21 Sep 2026 09:00:00 +0800\r\n"
            "\r\n"
            "请在本周五之前提交实验报告。\r\n"
        ).encode(),
    )
    sync = bootstrap.mail_sync_service(
        config, clock, database, source_factory=lambda account: source
    )
    await sync.sync_once()
    stored = await bootstrap.mail_repository(database).list_messages(limit=5)
    assert stored
    return str(stored[0].id)


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers: str):
    model = FakeModelAdapter()
    for answer in answers:
        model.queue_text(answer)
    executor = ScriptedMailExecutor()
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    monkeypatch.setattr(
        bootstrap,
        "registered_action_executors",
        lambda config=None: {ActionType("mail.send"): executor},
    )
    return model, executor


def test_pw_chat_reads_drafts_previews_and_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    env = _env(tmp_path)
    message_id = asyncio.run(_seed_mail(tmp_path))
    _, executor = _install(
        tmp_path,
        monkeypatch,
        plan(operation("mail.list", {"limit": 5, "requires_reply": False})),
        plan(
            operation(
                "mail.reply_draft",
                {
                    "message_id": message_id,
                    "body_text": "好的，我周五之前交。",
                    "context_query": None,
                },
            ),
            operation(
                "mail.prepare_reply_send", {"draft_id": None, "message_id": message_id}
            ),
        ),
        draft_answer("好的，我周五之前交。"),
    )

    result = runner.invoke(
        app,
        ["chat"],
        input="最近有什么邮件？\n回复张老师，说我周五之前交\n确认发送\n/exit\n",
        env=env,
    )

    assert result.exit_code == 0, result.output
    assert "SE 实验三" in result.output  # the read
    assert "将要发送的邮件" in result.output  # the exact preview
    assert "确认发送吗" in result.output
    assert "已发送。" in result.output
    assert len(executor.calls) == 1
    assert "pw mail" not in result.output
    assert "pw action" not in result.output


def test_pw_chat_can_withdraw_a_reviewed_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    env = _env(tmp_path)
    message_id = asyncio.run(_seed_mail(tmp_path))
    _, executor = _install(
        tmp_path,
        monkeypatch,
        plan(
            operation(
                "mail.reply_draft",
                {"message_id": message_id, "body_text": None, "context_query": None},
            ),
            operation(
                "mail.prepare_reply_send", {"draft_id": None, "message_id": message_id}
            ),
        ),
        draft_answer("好的。"),
    )

    result = runner.invoke(
        app,
        ["chat"],
        input="回复张老师，说好的\n不要发\n确认发送\n/exit\n",
        env=env,
    )

    # "不要发" withdraws; the later "确认发送" finds nothing live and is interpreted normally,
    # which with no scripted answer left is an honest failure rather than a send.
    assert "没有发送" in result.output
    assert executor.calls == []
    connection = sqlite3.connect(
        str(tmp_path / "data" / "growing-assistant" / "assistant.db")
    )
    try:
        approvals = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    finally:
        connection.close()
    assert approvals == 0
    assert runs == 0


def test_help_teaches_the_mail_phrases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)

    result = runner.invoke(app, ["chat"], input="/help\n/exit\n", env=_env(tmp_path))

    assert "最近有什么需要处理的邮件？" in result.output
    assert "确认发送" in result.output
    assert "不会发送邮件" in result.output
    assert "pw action" not in result.output


def test_rings_and_pw_chat_share_the_mail_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rings` and `pw chat` differ only in their greeting, never in their behaviour."""
    calls: list[str | None] = []

    def spy(*, input_fn=None, announce=None) -> int:
        calls.append(announce)
        return 0

    monkeypatch.setattr(cli_chat, "run_conversation", spy)

    with pytest.raises(SystemExit) as exit_info:
        cli_chat.main()
    runner.invoke(app, ["chat"])

    assert exit_info.value.code == 0

    assert len(calls) == 2
    assert any(entry is not None for entry in calls)
