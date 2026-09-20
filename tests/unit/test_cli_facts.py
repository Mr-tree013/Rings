"""`pw fact`, `pw facts`, `pw correction`, `pw corrections` (ADR-0027).

The command line is the *only* way a candidate is created and the only way one is confirmed, so
these tests check both halves of that sentence: the workflow works, and the flags that would let a
person promote something they did not read — a bulk confirm, an edited value, a forced
confirmation — do not exist.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app
from tests.support.fakes import FakeClock

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


def _propose(
    key: str = "profile.office",
    value: str = "Room 302",
    note: str = "My office is Room 302",
    *extra: str,
) -> str:
    result = runner.invoke(
        app, ["fact", "candidate", "add", key, value, "--note", note, *extra]
    )
    assert result.exit_code == 0, result.output
    return _prefix(result.output)


# -------------------------------------------------------------------- corrections


def test_corrections_start_empty_and_can_be_added(isolated: Path) -> None:
    empty = runner.invoke(app, ["corrections"])
    assert empty.exit_code == 0, empty.output
    assert "no corrections" in empty.output

    recorded = runner.invoke(app, ["correction", "add", "The deadline is the 23rd."])

    assert recorded.exit_code == 0, recorded.output
    assert "recorded" in recorded.output
    listed = runner.invoke(app, ["corrections"])
    assert "The deadline is the 23rd." in listed.output


def test_a_correction_can_be_shown_by_prefix(isolated: Path) -> None:
    runner.invoke(app, ["correction", "add", "I read mail on the phone."])
    prefix = _prefix(runner.invoke(app, ["corrections"]).output)

    shown = runner.invoke(app, ["correction", "show", prefix])

    assert shown.exit_code == 0, shown.output
    assert "I read mail on the phone." in shown.output


def test_an_unknown_correction_prefix_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["correction", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_a_blank_correction_is_refused(isolated: Path) -> None:
    result = runner.invoke(app, ["correction", "add", "   "])

    assert result.exit_code == 1
    assert "blank" in result.output


# --------------------------------------------------------------------- proposals


def test_a_candidate_is_created_with_its_note_and_is_not_a_fact(isolated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fact",
            "candidate",
            "add",
            "profile.office",
            "Room 302",
            "--note",
            "My office is Room 302 in the SE building",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "profile.office" in result.output
    assert "Room 302" in result.output
    assert "Status: pending" in result.output
    assert "not confirmed and will not be used automatically" in result.output
    # The note is stored as the provenance, and the fact list stays empty.
    assert "no facts" in runner.invoke(app, ["facts"]).output


def test_the_note_is_mandatory(isolated: Path) -> None:
    missing = runner.invoke(
        app, ["fact", "candidate", "add", "profile.office", "Room 302"]
    )
    blank = runner.invoke(
        app,
        ["fact", "candidate", "add", "profile.office", "Room 302", "--note", "   "],
    )

    assert missing.exit_code != 0
    assert blank.exit_code == 1
    assert "must carry the correction" in blank.output


def test_a_credential_like_key_is_refused_by_the_cli(isolated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fact",
            "candidate",
            "add",
            "profile.password",
            "hunter2",
            "--note",
            "this should never be stored",
        ],
    )

    assert result.exit_code == 1
    assert "credential store" in result.output
    assert "no candidates" in runner.invoke(app, ["fact", "candidates"]).output


def test_an_expiry_must_carry_an_offset(isolated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fact",
            "candidate",
            "add",
            "profile.office",
            "Room 302",
            "--note",
            "my office",
            "--valid-until",
            "2026-12-31T00:00:00",
        ],
    )

    assert result.exit_code == 1
    assert "timezone offset" in result.output


def test_candidates_can_be_listed_pending_or_historically(isolated: Path) -> None:
    pending = _propose(value="Room 302")
    rejected = _propose(value="Room 320", note="an old office")
    assert runner.invoke(app, ["fact", "candidate", "reject", rejected]).exit_code == 0

    default = runner.invoke(app, ["fact", "candidates"])
    everything = runner.invoke(app, ["fact", "candidates", "--all"])

    assert pending in default.output
    assert rejected not in default.output
    assert pending in everything.output and rejected in everything.output
    assert "not a fact" in default.output


def test_candidate_show_prints_the_users_own_words(isolated: Path) -> None:
    prefix = _propose(note="My office moved to Room 302 this term")

    shown = runner.invoke(app, ["fact", "candidate", "show", prefix])

    assert shown.exit_code == 0, shown.output
    assert "Room 302" in shown.output
    assert "pending" in shown.output
    assert "My office moved to Room 302 this term" in shown.output
    assert "not a confirmed fact" in shown.output


# -------------------------------------------------------------------- confirmation


def test_confirming_creates_a_fact_and_reports_no_previous_value(isolated: Path) -> None:
    prefix = _propose()

    confirmed = runner.invoke(app, ["fact", "candidate", "confirm", prefix])

    assert confirmed.exit_code == 0, confirmed.output
    assert "Confirmed fact" in confirmed.output
    assert "Key: profile.office" in confirmed.output
    assert "Value: Room 302" in confirmed.output
    assert "Previous current value:" in confirmed.output
    assert "none" in confirmed.output
    assert "not injected into models, mail drafts or eHall forms" in confirmed.output


def test_confirming_a_new_value_reports_the_superseded_one(isolated: Path) -> None:
    """The §28 example, through the command line: 302 becomes 320, and 302 stays on the record."""
    first = _propose(value="Room 302", note="my office is 302")
    runner.invoke(app, ["fact", "candidate", "confirm", first])
    second = _propose(value="Room 320", note="I moved to 320")

    confirmed = runner.invoke(app, ["fact", "candidate", "confirm", second])

    assert confirmed.exit_code == 0, confirmed.output
    assert "Room 320" in confirmed.output
    assert "Room 302" in confirmed.output
    assert "retained in history" in confirmed.output
    listing = runner.invoke(app, ["facts"])
    assert "Room 320" in listing.output
    assert "Room 302" not in listing.output  # superseded history is not active
    everything = runner.invoke(app, ["facts", "--all"])
    assert "Room 302" in everything.output
    assert "superseded" in everything.output


def test_a_candidate_cannot_be_confirmed_twice(isolated: Path) -> None:
    prefix = _propose()
    assert runner.invoke(app, ["fact", "candidate", "confirm", prefix]).exit_code == 0

    again = runner.invoke(app, ["fact", "candidate", "confirm", prefix])

    assert again.exit_code == 1
    assert "cannot become" in again.output


def test_rejecting_keeps_the_candidate_and_adds_no_fact(isolated: Path) -> None:
    prefix = _propose()

    rejected = runner.invoke(app, ["fact", "candidate", "reject", prefix])

    assert rejected.exit_code == 0, rejected.output
    assert "rejected" in rejected.output
    assert "no facts" in runner.invoke(app, ["facts"]).output
    assert prefix in runner.invoke(app, ["fact", "candidates", "--all"]).output


def test_a_rejected_candidate_cannot_be_confirmed(isolated: Path) -> None:
    prefix = _propose(value="Room 9")
    runner.invoke(app, ["fact", "candidate", "reject", prefix])

    result = runner.invoke(app, ["fact", "candidate", "confirm", prefix])

    assert result.exit_code == 1
    assert "cannot become" in result.output


# --------------------------------------------------------------------------- facts


def test_facts_and_fact_show_expose_the_provenance(isolated: Path) -> None:
    prefix = _propose(note="My office is Room 302, third floor")
    runner.invoke(app, ["fact", "candidate", "confirm", prefix])
    fact_prefix = _prefix(runner.invoke(app, ["facts"]).output)

    shown = runner.invoke(app, ["fact", "show", fact_prefix])

    assert shown.exit_code == 0, shown.output
    assert "profile.office" in shown.output
    assert "Room 302" in shown.output
    assert "active" in shown.output
    assert "Provenance" in shown.output
    assert "My office is Room 302, third floor" in shown.output


def test_an_expired_fact_is_hidden_by_default_and_visible_with_all(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is a read-time state: no command has to run for it to take effect."""
    clock = FakeClock(start=datetime(2026, 9, 20, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(bootstrap, "system_clock", lambda: clock)
    _propose(
        "profile.course",
        "SE-2026",
        "I am taking SE this term",
        "--valid-until",
        "2026-09-20T13:00:00+00:00",
    )
    prefix = _prefix(runner.invoke(app, ["fact", "candidates"]).output)
    runner.invoke(app, ["fact", "candidate", "confirm", prefix])

    clock.advance(3600)

    assert "no facts" in runner.invoke(app, ["facts"]).output
    history = runner.invoke(app, ["facts", "--all"])
    assert "expired" in history.output
    assert "SE-2026" in history.output


def test_an_unknown_fact_prefix_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["fact", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


# ------------------------------------------------------------- absent conveniences


@pytest.mark.parametrize(
    "arguments",
    [
        ["fact", "candidate", "confirm", "x", "--force"],
        ["fact", "candidate", "confirm", "x", "--edit-value", "other"],
        ["fact", "candidate", "confirm", "--all"],
        ["fact", "delete", "x"],
        ["correction", "delete", "x"],
    ],
)
def test_the_commands_that_must_not_exist_do_not(
    isolated: Path, arguments: list[str]
) -> None:
    """No force, no bulk confirm, no edited value and no delete: history is append-only here."""
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
