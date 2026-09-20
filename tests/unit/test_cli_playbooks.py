"""`pw playbook` / `pw playbooks`: the human review loop from the command line (ADR-0028).

The commands exist to be *careful*: creating a candidate never tests or promotes anything, a dry
run says out loud that it attempted no side effect, promotion refuses without a current pass, and
the convenient shortcuts that would skip review — `--force`, `--skip-test`, `run`, `apply` — do
not exist.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.cli import app
from assistant.domain.action import ActionRequest
from tests.support.actions import FakeActionExecutor
from tests.support.playbooks import MAIL_SEND_ACTION_TYPE, mail_send_payload

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary host, so the CLI writes its runtime database somewhere disposable."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


def _prefix(output: str) -> str:
    match = re.search(r"\b([0-9a-f]{8})\b", output)
    assert match is not None, output
    return match.group(1)


def _executed_action(payload: object | None = None) -> str:
    """A real successful execution in the same database the CLI will read."""

    async def run() -> ActionRequest:
        clock = bootstrap.system_clock()
        database = bootstrap.runtime_database(clock)
        cases = CaseService(
            bootstrap.case_repository(database),
            bootstrap.action_repository(database),
            clock,
        )
        case = await cases.create_case("Register for the course")
        action = await cases.prepare_action(
            case.id,
            MAIL_SEND_ACTION_TYPE,
            mail_send_payload() if payload is None else payload,
        )
        approvals = ApprovalService(
            bootstrap.action_repository(database),
            clock,
            token_factory=bootstrap.secure_approval_token_factory,
        )
        issued = await approvals.create_challenge(action.id)
        await approvals.approve(action.id, issued.token)
        executor = FakeActionExecutor(action_type=MAIL_SEND_ACTION_TYPE).script_success()
        await ActionExecutionService(
            bootstrap.action_repository(database),
            {MAIL_SEND_ACTION_TYPE: executor},
            clock,
        ).execute(action.id)
        return action

    return str(asyncio.run(run()).id)


def _add_candidate(action: str, name: str = "Approved mail shape") -> str:
    result = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            action[:8],
            "--name",
            name,
            "--note",
            "I reviewed the run and want to keep this shape.",
        ],
    )
    assert result.exit_code == 0, result.output
    return _prefix(result.output)


def _promoted_playbook() -> tuple[str, str]:
    """One full loop through the CLI: candidate, dry run, promotion."""
    candidate = _add_candidate(_executed_action())
    tested = runner.invoke(app, ["playbook", "candidate", "test", candidate])
    assert tested.exit_code == 0, tested.output
    promoted = runner.invoke(app, ["playbook", "candidate", "promote", candidate])
    assert promoted.exit_code == 0, promoted.output
    return candidate, _prefix(promoted.output)


# ------------------------------------------------------------------- candidate list


def test_candidates_start_empty(isolated: Path) -> None:
    result = runner.invoke(app, ["playbook", "candidates"])

    assert result.exit_code == 0, result.output
    assert "no playbook candidates" in result.output


# -------------------------------------------------------------------- candidate add


def test_a_candidate_can_be_created_from_a_successful_action(isolated: Path) -> None:
    action = _executed_action()

    result = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            action[:8],
            "--name",
            "Approved mail shape",
            "--note",
            "Reviewed the successful run.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Playbook candidate created." in result.output
    assert "Approved mail shape" in result.output
    assert "mail.send" in result.output
    assert "This is only a candidate." in result.output
    assert "Nothing will be replayed or executed automatically." in result.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output


def test_creating_a_candidate_does_not_test_or_promote_it(isolated: Path) -> None:
    action = _executed_action()

    _add_candidate(action[:8])

    listing = runner.invoke(app, ["playbook", "candidates"])
    assert "pending" in listing.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output


def test_name_and_note_are_required(isolated: Path) -> None:
    action = _executed_action()

    missing_note = runner.invoke(
        app, ["playbook", "candidate", "add", action[:8], "--name", "x"]
    )
    blank_name = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            action[:8],
            "--name",
            "   ",
            "--note",
            "y",
        ],
    )

    assert missing_note.exit_code != 0
    assert blank_name.exit_code == 1


def test_an_action_that_did_not_succeed_cannot_seed_a_candidate(isolated: Path) -> None:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    cases = CaseService(
        bootstrap.case_repository(database), bootstrap.action_repository(database), clock
    )
    prepared = asyncio.run(cases.prepare_action(
        asyncio.run(cases.create_case("Never executed")).id,
        MAIL_SEND_ACTION_TYPE,
        mail_send_payload(),
    ))

    result = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            str(prepared.id)[:8],
            "--name",
            "nope",
            "--note",
            "nope",
        ],
    )

    assert result.exit_code == 1
    assert "cannot seed a playbook candidate" in result.output


def test_an_unknown_action_prefix_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            "ffffffff",
            "--name",
            "x",
            "--note",
            "y",
        ],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_one_action_seeds_one_candidate(isolated: Path) -> None:
    action = _executed_action()
    _add_candidate(action[:8])

    second = runner.invoke(
        app,
        [
            "playbook",
            "candidate",
            "add",
            action[:8],
            "--name",
            "Another name",
            "--note",
            "Trying again.",
        ],
    )

    assert second.exit_code == 1
    assert "one reviewed candidate only" in second.output


# ------------------------------------------------------------------- candidate show


def test_candidate_show_prints_the_source_and_the_dry_runs(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())

    shown = runner.invoke(app, ["playbook", "candidate", "show", candidate])

    assert shown.exit_code == 0, shown.output
    assert "Source action" in shown.output
    assert "mail.send" in shown.output
    assert "pending" in shown.output
    assert "Replay tests" in shown.output
    assert "none" in shown.output
    assert "not a playbook" in shown.output
    assert "pw action show" in shown.output  # payload stays in the action view


# ------------------------------------------------------------------- candidate test


def test_a_dry_run_says_out_loud_that_it_is_a_dry_run(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())

    tested = runner.invoke(app, ["playbook", "candidate", "test", candidate])

    assert tested.exit_code == 0, tested.output
    assert "Dry-run only." in tested.output
    assert "No external side effect was attempted." in tested.output
    assert "Replay contract: mail.send v1" in tested.output
    assert "Result: passed" in tested.output
    assert "Issues: none" in tested.output
    assert "still accepted by the current local validator" in tested.output
    assert "does not prove the external operation would succeed today" in tested.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output


def test_a_failing_dry_run_says_what_happened_without_executing_anything(
    isolated: Path,
) -> None:
    """A successful run whose stored payload today's parser no longer accepts."""
    action = _executed_action({"to": "ada@example.edu"})
    candidate = _add_candidate(action[:8], name="A shape from before")

    tested = runner.invoke(app, ["playbook", "candidate", "test", candidate])

    assert tested.exit_code == 0, tested.output
    assert "Result: failed" in tested.output
    assert "payload-invalid" in tested.output
    assert "Nothing was executed" in tested.output


# ---------------------------------------------------------------- candidate promote


def test_promotion_requires_a_current_pass(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())

    promoted = runner.invoke(app, ["playbook", "candidate", "promote", candidate])

    assert promoted.exit_code == 1
    assert "no current passing dry run" in promoted.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output


def test_promotion_creates_a_playbook_and_says_what_it_is(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())
    runner.invoke(app, ["playbook", "candidate", "test", candidate])

    promoted = runner.invoke(app, ["playbook", "candidate", "promote", candidate])

    assert promoted.exit_code == 0, promoted.output
    assert "Promoted to Playbook" in promoted.output
    assert "Replay contract version: 1" in promoted.output
    assert "No execution capability was granted." in promoted.output
    assert "does not execute actions or bypass approval" in promoted.output
    listed = runner.invoke(app, ["playbooks"])
    assert "active" in listed.output
    assert "cannot run, execute or approve anything" in listed.output


def test_promoting_twice_is_refused(isolated: Path) -> None:
    candidate, _ = _promoted_playbook()

    again = runner.invoke(app, ["playbook", "candidate", "promote", candidate])

    assert again.exit_code == 1
    assert "cannot become" in again.output


# ------------------------------------------------------------------ candidate reject


def test_rejecting_keeps_the_candidate_and_creates_nothing(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())

    rejected = runner.invoke(app, ["playbook", "candidate", "reject", candidate])

    assert rejected.exit_code == 0, rejected.output
    assert "rejected" in rejected.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output
    assert candidate in runner.invoke(app, ["playbook", "candidates", "--all"]).output
    assert candidate not in runner.invoke(app, ["playbook", "candidates"]).output


def test_a_rejected_candidate_cannot_be_promoted(isolated: Path) -> None:
    candidate = _add_candidate(_executed_action())
    runner.invoke(app, ["playbook", "candidate", "test", candidate])
    runner.invoke(app, ["playbook", "candidate", "reject", candidate])

    promoted = runner.invoke(app, ["playbook", "candidate", "promote", candidate])

    assert promoted.exit_code == 1
    assert "cannot become" in promoted.output


# --------------------------------------------------------------------------- playbooks


def test_playbook_show_prints_the_whole_provenance(isolated: Path) -> None:
    _, playbook = _promoted_playbook()

    shown = runner.invoke(app, ["playbook", "show", playbook])

    assert shown.exit_code == 0, shown.output
    assert "Source action" in shown.output
    assert "Source execution" in shown.output
    assert "Source fingerprint" in shown.output
    assert "Validated replay contract version" in shown.output
    assert "Promotion test" in shown.output
    assert "reviewed reference blueprint" in shown.output


def test_retiring_hides_it_from_the_default_list_but_keeps_it(
    isolated: Path,
) -> None:
    _, playbook = _promoted_playbook()

    retired = runner.invoke(app, ["playbook", "retire", playbook])

    assert retired.exit_code == 0, retired.output
    assert "retired" in retired.output
    assert "nothing was deleted" in retired.output
    assert "no playbooks" in runner.invoke(app, ["playbooks"]).output
    history = runner.invoke(app, ["playbooks", "--all"])
    assert "retired" in history.output
    assert runner.invoke(app, ["playbook", "show", playbook]).exit_code == 0


def test_retiring_twice_is_refused(isolated: Path) -> None:
    _, playbook = _promoted_playbook()
    runner.invoke(app, ["playbook", "retire", playbook])

    again = runner.invoke(app, ["playbook", "retire", playbook])

    assert again.exit_code == 1
    assert "cannot become" in again.output


def test_an_unknown_playbook_prefix_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["playbook", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


# ------------------------------------------------------------- absent conveniences


@pytest.mark.parametrize(
    "arguments",
    [
        ["playbook", "run", "x"],
        ["playbook", "execute", "x"],
        ["playbook", "apply", "x"],
        ["playbook", "instantiate", "x"],
        ["playbook", "candidate", "confirm", "x"],
        ["playbook", "candidate", "rename", "x", "new name"],
        ["playbook", "candidate", "promote", "x", "--force"],
        ["playbook", "candidate", "promote", "x", "--skip-test"],
        ["playbook", "candidate", "add", "x", "--name", "n", "--note", "y", "--auto"],
    ],
)
def test_the_commands_that_would_skip_review_do_not_exist(
    isolated: Path, arguments: list[str]
) -> None:
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0, arguments
