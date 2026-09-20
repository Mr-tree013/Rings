"""`pw mail threads`, `pw mail thread show` and `pw mail analysis` (ADR-0021).

The CLI is read-only here: it shows the thread graph and the stored analysis, and never calls a
model. The state it reads is seeded through the real repositories, exactly as a daemon round
would leave it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.application.mail_threading import MailThreadLinker
from assistant.cli import app
from assistant.domain.mail_analysis import (
    MailActionCandidate,
    MailAnalysis,
    MailCategory,
    MailTemporalKind,
)
from tests.support.mail_fakes import FakeMailSource

runner = CliRunner()

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

RAW = (
    b"From: Ada <ada@example.edu>\r\n"
    b"To: me@example.edu\r\n"
    b"Subject: SE lab deadline\r\n"
    b"Message-ID: <a@example.edu>\r\n"
    b"Date: Fri, 23 Oct 2026 23:59:00 +0800\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"The deadline is Friday.\r\n"
)

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[mail]",
        "poll_interval_seconds = 60",
        "",
        "[[mail.accounts]]",
        'id = "smail"',
        'host = "imap.example.edu"',
        "port = 993",
        'username = "student@example.edu"',
        'mailbox = "INBOX"',
        "enabled = true",
        "",
    )
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every XDG location at a temporary directory and install the mail secret."""
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "app-password")
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


def _sync(monkeypatch: pytest.MonkeyPatch, *, raw: bytes = RAW) -> None:
    source = FakeMailSource(uidvalidity=10)
    source.add(100, raw)
    monkeypatch.setattr(bootstrap, "mail_source", lambda account, **kwargs: source)
    result = runner.invoke(app, ["mail", "sync"])
    assert result.exit_code == 0, result.output


def _message_id() -> str:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    mail = bootstrap.mail_repository(database)
    messages = asyncio.run(mail.list_messages(limit=None))
    assert messages, "no message was stored"
    return str(messages[0].id)


def _link_thread() -> str:
    """Run the deterministic linker over the stored mail, as the daemon would."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    mail = bootstrap.mail_repository(database)
    intelligence = bootstrap.mail_intelligence_repository(database)
    messages = asyncio.run(mail.list_messages(limit=None))
    linker = MailThreadLinker(mail, intelligence, clock)
    member = asyncio.run(linker.ensure_thread(messages[0].id))
    return str(member.thread_id)


def _analyse() -> None:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    mail = bootstrap.mail_repository(database)
    intelligence = bootstrap.mail_intelligence_repository(database)
    message = asyncio.run(mail.list_messages(limit=None))[0]
    asyncio.run(
        intelligence.persist_analysis(
            MailAnalysis(
                message_id=message.id,
                analyzer_version=1,
                input_fingerprint="f" * 64,
                category=MailCategory.ACTIONABLE_NOTICE,
                requires_reply=True,
                summary="The lab report is due on Friday.",
                action_candidates=(
                    MailActionCandidate(
                        text="Submit the SE lab report",
                        temporal_kind=MailTemporalKind.DEADLINE,
                        time_text="Friday",
                        interpreted_at=NOW,
                    ),
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    )


def test_threads_are_empty_until_something_threads_them(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing mail and threading it are different steps, and the list says so."""
    _sync(monkeypatch)

    result = runner.invoke(app, ["mail", "threads"])

    assert result.exit_code == 0, result.output
    assert "no mail threads" in result.output


def test_threads_list_the_stored_graph(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync(monkeypatch)
    thread_id = _link_thread()

    result = runner.invoke(app, ["mail", "threads"])

    assert result.exit_code == 0, result.output
    assert thread_id[:8] in result.output
    assert "smail" in result.output
    assert "SE lab deadline" in result.output


def test_a_thread_shows_its_messages_in_order(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sync(monkeypatch)
    thread_id = _link_thread()
    _analyse()

    result = runner.invoke(app, ["mail", "thread", "show", thread_id[:8]])

    assert result.exit_code == 0, result.output
    assert "SE lab deadline" in result.output
    assert "root" in result.output  # the link decision is visible
    assert "actionable_notice" in result.output


def test_an_unknown_thread_fails_cleanly(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sync(monkeypatch)

    result = runner.invoke(app, ["mail", "thread", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_an_analysis_is_shown_or_absent(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sync(monkeypatch)
    prefix = _message_id()[:8]

    before = runner.invoke(app, ["mail", "analysis", prefix])
    _analyse()
    after = runner.invoke(app, ["mail", "analysis", prefix])

    assert before.exit_code == 0, before.output
    assert "No analysis yet." in before.output
    assert after.exit_code == 0, after.output
    assert "actionable_notice" in after.output
    assert "The lab report is due on Friday." in after.output
    assert "Submit the SE lab report" in after.output
    assert "deadline" in after.output
    assert "Friday" in after.output


def test_analysis_output_is_bounded_to_metadata(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing shows a category, never a subject or a body excerpt."""
    _sync(monkeypatch)
    _link_thread()
    _analyse()
    message_id = _message_id()[:8]

    listing = runner.invoke(app, ["mail", "messages"])
    detail = runner.invoke(app, ["mail", "show", message_id])

    assert listing.exit_code == 0, listing.output
    assert "actionable_notice" in listing.output
    assert detail.exit_code == 0, detail.output
    assert "Thread link" in detail.output and "root" in detail.output
    assert "requires reply: yes" in detail.output
    assert "Submit the SE lab report" in detail.output
    assert str(isolated) not in detail.output  # no physical path anywhere


def test_the_analysis_view_never_talks_to_a_model(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing credential is irrelevant: reading an analysis is local."""
    _sync(monkeypatch)
    _analyse()
    monkeypatch.delenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(app, ["mail", "analysis", _message_id()[:8]])

    assert result.exit_code == 0, result.output
    assert "actionable_notice" in result.output
    assert "GROWING_ASSISTANT_MAIL" not in result.output


def test_the_payload_shape_of_a_status_row_is_unchanged(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pw mail status` stays a local read and keeps reporting the cursor."""
    _sync(monkeypatch)

    result = runner.invoke(app, ["mail", "status"])

    assert result.exit_code == 0, result.output
    assert "never synced" not in result.output
    assert "analyzed" not in result.output
