"""One product-level v1.1 conversation, end to end (the release acceptance flow).

Every capability this release added is exercised in a single transcript over the real runtime with
a scripted provider and a recording transport: recurring schedules, planning, contacts, outbound
mail review, confirmed facts and the today brief. What the flow pins is not that the model
understands the sentences — it is scripted — but that the runtime never crashes, never leaks an
internal, never claims an effect it did not have, and never performs an external action nobody
explicitly confirmed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
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
    "pw mail",
    "pw fact",
    "pw plan",
    "ActionRequest",
)

CLASS_TITLE = "计算机系统基础课"
CONTACT_NAME = "张老师"
CONTACT_ADDRESS = "zhang@example.edu"
OFFICE_KEY = "profile.office"
OFFICE_VALUE = "仙林校区"


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


def _counts(tmp_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(
        str(tmp_path / "data" / "growing-assistant" / "assistant.db")
    )
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "tasks",
                "plan_blocks",
                "recurring_calendar_rules",
                "contacts",
                "new_mail_drafts",
                "action_requests",
                "approvals",
                "execution_runs",
                "fact_candidates",
                "confirmed_facts",
            )
        }
    finally:
        connection.close()


def _assert_clean(output: str) -> None:
    for leak in INTERNAL_LEAKS:
        assert leak not in output, leak


def test_the_v1_1_conversation_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    model = FakeModelAdapter()
    executor = ScriptedMailExecutor()
    answers = (
        # 你能做什么
        plan(operation("system.capabilities", {})),
        # 加个任务，周五前交软件工程报告 (so the week has something to arrange)
        plan(
            operation(
                "task.create",
                {
                    "title": "交软件工程报告",
                    "description": None,
                    "priority": "high",
                    "estimated_minutes": 120,
                    "due_at": "2026-10-09T23:59:00+08:00",
                },
            )
        ),
        # 每周一 10 点到 12 点有课，记下来
        plan(
            operation(
                "calendar.recurring.create_weekly",
                {
                    "title": CLASS_TITLE,
                    "weekday": 1,
                    "start_local_time": "10:00",
                    "end_local_time": "12:00",
                    "timezone": None,
                    "starts_on": None,
                    "ends_on": None,
                },
            )
        ),
        # 根据这些固定安排规划下周
        plan(operation("plan.propose_week", {"next_week": True})),
        # 张老师邮箱是 …，记成联系人
        plan(
            operation(
                "contact.create",
                {"display_name": CONTACT_NAME, "email_address": CONTACT_ADDRESS},
            )
        ),
        # 给张老师发邮件，说我周五之前交
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "实验报告",
                    "body": "张老师您好，我周五之前交报告。",
                    "recipient_kind": "contact",
                    "recipient_address": None,
                    "recipient_name": CONTACT_NAME,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        ),
        # 记住我的办公室在仙林
        plan(
            operation(
                "fact.propose",
                {
                    "key": OFFICE_KEY,
                    "value": OFFICE_VALUE,
                    "correction_text": "记住我的办公室在仙林",
                },
            )
        ),
        # 可以 → the model answers; a generic yes must not confirm a fact
        direct_reply("要保存这条长期信息，请回复「确认记住」。"),
        # 你记得我的办公室在哪里吗？
        plan(operation("fact.show", {"key": OFFICE_KEY})),
        # 我今天有什么事？
        plan(operation("brief.today", {})),
    )
    for answer in answers:
        model.queue_text(answer)
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    monkeypatch.setattr(
        bootstrap,
        "registered_action_executors",
        lambda config=None: {ActionType("mail.send"): executor},
    )

    transcript = (
        "你能做什么\n"
        "加个任务，周五前交软件工程报告\n"
        "每周一 10 点到 12 点有课，记下来\n"
        "根据这些固定安排规划下周\n"
        "可以\n"
        f"{CONTACT_NAME}邮箱是 {CONTACT_ADDRESS}，记成联系人\n"
        f"给{CONTACT_NAME}发邮件，说我周五之前交\n"
        "不要发\n"
        "记住我的办公室在仙林\n"
        "可以\n"
        "确认记住\n"
        "你记得我的办公室在哪里吗？\n"
        "我今天有什么事？\n"
        "/exit\n"
    )

    result = runner.invoke(app, ["chat"], input=transcript, env=env)

    assert result.exit_code == 0, result.output
    output = result.output
    _assert_clean(output)

    # Capability self-knowledge is truthful about this release.
    assert "固定安排（每周重复）" in output
    assert "新建邮件" in output
    assert "长期信息" in output
    assert "今日概览" in output
    # A recurring schedule was created, and the proposal avoided it.
    assert CLASS_TITLE in output
    assert "已应用周计划提案" in output
    # A contact was recorded and then used by name, with the exact preview, then withdrawn.
    assert f"{CONTACT_NAME} <{CONTACT_ADDRESS}>" in output
    assert "将要发送的邮件" in output
    assert f"· 收件人：{CONTACT_ADDRESS}" in output
    assert "没有发送" in output
    # A fact was proposed, a generic yes did not confirm it, and the explicit phrase did.
    assert "我准备记录这条长期信息" in output
    assert "确认记住" in output
    assert "已记住" in output
    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in output
    # The today brief is deterministic and reads real state.
    assert "今天（" in output
    assert "Asia/Shanghai" in output

    counts = _counts(tmp_path)
    assert counts["recurring_calendar_rules"] == 1
    assert counts["plan_blocks"] >= 1
    assert counts["contacts"] == 1
    assert counts["new_mail_drafts"] == 1
    assert counts["action_requests"] == 1
    assert counts["confirmed_facts"] == 1
    assert counts["fact_candidates"] == 1
    # Nothing external happened: a withdrawn review leaves no approval and no execution.
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    assert executor.calls == []
