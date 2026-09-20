"""Restarting the daemon is not consent (ADR-0032 §21/§24/§25/§39/§40).

An approval binds one exact fingerprint to one human decision at one moment. "The daemon restarted
and saw an approved action" is not a second decision, and an `UNKNOWN` external result is not a
licence to try again — so a restart must do nothing at all to either. The tests start the real
`assistantd` process against a runtime that contains a prepared, approved `mail.send` action and an
`UNKNOWN` execution, stop it, and check the database is untouched. Then, in process, they prove the
action *was* executable — so the daemon's silence is a boundary, not a coincidence.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.adapters.mail.smtp import SmtpMailExecutor
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.mail_send_actions import MailSendActionService
from assistant.domain.action import ActionRequestStatus
from assistant.domain.execution import ExecutionRunStatus
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_send import SqliteMailSendRepository
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.mail_send import (
    SEND_ACCOUNT_ID,
    SMTP_PASSWORD,
    StrictSmtpServer,
    assistant_config,
    smtp_account,
)
from tests.support.ops import RuntimeFixture

DAEMON_TIMEOUT_SECONDS = 20.0

CONFIG = """format_version = 1

[indexing]
interval_seconds = 10
run_on_startup = true

[scheduler]
poll_interval_seconds = 15
replan_debounce_seconds = 60
"""
"""No mail account and no watcher: the daemon runs index-sync and the scheduler only."""


def _environment(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "growing-assistant"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG, encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def _daemon_command() -> list[str]:
    script = Path(sys.executable).parent / "assistantd"
    if script.is_file():
        return [str(script)]
    return [sys.executable, "-c", "from assistant.daemon import main; main()"]


def _run_daemon_briefly(tmp_path: Path, environment: dict[str, str]) -> int:
    """Start the real daemon, let it settle, stop it with SIGTERM, and return its exit status."""
    process = subprocess.Popen(
        _daemon_command(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + DAEMON_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:  # pragma: no cover - a daemon that failed to start
                break
            time.sleep(0.2)
            if (tmp_path / "data" / "growing-assistant" / "assistantd.lock").exists():
                # Started and holding its lock: give the services a moment, then stop it.
                time.sleep(1.0)
                break
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.communicate(timeout=DAEMON_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failure
            process.kill()
            process.communicate(timeout=DAEMON_TIMEOUT_SECONDS)
    return process.returncode


async def _approved_send(fixture: RuntimeFixture):
    """A real prepared `mail.send` action with a live, human approval — and nothing executed."""
    from tests.support.mail_intelligence import MailStores, build_message

    stores = MailStores(fixture.database, fixture.clock)
    message = await stores.store(
        build_message(
            message_id_header="<restart@example.edu>",
            subject="SE lab deadline",
            body_text="Could you submit the report by Friday?",
        )
    )
    drafts = SqliteMailDraftRepository(fixture.database)
    from tests.support.mail_send import build_sendable_draft

    stored_draft = replace(
        build_sendable_draft(), reply_to_message_id=message.id
    )
    draft = await drafts.add_draft(stored_draft)
    actions = SqliteActionRepository(fixture.database)
    cases = CaseService(SqliteCaseRepository(fixture.database), actions, fixture.clock)
    case = await cases.create_case("Reply about the report")
    config = assistant_config()
    prepared = await MailSendActionService(
        drafts,
        SqliteMailRepository(fixture.database),
        SqliteCaseRepository(fixture.database),
        actions,
        SqliteMailSendRepository(fixture.database),
        fixture.clock,
        accounts=config.mail.accounts,
        message_id_factory=lambda domain: f"<restart-1@{domain}>",
        date_header_factory=lambda now: "Thu, 26 Sep 2026 09:00:00 +0000",
    ).prepare_send(draft.id, case.id)
    approvals = ApprovalService(
        actions, fixture.clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
    )
    issued = await approvals.create_challenge(prepared.action.id)
    await approvals.approve(prepared.action.id, issued.token)
    assert message.id is not None
    return prepared.action.id


def test_restarting_the_daemon_never_executes_an_approved_action(tmp_path: Path) -> None:
    """§39: an approved action survives a restart as an approved action, and nothing else."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    action_id = asyncio.run(_approved_send(fixture))
    environment = _environment(tmp_path)

    assert _run_daemon_briefly(tmp_path, environment) == 0
    assert _run_daemon_briefly(tmp_path, environment) == 0  # twice, for good measure

    with fixture.database.connect() as connection:
        executions = connection.execute(
            "SELECT count(*) AS total FROM execution_runs"
        ).fetchone()["total"]
        action = connection.execute(
            "SELECT status FROM action_requests WHERE id = ?", (str(action_id),)
        ).fetchone()
        approvals = connection.execute(
            "SELECT count(*) AS total FROM approvals WHERE consumed_at IS NULL"
        ).fetchone()["total"]

    assert executions == 0  # the daemon executed nothing
    assert action["status"] == ActionRequestStatus.PREPARED.value
    assert approvals == 1  # the human approval is still the live one

    # The action really was executable: an explicit `pw action execute` does send it, once.
    server = StrictSmtpServer()
    executor = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )
    result = asyncio.run(
        ActionExecutionService(
            SqliteActionRepository(fixture.database),
            {executor.action_type: executor},
            fixture.clock,
        ).execute(action_id)
    )
    assert result.run.status.value == "succeeded"
    assert server.stages.count("data") == 1


def test_restarting_the_daemon_never_retries_an_unknown_execution(tmp_path: Path) -> None:
    """§40: `UNKNOWN` stays unresolved across restarts, and keeps blocking a blind retry."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    action_id = asyncio.run(_approved_send(fixture))

    with fixture.database.connect() as connection:
        run_id = str(uuid4())
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, error_summary, "
            "started_at, finished_at) SELECT ?, id, (SELECT id FROM approvals WHERE action_id = ? "
            "AND consumed_at IS NULL), 'unknown', ?, ?, ? FROM action_requests WHERE id = ?",
            (
                run_id,
                str(action_id),
                "the SMTP conversation was interrupted after DATA",
                "2026-09-26T09:00:00.000000+00:00",
                "2026-09-26T09:00:05.000000+00:00",
                str(action_id),
            ),
        )
    environment = _environment(tmp_path)

    assert _run_daemon_briefly(tmp_path, environment) == 0
    assert _run_daemon_briefly(tmp_path, environment) == 0

    with fixture.database.connect() as connection:
        runs = connection.execute(
            "SELECT status, error_summary FROM execution_runs WHERE action_id = ?",
            (str(action_id),),
        ).fetchall()
        action = connection.execute(
            "SELECT status FROM action_requests WHERE id = ?", (str(action_id),)
        ).fetchone()

    assert len(runs) == 1  # no second attempt appeared
    assert runs[0]["status"] == ExecutionRunStatus.UNKNOWN.value
    assert runs[0]["error_summary"].startswith("the SMTP conversation")
    assert action["status"] == ActionRequestStatus.PREPARED.value

    # And an explicit execution still refuses to touch it.
    server = StrictSmtpServer()
    executor = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )
    from assistant.domain.errors import ActionExecutionUnresolved

    with pytest.raises(ActionExecutionUnresolved):
        asyncio.run(
            ActionExecutionService(
                SqliteActionRepository(fixture.database),
                {executor.action_type: executor},
                fixture.clock,
            ).execute(action_id)
        )
    assert server.stages == []  # the transport was never touched
