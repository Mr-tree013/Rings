"""`pw mail draft acknowledge`, `pw mail send …` and `pw mail sends` (ADR-0024).

The commands are exercised against a real database with a scripted Sent mailbox, because the
interesting behaviour is the refusal: preparing sends nothing, reconciling never resends, and an
unconfirmed delivery is reported as unconfirmed rather than retried.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app
from assistant.domain.action import ActionRequest
from assistant.domain.execution import ExecutionOutcome
from tests.support.actions import ActionStores
from tests.support.mail_drafts import DraftStores, build_draft
from tests.support.mail_intelligence import MailStores, build_message
from tests.support.mail_send import (
    FROM_ADDRESS,
    SEND_ACCOUNT_ID,
    SENT_MAILBOX,
    TO_ADDRESS,
    FakeSentLookup,
)

runner = CliRunner()
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

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
        'smtp_host = "smtp.example.edu"',
        "smtp_port = 587",
        'smtp_security = "starttls"',
        'smtp_username = "student@example.edu"',
        'from_address = "student@example.edu"',
        'sent_mailbox = "Sent"',
        "",
    )
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary host with mail, SMTP and both credentials present."""
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD", "imap-secret")
    monkeypatch.setenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD", "smtp-secret")
    return tmp_path


def _clock():
    return bootstrap.system_clock()


def _seed(
    *,
    needs_user_input: tuple[str, ...] = (),
    acknowledged: bool = False,
) -> str:
    """Store a reply target, a draft and an open case, and return the draft id."""
    clock = _clock()
    database = bootstrap.runtime_database(clock)
    mail = MailStores(database, clock)
    drafts = DraftStores(database, clock)
    original = asyncio.run(
        mail.store(
            build_message(
                message_id_header="<original@example.edu>",
                from_address=TO_ADDRESS,
                subject="SE lab deadline",
                body_text="Could you submit the report by Friday?",
            )
        )
    )
    draft = build_draft(
        reply_to_message_id=original.id,
        to_addresses=(TO_ADDRESS,),
        subject="Re: SE lab deadline",
        needs_user_input=needs_user_input,
        at=NOW,
    )
    if acknowledged:
        draft = draft.acknowledge_user_input(NOW)
    asyncio.run(drafts.drafts.add_draft(draft))
    return str(draft.id)


def _add_case(title: str = "Send the report") -> str:
    result = runner.invoke(app, ["case", "add", title])
    assert result.exit_code == 0, result.output
    match = re.search(r"\b([0-9a-f]{8})\b", result.output)
    assert match is not None, result.output
    return match.group(1)


def _prepare(draft_id: str, case_prefix: str) -> str:
    result = runner.invoke(
        app, ["mail", "send", "prepare", draft_id[:8], "--case", case_prefix]
    )
    assert result.exit_code == 0, result.output
    return result.output


def _action_id_from(output: str) -> str:
    match = re.search(r"Action:\s+([0-9a-f-]{36})", output)
    assert match is not None, output
    return match.group(1)


# ------------------------------------------------------------------- acknowledge


def test_a_draft_with_questions_must_be_acknowledged_before_preparing(
    isolated: Path,
) -> None:
    draft_id = _seed(needs_user_input=("What is your student number?",))
    case = _add_case()

    blocked = runner.invoke(
        app, ["mail", "send", "prepare", draft_id[:8], "--case", case]
    )
    acknowledged = runner.invoke(app, ["mail", "draft", "acknowledge", draft_id[:8]])
    prepared = runner.invoke(
        app, ["mail", "send", "prepare", draft_id[:8], "--case", case]
    )

    assert blocked.exit_code == 1
    assert "acknowledge" in blocked.output
    assert acknowledged.exit_code == 0, acknowledged.output
    assert "version 2" in acknowledged.output
    assert prepared.exit_code == 0, prepared.output
    assert "Nothing was sent" in prepared.output


def test_acknowledging_a_draft_without_questions_is_refused(isolated: Path) -> None:
    draft_id = _seed()

    result = runner.invoke(app, ["mail", "draft", "acknowledge", draft_id[:8]])

    assert result.exit_code == 1
    assert "no open questions" in result.output


# ---------------------------------------------------------------------- prepare


def test_preparing_prints_the_exact_action_and_does_not_approve_it(isolated: Path) -> None:
    draft_id = _seed()
    case = _add_case()

    output = _prepare(draft_id, case)

    assert "Prepared exact mail send action." in output
    assert f"From:        {FROM_ADDRESS}" in output
    assert f"To:          {TO_ADDRESS}" in output
    assert "Subject:     Re: SE lab deadline" in output
    assert "Message-ID:  <" in output
    assert "Fingerprint:" in output
    assert "Nothing was sent." in output
    assert "pw action challenge" in output
    # Preparing approves nothing: the action is still waiting for a challenge.
    action_id = _action_id_from(output)
    shown = runner.invoke(app, ["action", "show", action_id[:8]])
    assert "Approval" in shown.output and "none" in shown.output


def test_preparing_twice_for_one_draft_version_is_refused(isolated: Path) -> None:
    draft_id = _seed()
    case = _add_case()
    _prepare(draft_id, case)

    again = runner.invoke(
        app, ["mail", "send", "prepare", draft_id[:8], "--case", case]
    )

    assert again.exit_code == 1
    assert "already has action" in again.output


def test_preparing_for_a_closed_case_is_refused(isolated: Path) -> None:
    draft_id = _seed()
    case = _add_case()
    runner.invoke(app, ["case", "done", case])

    result = runner.invoke(
        app, ["mail", "send", "prepare", draft_id[:8], "--case", case]
    )

    assert result.exit_code == 1
    assert "cannot receive a new action" in result.output


# ------------------------------------------------------------------------- show


def test_show_prints_the_exact_content_and_the_draft_version_warning(
    isolated: Path,
) -> None:
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))
    edited = runner.invoke(
        app, ["mail", "draft", "edit", draft_id[:8], "--body", "Different words."]
    )
    assert edited.exit_code == 0, edited.output

    shown = runner.invoke(app, ["mail", "send", "show", action_id[:8]])

    assert shown.exit_code == 0, shown.output
    assert "Delivery state" in shown.output and "draft" in shown.output
    assert "Message-ID" in shown.output
    assert "In-Reply-To" in shown.output
    assert "Prepared from draft version" in shown.output and "1" in shown.output
    assert "Current draft version" in shown.output and "2" in shown.output
    assert "WARNING: the draft has changed" in shown.output
    # The body shown is the approved one, not the edited draft's.
    assert "Thanks" in shown.output or "Dear" in shown.output
    assert "Different words." not in shown.output
    assert "No resend is performed by this command." in shown.output


def test_the_send_list_is_read_only(isolated: Path) -> None:
    empty = runner.invoke(app, ["mail", "sends"])
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))

    listed = runner.invoke(app, ["mail", "sends"])

    assert empty.exit_code == 0 and "no prepared sends" in empty.output
    assert listed.exit_code == 0, listed.output
    assert action_id[:8] in listed.output
    assert SEND_ACCOUNT_ID in listed.output
    assert TO_ADDRESS in listed.output
    assert "draft" in listed.output
    assert "Nothing here sends, approves or resends." in listed.output


# -------------------------------------------------------------------- reconcile


def test_reconciling_an_attempt_that_never_ran_is_refused(isolated: Path) -> None:
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))

    result = runner.invoke(app, ["mail", "send", "reconcile", action_id[:8]])

    assert result.exit_code == 1
    assert "cannot be reconciled" in result.output


def test_reconciling_an_unresolved_send_reports_the_lookup(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed delivery resolves; a missing one says so and stops there."""
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))
    _seed_unresolved_attempt(action_id)
    lookup = FakeSentLookup().with_match(uid=42, uidvalidity=7)
    monkeypatch.setattr(bootstrap, "ImapSentMailLookup", lambda **kwargs: lookup)

    confirmed = runner.invoke(app, ["mail", "send", "reconcile", action_id[:8]])
    shown = runner.invoke(app, ["mail", "send", "show", action_id[:8]])

    assert confirmed.exit_code == 0, confirmed.output
    assert "found" in confirmed.output
    assert "Delivery confirmed" in confirmed.output
    assert lookup.answered == [(SENT_MAILBOX, _message_id_of(action_id))]
    assert "sent" in shown.output
    assert "reconciliation history" in shown.output
    assert "found" in shown.output


def test_reconciling_without_a_confirmation_says_do_not_resend(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))
    _seed_unresolved_attempt(action_id)
    lookup = FakeSentLookup().with_nothing()
    monkeypatch.setattr(bootstrap, "ImapSentMailLookup", lambda **kwargs: lookup)

    result = runner.invoke(app, ["mail", "send", "reconcile", action_id[:8]])
    shown = runner.invoke(app, ["mail", "send", "show", action_id[:8]])

    assert result.exit_code == 0, result.output
    assert "not_found" in result.output
    assert "Do not resend blindly." in result.output
    assert "Prepare" not in result.output  # no resend path is offered
    assert "sending_unknown" in shown.output
    assert "No resend is performed by this command." in shown.output


def test_reconciliation_history_is_kept_across_lookups(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = _seed()
    case = _add_case()
    action_id = _action_id_from(_prepare(draft_id, case))
    _seed_unresolved_attempt(action_id)
    monkeypatch.setattr(
        bootstrap, "ImapSentMailLookup", lambda **kwargs: FakeSentLookup().with_nothing()
    )
    runner.invoke(app, ["mail", "send", "reconcile", action_id[:8]])
    monkeypatch.setattr(
        bootstrap,
        "ImapSentMailLookup",
        lambda **kwargs: FakeSentLookup().with_ambiguity(),
    )

    second = runner.invoke(app, ["mail", "send", "reconcile", action_id[:8]])
    shown = runner.invoke(app, ["mail", "send", "show", action_id[:8]])

    assert "ambiguous" in second.output
    assert "More than one message carries this Message-ID." in second.output
    assert shown.output.count("not_found") >= 1
    assert shown.output.count("ambiguous") >= 1


# ----------------------------------------------------------------------- accounts


def test_accounts_report_smtp_presence_without_a_network_call(isolated: Path) -> None:
    result = runner.invoke(app, ["mail", "accounts"])

    assert result.exit_code == 0, result.output
    assert "outbound (SMTP)" in result.output
    assert "smtp.example.edu:587 (starttls)" in result.output
    assert FROM_ADDRESS in result.output
    assert SENT_MAILBOX in result.output
    assert "smtp-secret" not in result.output


def test_accounts_show_a_missing_smtp_credential(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD")

    result = runner.invoke(app, ["mail", "accounts"])
    doctor = runner.invoke(app, ["doctor"])

    assert "missing" in result.output
    assert doctor.exit_code == 1
    assert "smtp" in doctor.output.lower()


# ------------------------------------------------------------------------ helpers


def _seed_unresolved_attempt(action_reference: str) -> None:
    """Leave an UNKNOWN execution behind, exactly as a dropped SMTP connection would."""
    clock = _clock()
    database = bootstrap.runtime_database(clock)
    stores = ActionStores(database, clock)
    action_id = asyncio.run(stores.actions.resolve_action_id(action_reference[:8]))
    action = asyncio.run(stores.actions.get_action(action_id))
    assert action is not None
    asyncio.run(_prepare_and_fail(stores, clock, action))


async def _prepare_and_fail(stores: ActionStores, clock, action: ActionRequest) -> None:
    from assistant.application.approval_service import ApprovalService

    approvals = ApprovalService(
        stores.actions, clock, token_factory=lambda: "cli-token"
    )
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    started = await stores.actions.begin_execution(
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        run_id=uuid4(),
        now=clock.now(),
    )
    await stores.actions.finish_execution(
        run_id=started.run.id,
        outcome=ExecutionOutcome.unknown("the connection dropped during DATA"),
        at=clock.now(),
    )


def _message_id_of(action_reference: str) -> str:
    clock = _clock()
    database = bootstrap.runtime_database(clock)
    stores = ActionStores(database, clock)
    action_id = asyncio.run(stores.actions.resolve_action_id(action_reference[:8]))
    action = asyncio.run(stores.actions.get_action(action_id))
    assert action is not None
    return str(action.payload["rfc_message_id"])
