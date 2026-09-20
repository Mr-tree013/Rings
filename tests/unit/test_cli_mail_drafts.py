"""`pw mail drafts`, `pw mail draft create|show|edit` (ADR-0022).

The commands are exercised with a scripted provider and a knowledge spy, because the interesting
part is not the prose but the boundary: who triggers a search, what reaches the provider and what
the stored state looks like afterwards.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from tests.support.mail_drafts import KnowledgeSpy, draft_response, make_evidence
from tests.support.mail_fakes import FakeMailSource

runner = CliRunner()

RAW = (
    b"From: Ada <ada@example.edu>\r\n"
    b"Reply-To: registrar@example.edu\r\n"
    b"To: me@example.edu\r\n"
    b"Subject: SE lab deadline\r\n"
    b"Message-ID: <a@example.edu>\r\n"
    b"Date: Fri, 23 Oct 2026 23:59:00 +0800\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Could you submit the report by Friday?\r\n"
)

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
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
    """A temporary host with mail, a model section and a stored message."""
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "app-password")
    monkeypatch.setenv("COLUMNS", "200")
    source = FakeMailSource(uidvalidity=10)
    source.add(100, RAW)
    monkeypatch.setattr(bootstrap, "mail_source", lambda account, **kwargs: source)
    synced = runner.invoke(app, ["mail", "sync"])
    assert synced.exit_code == 0, synced.output
    return tmp_path


def _message_prefix() -> str:
    listing = runner.invoke(app, ["mail", "messages"])
    match = re.search(r"\b([0-9a-f]{8})\b", listing.output)
    assert match is not None, listing.output
    return match.group(1)


def _install_model(monkeypatch: pytest.MonkeyPatch, model: FakeModelAdapter) -> None:
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)


def _install_knowledge(monkeypatch: pytest.MonkeyPatch, spy: KnowledgeSpy) -> None:
    monkeypatch.setattr(
        bootstrap, "grounded_context_builder", lambda clock, database: spy
    )


def test_without_drafts_the_list_says_so(isolated: Path) -> None:
    result = runner.invoke(app, ["mail", "drafts"])

    assert result.exit_code == 0, result.output
    assert "no mail drafts" in result.output


def test_a_draft_is_written_locally_and_never_sent(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModelAdapter().queue_text(draft_response(body="Yes, Friday works."))
    spy = KnowledgeSpy((make_evidence(),))
    _install_model(monkeypatch, model)
    _install_knowledge(monkeypatch, spy)

    created = runner.invoke(app, ["mail", "draft", "create", _message_prefix()])

    assert created.exit_code == 0, created.output
    assert "registrar@example.edu" in created.output  # Reply-To, not From
    assert "Re: SE lab deadline" in created.output
    assert "Yes, Friday works." in created.output
    assert "Nothing was sent" in created.output
    assert "knowledge sources used: 0" in created.output
    # The privacy boundary: no query means no search at all.
    assert spy.called is False
    assert len(model.requests) == 1
    listing = runner.invoke(app, ["mail", "drafts"])
    assert "model_generated" in listing.output
    assert "Nothing was sent" in listing.output


def test_an_explicit_context_query_reads_the_index(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModelAdapter().queue_text(
        draft_response(used_source_ids=["S1"], body="My office hours are Tuesday.")
    )
    spy = KnowledgeSpy((make_evidence(content="Office hours are Tuesday 14:00-16:00."),))
    _install_model(monkeypatch, model)
    _install_knowledge(monkeypatch, spy)

    created = runner.invoke(
        app,
        [
            "mail",
            "draft",
            "create",
            _message_prefix(),
            "--context-query",
            "my office hours",
            "--root",
            "university",
        ],
    )

    assert created.exit_code == 0, created.output
    assert spy.queries == [("my office hours", "university", 6)]
    assert "knowledge sources used: 1" in created.output
    assert "vault://university/notes/office-hours.md" in created.output
    assert "lines 18-31" in created.output
    assert str(isolated) not in created.output  # never a physical path


def test_root_and_limit_are_validated_before_anything_runs(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModelAdapter()
    spy = KnowledgeSpy()
    _install_model(monkeypatch, model)
    _install_knowledge(monkeypatch, spy)
    prefix = _message_prefix()

    without_query = runner.invoke(
        app, ["mail", "draft", "create", prefix, "--root", "university"]
    )
    bad_limit = runner.invoke(
        app, ["mail", "draft", "create", prefix, "--context-limit", "0"]
    )

    assert without_query.exit_code == 1
    assert "--context-query" in without_query.output
    assert bad_limit.exit_code == 1
    assert model.requests == []
    assert spy.called is False


def test_a_draft_can_be_shown_and_edited(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModelAdapter().queue_text(
        draft_response(body="Draft body.", needs_user_input=["Which day works?"])
    )
    _install_model(monkeypatch, model)
    _install_knowledge(monkeypatch, KnowledgeSpy())
    created = runner.invoke(app, ["mail", "draft", "create", _message_prefix()])
    draft_id = re.search(r"\b[0-9a-f-]{36}\b", created.output)
    assert draft_id is not None
    prefix = draft_id.group(0)[:8]

    shown = runner.invoke(app, ["mail", "draft", "show", prefix])
    edited = runner.invoke(
        app,
        ["mail", "draft", "edit", prefix, "--body", "My own words.", "--subject", "Re: mine"],
    )
    after = runner.invoke(app, ["mail", "draft", "show", prefix])

    assert shown.exit_code == 0, shown.output
    assert "Draft body." in shown.output
    assert "Which day works?" in shown.output
    assert "model_generated" in shown.output
    assert "Knowledge sources: none" in shown.output
    assert edited.exit_code == 0, edited.output
    assert "version 2" in edited.output
    assert "user_edited" in after.output
    assert "My own words." in after.output
    assert "Re: mine" in after.output
    assert "Draft body." not in after.output


def test_editing_needs_a_change_and_a_real_draft(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, FakeModelAdapter())
    _install_knowledge(monkeypatch, KnowledgeSpy())

    nothing = runner.invoke(app, ["mail", "draft", "edit", "ffffffff"])
    unknown = runner.invoke(
        app, ["mail", "draft", "edit", "ffffffff", "--body", "hello"]
    )
    missing_show = runner.invoke(app, ["mail", "draft", "show", "ffffffff"])

    assert nothing.exit_code == 1
    assert "--subject or --body" in nothing.output
    assert unknown.exit_code == 1
    assert "does not exist" in unknown.output
    assert missing_show.exit_code == 1


def test_reading_and_editing_never_need_a_provider(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModelAdapter().queue_text(draft_response(body="Draft body."))
    _install_model(monkeypatch, model)
    _install_knowledge(monkeypatch, KnowledgeSpy())
    created = runner.invoke(app, ["mail", "draft", "create", _message_prefix()])
    prefix = re.search(r"\b[0-9a-f-]{36}\b", created.output)
    assert prefix is not None
    # The provider disappears; local reading and editing still work.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def _refuse(config: object) -> object:
        raise AssertionError("reading a draft must not build a provider")

    monkeypatch.setattr(bootstrap, "model_adapter", _refuse)
    listed = runner.invoke(app, ["mail", "drafts"])
    edited = runner.invoke(
        app, ["mail", "draft", "edit", prefix.group(0)[:8], "--body", "offline edit"]
    )

    assert listed.exit_code == 0, listed.output
    assert "offline edit" not in listed.output
    assert edited.exit_code == 0, edited.output
    assert "version 2" in edited.output


def test_creating_a_draft_without_a_model_fails_cleanly(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = Path(isolated) / "config" / "growing-assistant"
    (directory / "config.toml").write_text(
        CONFIG.replace('[model]\nprovider = "deepseek"\nmodel = "deepseek-flash"\n\n', ""),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["mail", "draft", "create", _message_prefix()])

    assert result.exit_code == 1
    assert "model" in result.output.lower()
    assert "Traceback" not in result.output
