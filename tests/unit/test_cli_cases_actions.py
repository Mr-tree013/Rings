"""`pw cases` and `pw actions`: the approval workflow from the command line (ADR-0023).

The important assertions are about what the commands *refuse* to do: they never create an action
from arbitrary input, they never approve with a wrong token, they never echo a token back, and
`pw action execute` cannot run anything in a deployment with no registered capability.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary host, so the CLI writes its runtime database somewhere disposable."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


def _add_case(title: str = "Register for the course") -> str:
    result = runner.invoke(app, ["case", "add", title])
    assert result.exit_code == 0, result.output
    match = re.search(r"\b([0-9a-f]{8})\b", result.output)
    assert match is not None, result.output
    return match.group(1)


def _prepare_action(case_prefix: str, action_type: str = "mail.send") -> str:
    """Prepare an action through the application API: there is no CLI that does this."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.case_service(clock, database)
    case_id = asyncio.run(service.resolve_case_id(case_prefix))
    action = asyncio.run(
        service.prepare_action(
            case_id,
            action_type,
            {"to": "ada@example.edu", "subject": "Registration", "body": "Please register me."},
        )
    )
    return str(action.id)


# ------------------------------------------------------------------------ cases


def test_cases_start_empty_and_can_be_created(isolated: Path) -> None:
    empty = runner.invoke(app, ["cases"])
    assert empty.exit_code == 0, empty.output
    assert "no cases" in empty.output

    prefix = _add_case()
    listed = runner.invoke(app, ["cases"])
    shown = runner.invoke(app, ["case", "show", prefix])

    assert listed.exit_code == 0, listed.output
    assert "Register for the course" in listed.output
    assert "open" in listed.output
    assert shown.exit_code == 0, shown.output
    assert "Register for the course" in shown.output
    assert "actions: none" in shown.output


def test_a_case_shows_the_actions_it_contains(isolated: Path) -> None:
    prefix = _add_case()
    _prepare_action(prefix)

    shown = runner.invoke(app, ["case", "show", prefix])

    assert shown.exit_code == 0, shown.output
    # The action table has its own headers: the case table must not be widened by them.
    assert "Fingerprint" in shown.output
    assert "Prepared" in shown.output
    assert "mail.send" in shown.output
    assert "prepared" in shown.output


def test_a_case_can_be_completed_or_cancelled_but_not_both(isolated: Path) -> None:
    done_prefix = _add_case("Finish the registration")
    cancelled_prefix = _add_case("Abandon the registration")

    done = runner.invoke(app, ["case", "done", done_prefix])
    cancelled = runner.invoke(app, ["case", "cancel", cancelled_prefix])
    again = runner.invoke(app, ["case", "done", done_prefix])

    assert done.exit_code == 0, done.output
    assert "completed" in done.output
    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled" in cancelled.output
    assert again.exit_code == 1
    assert "cannot move" in again.output


def test_an_unknown_case_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["case", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


# ---------------------------------------------------------------------- actions


def test_actions_start_empty(isolated: Path) -> None:
    result = runner.invoke(app, ["actions"])

    assert result.exit_code == 0, result.output
    assert "no actions" in result.output


def test_an_action_is_shown_with_its_exact_payload_and_fingerprint(isolated: Path) -> None:
    prefix = _add_case()
    action_id = _prepare_action(prefix)

    listed = runner.invoke(app, ["actions"])
    shown = runner.invoke(app, ["action", "show", action_id[:8]])

    assert listed.exit_code == 0, listed.output
    assert "mail.send" in listed.output and "prepared" in listed.output
    assert "none" in listed.output  # no approval, no execution
    assert shown.exit_code == 0, shown.output
    assert action_id in shown.output
    assert "Fingerprint" in shown.output
    assert "Canonical payload" in shown.output
    # The whole payload is printed, key by key: nothing about what would happen is hidden.
    assert '"to": "ada@example.edu"' in shown.output
    assert '"subject": "Registration"' in shown.output
    assert '"body": "Please register me."' in shown.output
    assert "Approval" in shown.output and "Execution" in shown.output


def test_a_challenge_prints_a_token_once_and_stores_only_its_hash(isolated: Path) -> None:
    prefix = _add_case()
    action_id = _prepare_action(prefix)

    result = runner.invoke(app, ["action", "challenge", action_id[:8]])

    assert result.exit_code == 0, result.output
    assert "fingerprint" in result.output
    token = re.search(r"approval token\s+(\S+)", result.output)
    assert token is not None, result.output
    assert "shown once" in result.output
    assert "not stored in plaintext" in result.output

    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    with database.connect() as connection:
        dumped = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM approval_challenges").fetchall()
            for value in tuple(row)
        )
    assert token.group(1) not in dumped
    # The same plaintext is never printed again by a later command.
    later = runner.invoke(app, ["action", "show", action_id[:8]])
    assert token.group(1) not in later.output


def test_approving_with_the_token_works_and_with_a_wrong_one_does_not(isolated: Path) -> None:
    prefix = _add_case()
    action_id = _prepare_action(prefix)
    challenged = runner.invoke(app, ["action", "challenge", action_id[:8]])
    token = re.search(r"approval token\s+(\S+)", challenged.output)
    assert token is not None, challenged.output

    rejected = runner.invoke(
        app, ["action", "approve", action_id[:8], "not-the-real-token"]
    )
    approved = runner.invoke(app, ["action", "approve", action_id[:8], token.group(1)])

    assert rejected.exit_code == 1
    assert "not-the-real-token" not in rejected.output
    assert approved.exit_code == 0, approved.output
    assert f"Approved exact action {action_id}" in approved.output
    assert "Fingerprint:" in approved.output
    assert "Nothing has been executed" in approved.output
    shown = runner.invoke(app, ["action", "show", action_id[:8]])
    assert "valid" in shown.output


def test_executing_without_a_capability_consumes_nothing(isolated: Path) -> None:
    """The production executor set is empty, so this is the honest answer."""
    prefix = _add_case()
    action_id = _prepare_action(prefix)
    challenged = runner.invoke(app, ["action", "challenge", action_id[:8]])
    token = re.search(r"approval token\s+(\S+)", challenged.output)
    assert token is not None, challenged.output
    runner.invoke(app, ["action", "approve", action_id[:8], token.group(1)])

    result = runner.invoke(app, ["action", "execute", action_id[:8]])

    assert result.exit_code == 1
    assert "capability unavailable for action type mail.send" in result.output.lower()
    shown = runner.invoke(app, ["action", "show", action_id[:8]])
    assert "valid" in shown.output  # the approval was not spent
    assert "none" in shown.output  # and no execution run exists


def test_executing_something_that_was_never_approved_is_refused(isolated: Path) -> None:
    prefix = _add_case()
    action_id = _prepare_action(prefix, "ehall.submit-certificate")

    result = runner.invoke(app, ["action", "execute", action_id[:8]])

    assert result.exit_code == 1
    assert "capability unavailable" in result.output.lower()


def test_a_prepared_action_can_be_cancelled(isolated: Path) -> None:
    prefix = _add_case()
    action_id = _prepare_action(prefix)

    cancelled = runner.invoke(app, ["action", "cancel", action_id[:8]])
    afterwards = runner.invoke(app, ["action", "challenge", action_id[:8]])

    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled" in cancelled.output
    assert afterwards.exit_code == 1
    assert "not prepared" in afterwards.output


def test_there_is_no_command_that_creates_or_forces_an_action(isolated: Path) -> None:
    """Creation belongs to a typed factory, and approval is never bypassable."""
    for arguments in (
        ["action", "create", "--type", "mail.send", "--payload", "{}"],
        ["action", "approve", "ffffffff", "--force"],
        ["action", "execute", "ffffffff", "--yes"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code != 0, arguments

    help_text = runner.invoke(app, ["action", "--help"]).output
    assert "create" not in help_text
    assert "force" not in help_text
