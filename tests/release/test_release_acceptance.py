"""Release acceptance: the three end-to-end paths a v1 operator walks (sections 48-52).

* **fresh runtime** — nothing exists, and the first commands a person runs must work: the version,
  status, doctor, a task, the daemon starting and stopping, a backup, an audit;
* **historical runtime** — a database from an older release upgrades through the normal path and is
  then *usable*, not merely openable: status, doctor, integrity, the old rows, a daemon start/stop;
* **backup and restore** — a runtime with the representative domains is backed up, verified and
  restored into staging, then read back through a fresh bootstrap with the Phase 9A authorization
  rules still holding.

Everything runs against temporary XDG roots with fake external adapters; the suite's socket guard
covers the in-process parts, and no test contacts a provider.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from assistant.adapters.runtime.instance_lock import daemon_lock_path
from assistant.application.learning_service import LearningService
from assistant.cli import app
from assistant.store.db import Database
from assistant.store.learning import SqliteLearningRepository
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.ops import NOW, RuntimeFixture
from tests.support.upgrades import database_at_prefix, seed_era, seeded_id

runner = CliRunner()
DAEMON_TIMEOUT_SECONDS = 20.0

CONFIG = """format_version = 1

[indexing]
interval_seconds = 10
run_on_startup = true

[scheduler]
poll_interval_seconds = 15
replan_debounce_seconds = 60
"""


def _host(tmp_path: Path) -> dict[str, str]:
    """Fresh XDG roots, plus a daemon config that needs no network and no credential."""
    config = tmp_path / "config" / "growing-assistant"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG, encoding="utf-8")
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "COLUMNS": "200",
    }


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    environment = _host(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return environment


def _daemon_command() -> list[str]:
    script = Path(sys.executable).parent / "assistantd"
    if script.is_file():
        return [str(script)]
    return [sys.executable, "-c", "from assistant.daemon import main; main()"]


def _start_stop_daemon(environment: dict[str, str], lock_path: Path) -> int:
    """Start the real daemon, wait for its lock, stop it with SIGTERM, return its exit status."""
    process = subprocess.Popen(
        _daemon_command(),
        env={**os.environ, **environment},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + DAEMON_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and process.poll() is None:
            if lock_path.exists():
                time.sleep(1.0)
                break
            time.sleep(0.1)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.communicate(timeout=DAEMON_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failure
            process.kill()
            process.communicate(timeout=DAEMON_TIMEOUT_SECONDS)
    return process.returncode


# ------------------------------------------------------------------ fresh runtime (§48)


def test_a_fresh_host_can_go_from_nothing_to_a_backup(host: dict[str, str], tmp_path: Path) -> None:
    """§48: no old state, no development machine: the first commands work."""
    runtime = tmp_path / "data" / "growing-assistant"
    assert not runtime.exists()

    assert runner.invoke(app, ["status"]).exit_code == 0
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    created = runner.invoke(app, ["task", "add", "The first task on a new machine"])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(app, ["tasks"])
    assert listed.exit_code == 0, listed.output
    assert "The first task on a new machine" in listed.output

    daemon = _start_stop_daemon(host, daemon_lock_path(runtime))
    assert daemon == 0

    archive = tmp_path / "first-backup.gab"
    backup = runner.invoke(app, ["backup", "create", str(archive)])
    audit = runner.invoke(app, ["integrity", "check"])
    assert backup.exit_code == 0, backup.output
    assert archive.is_file()
    assert audit.exit_code == 0, audit.output
    assert "PASS" in audit.output


# ------------------------------------------------------------- historical runtime (§49)


def test_a_runtime_from_an_old_release_upgrades_and_then_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§49: an upgraded database is usable — status, doctor, audit, reads and a daemon run."""
    clock = FakeClock(start=NOW)
    legacy = database_at_prefix(tmp_path, "0006", clock)
    with legacy.connect() as connection:
        seeded = seed_era(connection, "0006")

    # The normal startup path (what `pw` and `assistantd` both use) performs the upgrade.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    environment = _host(tmp_path)
    target = tmp_path / "data" / "growing-assistant"
    target.mkdir(parents=True, exist_ok=True)
    # The old runtime *is* the database the new binary opens: it is moved into the data directory
    # rather than recreated, because "upgrade" means upgrading this file.
    shutil.move(str(Path(legacy.path)), str(target / "assistant.db"))
    upgraded = Database.at(target / "assistant.db")
    apply_migrations(upgraded, clock=clock)

    assert runner.invoke(app, ["status"]).exit_code == 0
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    audit = runner.invoke(app, ["integrity", "check"])
    tasks = runner.invoke(app, ["tasks"])
    assert audit.exit_code == 0, audit.output
    assert tasks.exit_code == 0, tasks.output
    assert "An old task" in tasks.output  # the row written in the old era is still readable
    assert seeded["tasks"] == seeded_id("task-era-0006")

    assert _start_stop_daemon(environment, daemon_lock_path(target)) == 0


# -------------------------------------------------------- backup and restore (§50/§51/§52)


def _seeded_runtime(tmp_path: Path) -> RuntimeFixture:
    """One runtime with the representative domains, a live capability and an UNKNOWN run."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")

    async def _seed() -> None:
        await fixture.add_task("A task worth recovering")
        await fixture.add_case("A case worth recovering")
        await fixture.add_raw_mail()
        await fixture.add_web_observation()
        fixture.insert_mobile_rows()
        learning = LearningService(SqliteLearningRepository(fixture.database), fixture.clock)
        proposal = await learning.propose_fact(
            "profile.office", "Room 302", "My office is Room 302."
        )
        await learning.confirm_fact(proposal.candidate.id)

    asyncio.run(_seed())

    async def _capabilities() -> tuple[str, str]:
        """A real prepared action with a real fingerprint, plus a live challenge and approval.

        The fingerprint matters: the integrity check re-hashes every stored payload, so a fixture
        that wrote a made-up one would (correctly) be reported as corrupt authority.
        """
        from assistant.application.approval_service import ApprovalService
        from assistant.application.case_service import CaseService
        from assistant.store.actions import SqliteActionRepository
        from assistant.store.cases import SqliteCaseRepository

        actions = SqliteActionRepository(fixture.database)
        cases = CaseService(SqliteCaseRepository(fixture.database), actions, fixture.clock)
        case = await cases.create_case("A case with a prepared action")
        action = await cases.prepare_action(case.id, "mail.send", {"to": "ada@example.edu"})
        approvals = ApprovalService(
            actions, fixture.clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
        )
        challenge = await approvals.create_challenge(action.id)
        approval = await approvals.approve(action.id, challenge.token)
        return str(action.id), str(approval.id)

    action_id, approval_id = asyncio.run(_capabilities())
    with fixture.database.connect() as connection:
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, error_summary, "
            "started_at, finished_at) VALUES (?, ?, ?, 'unknown', ?, ?, ?)",
            (
                str(uuid4()),
                action_id,
                approval_id,
                "the SMTP conversation was interrupted",
                "2026-09-26T09:00:00.000000+00:00",
                "2026-09-26T09:00:05.000000+00:00",
            ),
        )
    return fixture


def test_a_backup_of_a_full_runtime_restores_into_a_usable_staging_tree(
    tmp_path: Path, host: dict[str, str]
) -> None:
    """§50: create, verify, restore, audit and read, with the authorization rules intact."""
    fixture = _seeded_runtime(tmp_path)
    archive = tmp_path / "runtime.gab"
    destination = tmp_path / "recovery" / "growing-assistant"

    created = runner.invoke(app, ["backup", "create", str(archive)])
    verified = runner.invoke(app, ["backup", "verify", str(archive)])
    restored = runner.invoke(
        app, ["backup", "restore", str(archive), "--to", str(destination)]
    )

    assert created.exit_code == 0, created.output
    assert verified.exit_code == 0, verified.output
    assert "VALID" in verified.output
    assert restored.exit_code == 0, restored.output
    assert "Restore completed successfully." in restored.output

    restored_database = Database.at(destination / "assistant.db")
    with restored_database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # §20-§22: capabilities are invalidated, history is not.
        assert connection.execute(
            "SELECT count(*) AS total FROM approval_challenges WHERE consumed_at IS NULL"
        ).fetchone()["total"] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM approvals WHERE superseded_at IS NULL"
        ).fetchone()["total"] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM approvals WHERE consumed_at IS NOT NULL"
        ).fetchone()["total"] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM mobile_pairing_tokens WHERE consumed_at IS NULL"
        ).fetchone()["total"] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM mobile_sessions WHERE revoked_at IS NULL"
        ).fetchone()["total"] == 0
        # §23: the unresolved execution is still there, and still unresolved.
        run = connection.execute(
            "SELECT status, error_summary FROM execution_runs"
        ).fetchone()
        assert run["status"] == "unknown"
        assert run["error_summary"] == "the SMTP conversation was interrupted"
        assert connection.execute("SELECT count(*) AS total FROM tasks").fetchone()["total"] == 1
        assert connection.execute(
            "SELECT count(*) AS total FROM confirmed_facts"
        ).fetchone()["total"] == 1
        assert connection.execute(
            "SELECT count(*) AS total FROM web_observations"
        ).fetchone()["total"] == 1
        assert connection.execute(
            "SELECT count(*) AS total FROM mail_messages"
        ).fetchone()["total"] == 1

    # The restored tree is a runtime: point a fresh host at it and read it back.
    assert _start_stop_daemon(
        {
            "XDG_DATA_HOME": str(destination.parent),
            "XDG_CONFIG_HOME": host["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": host["XDG_CACHE_HOME"],
        },
        daemon_lock_path(destination),
    ) == 0

    import subprocess as _subprocess

    audit = _subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['pw', 'integrity', 'check']; "
            "from assistant.cli import main; main()",
        ],
        env={
            **os.environ,
            "XDG_DATA_HOME": str(destination.parent),
            "XDG_CONFIG_HOME": host["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": host["XDG_CACHE_HOME"],
            "COLUMNS": "200",
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert "PASS" in audit.stdout
    assert fixture.runtime.is_dir()  # the live runtime was never touched


# ------------------------------------------------------------- privacy sweep (§52)


PRIVACY_CONFIG = """format_version = 1

[indexing]
interval_seconds = 300
run_on_startup = true

[model]
provider = "deepseek"
model = "deepseek-flash"

[mail]
poll_interval_seconds = 60

[[mail.accounts]]
id = "smail"
host = "imap.example.edu"
port = 993
username = "student@example.edu"
mailbox = "INBOX"
enabled = true
smtp_host = "smtp.example.edu"
smtp_port = 465
smtp_security = "ssl"
smtp_from = "student@example.edu"
"""
"""A host with every credential configured from the environment, so doctor consults all of them."""

MODEL_KEY_SENTINEL = "MODEL-KEY-SENTINEL-11"
IMAP_PASSWORD_SENTINEL = "IMAP-PASSWORD-SENTINEL-10"
SMTP_PASSWORD_SENTINEL = "SMTP-PASSWORD-SENTINEL-9"


def test_status_doctor_integrity_and_backup_output_never_leak_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§52: sentinels in every domain stay out of every operator-facing output and log line."""
    from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
    from tests.support.mail_send import SMTP_PASSWORD
    from tests.support.mobile import (
        PAIRING_TOKEN,
        SESSION_TOKEN,
        MobileStack,
        ScriptedTokenFactory,
    )
    from tests.support.ops import MAIL_BODY, PAGE_TEXT

    environment = _host(tmp_path)
    (tmp_path / "config" / "growing-assistant" / "config.toml").write_text(
        PRIVACY_CONFIG, encoding="utf-8"
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    # Every credential the host could read is a sentinel, so a leak is unmistakable.
    monkeypatch.setenv("DEEPSEEK_API_KEY", MODEL_KEY_SENTINEL)
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", IMAP_PASSWORD_SENTINEL)
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD", SMTP_PASSWORD_SENTINEL)

    sentinels = {
        MAIL_BODY,
        PAGE_TEXT,
        "DRAFT-BODY-SENTINEL-2",
        "MANUAL-TEXT-SENTINEL-3",
        "FACT-VALUE-SENTINEL-5",
        "PLAYBOOK-NOTE-SENTINEL-6",
        SECRET_TOKEN,
        PAIRING_TOKEN,
        SESSION_TOKEN,
        SMTP_PASSWORD,
        SMTP_PASSWORD_SENTINEL,
        IMAP_PASSWORD_SENTINEL,
        MODEL_KEY_SENTINEL,
    }
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")

    async def _seed() -> None:
        await fixture.add_task("A task with no secret in it")
        await fixture.add_case("A case with no secret in it")
        await fixture.add_raw_mail()
        await fixture.add_web_observation()
        fixture.insert_mobile_rows()
        learning = LearningService(SqliteLearningRepository(fixture.database), fixture.clock)
        proposal = await learning.propose_fact(
            "profile.office", "FACT-VALUE-SENTINEL-5", "My office is FACT-VALUE-SENTINEL-5."
        )
        await learning.confirm_fact(proposal.candidate.id)

    asyncio.run(_seed())
    with fixture.database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_drafts (id, account_id, thread_id, reply_to_message_id, "
            "to_addresses_json, subject, body_text, needs_user_input_json, origin, version, "
            "generation_input_fingerprint, prompt_version, created_at, updated_at) VALUES "
            "(?, 'smail', NULL, (SELECT id FROM mail_messages LIMIT 1), '[]', 'Re: sentinel', ?, "
            "'[]', 'model_generated', 1, ?, 1, ?, ?)",
            (
                str(uuid4()),
                "DRAFT-BODY-SENTINEL-2",
                "d" * 64,
                "2026-09-26T09:00:00.000000+00:00",
                "2026-09-26T09:00:00.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO manual_inputs (id, source, text, content_sha256, created_at) "
            "VALUES (?, 'manual', ?, ?, ?)",
            (str(uuid4()), "MANUAL-TEXT-SENTINEL-3", "e" * 64, "2026-09-26T09:00:00.000000+00:00"),
        )
    # A real prepared action with a real fingerprint (the integrity check re-hashes it), a live
    # challenge whose plaintext token exists in memory, and a paired phone.
    from assistant.application.approval_service import ApprovalService
    from assistant.application.case_service import CaseService
    from assistant.store.actions import SqliteActionRepository
    from assistant.store.cases import SqliteCaseRepository

    async def _capabilities() -> str:
        actions = SqliteActionRepository(fixture.database)
        cases = CaseService(SqliteCaseRepository(fixture.database), actions, fixture.clock)
        case = await cases.create_case("A case with a live challenge")
        action = await cases.prepare_action(case.id, "mail.send", {"to": "ada@example.edu"})
        approvals = ApprovalService(
            actions, fixture.clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
        )
        await approvals.create_challenge(action.id)
        return str(action.id)

    action_id = asyncio.run(_capabilities())
    with fixture.database.connect() as connection:
        fingerprint = connection.execute(
            "SELECT fingerprint FROM action_requests WHERE id = ?", (action_id,)
        ).fetchone()["fingerprint"]
        approval_id, run_id = str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, expires_at, "
            "consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            (
                approval_id,
                action_id,
                fingerprint,
                "2026-09-26T08:00:00.000000+00:00",
                "2026-09-26T08:30:00.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, error_summary, "
            "started_at, finished_at) VALUES (?, ?, ?, 'succeeded', NULL, ?, ?)",
            (
                run_id,
                action_id,
                approval_id,
                "2026-09-26T08:05:00.000000+00:00",
                "2026-09-26T08:05:10.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO playbook_candidates (id, name, note, source_action_id, "
            "source_execution_run_id, source_action_type, source_action_fingerprint, status, "
            "created_at) VALUES (?, 'Sentinel playbook', ?, ?, ?, 'mail.send', ?, 'pending', ?)",
            (
                str(uuid4()),
                "PLAYBOOK-NOTE-SENTINEL-6",
                action_id,
                run_id,
                fingerprint,
                "2026-09-26T09:00:00.000000+00:00",
            ),
        )

    stack = MobileStack(fixture.database, fixture.clock, ScriptedTokenFactory())
    client = stack.client()
    stack.pair(client)

    caplog.set_level("INFO")
    archive = tmp_path / "privacy.gab"
    outputs = {
        "status": runner.invoke(app, ["status"]).output,
        "doctor": runner.invoke(app, ["doctor"]).output,
        "integrity": runner.invoke(app, ["integrity", "check"]).output,
        "backup create": runner.invoke(app, ["backup", "create", str(archive)]).output,
        "backup inspect": runner.invoke(app, ["backup", "inspect", str(archive)]).output,
        "backup verify": runner.invoke(app, ["backup", "verify", str(archive)]).output,
        "tasks": runner.invoke(app, ["tasks"]).output,
        "cases": runner.invoke(app, ["cases"]).output,
    }
    # The credential rows were actually consulted, so the sweep is not vacuous.
    assert "present" in outputs["doctor"]

    for label, output in outputs.items():
        for sentinel in sentinels:
            assert sentinel not in output, f"{label} leaked {sentinel}"
    for record in caplog.records:
        text = record.getMessage()
        for sentinel in sentinels:
            assert sentinel not in text, f"{record.name} logged {sentinel}"
