"""The outbound-mail and contacts transcript, as a regression (ADR-0037 §49).

One product-level conversation, exactly as a person types it, over the real runtime with a
scripted provider and a recording transport. The properties under test are the ones the phase
exists for: a preview is shown and *nothing* is sent, a generic "可以" does not send, a "不要发"
withdraws the review, a contact records real data and is then used by name, and no transcript ever
shows a traceback, a schema error or an internal command.
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
    "pw action",
    "ActionRequest",
    "ApprovalChallenge",
)
"""Strings that must never appear in a normal conversation reply (ADR-0035 §32, ADR-0037 §1)."""

OWN_ADDRESS = "student@example.edu"
CONTACT_ADDRESS = "codex-test@example.invalid"
CONTACT_NAME = "张老师"


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


def _install(
    monkeypatch: pytest.MonkeyPatch, *answers: str
) -> tuple[FakeModelAdapter, ScriptedMailExecutor]:
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


def _counts(tmp_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(
        str(tmp_path / "data" / "growing-assistant" / "assistant.db")
    )
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "action_requests",
                "approvals",
                "execution_runs",
                "new_mail_drafts",
                "contacts",
            )
        }
    finally:
        connection.close()


def _assert_clean(output: str) -> None:
    for leak in INTERNAL_LEAKS:
        assert leak not in output, leak


def test_the_outbound_mail_transcript_never_leaks_and_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _, executor = _install(
        monkeypatch,
        plan(operation("mail.accounts", {})),
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "打个招呼",
                    "body": "你好，这是一封来自 Rings 的测试邮件。",
                    "recipient_kind": "self",
                    "recipient_address": None,
                    "recipient_name": None,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        ),
        direct_reply("好，这封就不发了。"),
        plan(
            operation(
                "contact.create",
                {"display_name": CONTACT_NAME, "email_address": CONTACT_ADDRESS},
            )
        ),
        plan(operation("contact.list", {"include_retired": False})),
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "测试",
                    "body": f"{CONTACT_NAME}您好，这是一封测试邮件。",
                    "recipient_kind": "contact",
                    "recipient_address": None,
                    "recipient_name": CONTACT_NAME,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        ),
        direct_reply("好，这封也不发。"),
    )

    transcript = (
        "我的邮箱是什么\n"
        "发个打招呼的邮件给我自己\n"
        "可以\n"
        "不要发\n"
        f"{CONTACT_NAME}邮箱是 {CONTACT_ADDRESS}，记成联系人\n"
        "我有哪些联系人\n"
        f"给{CONTACT_NAME}发邮件，说这是测试\n"
        "不要发\n"
        "/exit\n"
    )

    result = runner.invoke(app, ["chat"], input=transcript, env=env)

    assert result.exit_code == 0, result.output
    output = result.output
    _assert_clean(output)
    # The mailbox question is answered about configuration, not about message counts.
    assert OWN_ADDRESS in output
    # The self-send preview is exact, and a generic yes did not send it.
    assert "将要发送的邮件" in output
    assert f"· 收件人：{OWN_ADDRESS}" in output
    assert "确认发送" in output
    # The contact was recorded and then used by name.
    assert f"{CONTACT_NAME} <{CONTACT_ADDRESS}>" in output
    assert f"· 收件人：{CONTACT_ADDRESS}" in output
    assert executor.calls == []
    counts = _counts(tmp_path)
    assert counts["new_mail_drafts"] == 2
    assert counts["action_requests"] == 2
    assert counts["contacts"] == 1
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0


def test_a_greeting_to_myself_shows_a_usable_greeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release gate from the product goal: one sentence, one exact preview, no send."""
    env = _env(tmp_path)
    _, executor = _install(
        monkeypatch,
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "打个招呼",
                    "body": "你好，这是一封来自 Rings 的测试邮件。",
                    "recipient_kind": "self",
                    "recipient_address": None,
                    "recipient_name": None,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        ),
    )

    result = runner.invoke(
        app, ["chat"], input="发个打招呼的邮件给我自己\n/exit\n", env=env
    )

    assert result.exit_code == 0, result.output
    _assert_clean(result.output)
    assert "打个招呼" in result.output
    assert "这是一封来自 Rings 的测试邮件" in result.output
    assert f"· 发件账号：smail（{OWN_ADDRESS}）" in result.output
    assert executor.calls == []
    assert _counts(tmp_path)["approvals"] == 0
