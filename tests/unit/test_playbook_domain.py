"""Playbook candidates, replay tests and playbooks as values (ADR-0028).

These are the rules that hold before any database is involved: a candidate always names an action
and the run that proved it, a replay result can only fail with a bounded code, and neither a
candidate nor a playbook can be edited after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.action import ActionType
from assistant.domain.errors import (
    InvalidPlaybook,
    InvalidPlaybookCandidate,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookReplayTest,
    InvalidPlaybookTransition,
)
from assistant.domain.playbook import (
    CANDIDATE_NAME_MAX_LENGTH,
    CANDIDATE_NOTE_MAX_LENGTH,
    ISSUE_ACTION_TYPE_MISMATCH,
    ISSUE_PAYLOAD_INVALID,
    Playbook,
    PlaybookCandidate,
    PlaybookCandidateStatus,
    PlaybookReplayTest,
    PlaybookStatus,
    ReplayTestStatus,
    ReplayValidationResult,
    new_playbook_candidate_id,
    new_playbook_id,
    new_playbook_replay_test_id,
    replay_input_fingerprint,
    validate_candidate_name,
    validate_candidate_note,
)

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
MAIL_SEND = ActionType("mail.send")


def _candidate(**overrides: object) -> PlaybookCandidate:
    values: dict[str, object] = {
        "name": "Approved certificate workflow",
        "note": "Ran it once, reviewed the run, keeping the shape.",
        "source_action_id": uuid4(),
        "source_execution_run_id": uuid4(),
        "source_action_type": MAIL_SEND,
        "source_action_fingerprint": FINGERPRINT,
        "created_at": NOW,
    }
    values.update(overrides)
    return PlaybookCandidate(**values)  # type: ignore[arg-type]


def _test(**overrides: object) -> PlaybookReplayTest:
    values: dict[str, object] = {
        "candidate_id": uuid4(),
        "action_type": MAIL_SEND,
        "contract_version": 1,
        "input_fingerprint": "b" * 64,
        "status": ReplayTestStatus.PASSED,
        "tested_at": NOW,
    }
    values.update(overrides)
    return PlaybookReplayTest(**values)  # type: ignore[arg-type]


def _playbook(**overrides: object) -> Playbook:
    values: dict[str, object] = {
        "candidate_id": uuid4(),
        "name": "Approved certificate workflow",
        "note": "Reviewed and tested.",
        "action_type": MAIL_SEND,
        "source_action_id": uuid4(),
        "source_execution_run_id": uuid4(),
        "source_action_fingerprint": FINGERPRINT,
        "replay_contract_version": 1,
        "promoted_from_test_id": uuid4(),
        "created_at": NOW,
    }
    values.update(overrides)
    return Playbook(**values)  # type: ignore[arg-type]


# ------------------------------------------------------------------------ candidates


def test_a_candidate_names_its_source_and_starts_pending() -> None:
    candidate = _candidate()

    assert candidate.status is PlaybookCandidateStatus.PENDING
    assert candidate.is_pending is True
    assert candidate.resolved_at is None
    assert candidate.source_action_fingerprint == FINGERPRINT


def test_a_candidate_needs_a_source_run_as_well_as_an_action() -> None:
    """Provenance is the pair: the immutable action *and* the attempt that succeeded."""
    with pytest.raises(TypeError):
        PlaybookCandidate(  # type: ignore[call-arg]
            name="x",
            note="y",
            source_action_id=uuid4(),
            source_action_type=MAIL_SEND,
            source_action_fingerprint=FINGERPRINT,
            created_at=NOW,
        )


@pytest.mark.parametrize("name", ["", "   ", "\n"])
def test_a_blank_name_is_refused(name: str) -> None:
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(name=name)


@pytest.mark.parametrize("note", ["", "   "])
def test_a_blank_note_is_refused(note: str) -> None:
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(note=note)


def test_names_and_notes_are_bounded() -> None:
    assert validate_candidate_name("x" * CANDIDATE_NAME_MAX_LENGTH)
    assert validate_candidate_note("x" * CANDIDATE_NOTE_MAX_LENGTH)
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(name="x" * (CANDIDATE_NAME_MAX_LENGTH + 1))
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(note="x" * (CANDIDATE_NOTE_MAX_LENGTH + 1))


@pytest.mark.parametrize(
    "fingerprint", ["", "A" * 64, "a" * 63, "a" * 65, "z" * 64, "not-a-fingerprint"]
)
def test_a_candidate_fingerprint_must_be_sha256_hex(fingerprint: str) -> None:
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(source_action_fingerprint=fingerprint)


def test_candidate_timestamps_must_be_aware_and_coherent() -> None:
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(created_at=datetime(2026, 9, 24, 9, 0))
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(status=PlaybookCandidateStatus.PROMOTED)
    with pytest.raises(InvalidPlaybookCandidate):
        _candidate(resolved_at=NOW)  # still pending


def test_a_candidate_is_promoted_or_rejected_exactly_once() -> None:
    candidate = _candidate()
    promoted = candidate.promote(NOW + timedelta(minutes=1))
    rejected = candidate.reject(NOW + timedelta(minutes=1))

    assert promoted.status is PlaybookCandidateStatus.PROMOTED
    assert promoted.resolved_at == NOW + timedelta(minutes=1)
    assert rejected.status is PlaybookCandidateStatus.REJECTED
    for resolved in (promoted, rejected):
        assert resolved.is_pending is False
        with pytest.raises(InvalidPlaybookCandidateTransition):
            resolved.promote(NOW + timedelta(minutes=2))
        with pytest.raises(InvalidPlaybookCandidateTransition):
            resolved.reject(NOW + timedelta(minutes=2))


def test_a_rejected_candidate_can_never_be_promoted() -> None:
    rejected = _candidate().reject(NOW)

    with pytest.raises(InvalidPlaybookCandidateTransition):
        rejected.promote(NOW + timedelta(minutes=1))


def test_candidate_identities_are_unique() -> None:
    assert new_playbook_candidate_id() != new_playbook_candidate_id()


# ----------------------------------------------------------------------- replay results


def test_a_passing_result_carries_no_issues() -> None:
    result = ReplayValidationResult.pass_()

    assert result.passed is True
    assert result.issue_codes == ()
    with pytest.raises(InvalidPlaybookReplayTest):
        ReplayValidationResult(
            status=ReplayTestStatus.PASSED, issue_codes=(ISSUE_PAYLOAD_INVALID,)
        )


def test_a_failing_result_must_say_what_went_wrong() -> None:
    result = ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID)

    assert result.passed is False
    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)
    with pytest.raises(InvalidPlaybookReplayTest):
        ReplayValidationResult(status=ReplayTestStatus.FAILED)


def test_only_bounded_issue_codes_are_allowed() -> None:
    """A validator cannot smuggle a payload excerpt out through its failure message."""
    with pytest.raises(InvalidPlaybookReplayTest) as raised:
        ReplayValidationResult.fail("the body said: Dear Ada, I resign")

    assert "bounded issue codes" in str(raised.value)
    with pytest.raises(InvalidPlaybookReplayTest):
        ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID, ISSUE_PAYLOAD_INVALID)


# ------------------------------------------------------------------------ replay tests


def test_a_replay_test_records_what_was_tested_and_with_which_contract() -> None:
    test = _test()

    assert test.passed is True
    assert test.contract_version == 1
    assert test.action_type == MAIL_SEND


@pytest.mark.parametrize("version", [0, -1])
def test_a_contract_version_must_be_positive(version: int) -> None:
    with pytest.raises(InvalidPlaybookReplayTest):
        _test(contract_version=version)


def test_a_replay_test_fingerprint_must_be_sha256_hex() -> None:
    with pytest.raises(InvalidPlaybookReplayTest):
        _test(input_fingerprint="not-a-fingerprint")


def test_replay_test_identities_are_unique() -> None:
    assert new_playbook_replay_test_id() != new_playbook_replay_test_id()


# --------------------------------------------------------------------------- playbooks


def test_a_playbook_starts_active_and_carries_its_provenance() -> None:
    playbook = _playbook()

    assert playbook.status is PlaybookStatus.ACTIVE
    assert playbook.is_active is True
    assert playbook.retired_at is None
    assert playbook.replay_contract_version == 1


def test_retiring_is_a_one_way_transition() -> None:
    playbook = _playbook()

    retired = playbook.retire(NOW + timedelta(days=1))

    assert retired.status is PlaybookStatus.RETIRED
    assert retired.retired_at == NOW + timedelta(days=1)
    with pytest.raises(InvalidPlaybookTransition):
        retired.retire(NOW + timedelta(days=2))


def test_playbook_status_and_retirement_must_agree() -> None:
    with pytest.raises(InvalidPlaybook):
        _playbook(retired_at=NOW)  # active, but retired
    with pytest.raises(InvalidPlaybook):
        _playbook(status=PlaybookStatus.RETIRED)  # retired, with no timestamp


def test_a_playbook_records_a_positive_contract_version() -> None:
    with pytest.raises(InvalidPlaybook):
        _playbook(replay_contract_version=0)


def test_a_playbook_has_no_edit_path() -> None:
    """The name, the note and the provenance are what a reviewer approved, so they are frozen."""
    playbook = _playbook()

    assert not hasattr(playbook, "rename")
    assert not hasattr(playbook, "edit")
    assert not hasattr(playbook, "instantiate")
    assert not hasattr(playbook, "to_action_payload")
    assert not hasattr(playbook, "to_action_request")


def test_playbook_identities_are_unique() -> None:
    assert new_playbook_id() != new_playbook_id()


# ---------------------------------------------------------------- input fingerprint


def test_the_replay_fingerprint_binds_the_snapshot_and_the_contract() -> None:
    arguments: dict[str, object] = {
        "candidate_id": uuid4(),
        "source_action_id": uuid4(),
        "source_action_fingerprint": FINGERPRINT,
        "source_action_type": MAIL_SEND,
        "validator_action_type": MAIL_SEND,
        "contract_version": 1,
    }
    baseline = replay_input_fingerprint(**arguments)  # type: ignore[arg-type]

    assert len(baseline) == 64
    assert baseline == baseline.lower()
    changed = [
        {**arguments, "candidate_id": uuid4()},
        {**arguments, "source_action_id": uuid4()},
        {**arguments, "source_action_fingerprint": "c" * 64},
        {**arguments, "source_action_type": ActionType("ehall.submit-certificate")},
        {**arguments, "validator_action_type": ActionType("ehall.submit-certificate")},
        {**arguments, "contract_version": 2},
    ]
    for variant in changed:
        assert replay_input_fingerprint(**variant) != baseline  # type: ignore[arg-type]


def test_the_replay_fingerprint_is_stable_for_the_same_inputs() -> None:
    candidate_id, action_id = new_playbook_candidate_id(), uuid4()

    first = replay_input_fingerprint(
        candidate_id=candidate_id,
        source_action_id=action_id,
        source_action_fingerprint=FINGERPRINT,
        source_action_type=MAIL_SEND,
        validator_action_type=MAIL_SEND,
        contract_version=1,
    )
    second = replay_input_fingerprint(
        candidate_id=candidate_id,
        source_action_id=action_id,
        source_action_fingerprint=FINGERPRINT,
        source_action_type=MAIL_SEND,
        validator_action_type=MAIL_SEND,
        contract_version=1,
    )

    assert first == second
    assert ISSUE_ACTION_TYPE_MISMATCH != ISSUE_PAYLOAD_INVALID
