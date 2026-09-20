"""Corrections, candidates and confirmed facts as values (ADR-0027).

The rules under test here are the ones that make the phase trustworthy before any SQLite is
involved: a key cannot name a secret, a candidate cannot skip its provenance, a confirmation
copies the proposal instead of editing it, and "active" means current *and* unexpired — computed
from the fact itself, with no scheduler anywhere in sight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.domain.correction import (
    CORRECTION_MAX_LENGTH,
    Correction,
    new_correction_id,
    validate_correction_text,
)
from assistant.domain.errors import (
    ForbiddenFactKey,
    InvalidConfirmedFact,
    InvalidCorrection,
    InvalidFactCandidate,
    InvalidFactCandidateTransition,
    InvalidFactKey,
)
from assistant.domain.fact import (
    FACT_VALUE_MAX_LENGTH,
    ConfirmedFact,
    FactCandidate,
    FactCandidateStatus,
    FactState,
    forbidden_segment,
    new_confirmed_fact_id,
    new_fact_candidate_id,
    validate_fact_key,
    validate_fact_value,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------- corrections


def test_a_correction_is_trimmed_and_kept_as_written() -> None:
    correction = Correction(text="  My office is Room 302.  ", created_at=NOW)

    assert correction.text == "My office is Room 302."


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_a_blank_correction_is_refused(text: str) -> None:
    with pytest.raises(InvalidCorrection):
        Correction(text=text, created_at=NOW)


def test_a_correction_longer_than_the_limit_is_refused() -> None:
    assert validate_correction_text("x" * CORRECTION_MAX_LENGTH)
    with pytest.raises(InvalidCorrection):
        Correction(text="x" * (CORRECTION_MAX_LENGTH + 1), created_at=NOW)


def test_a_correction_needs_an_aware_timestamp() -> None:
    with pytest.raises(InvalidCorrection):
        Correction(text="something", created_at=datetime(2026, 9, 20, 12, 0))


def test_a_correction_preview_is_single_line() -> None:
    correction = Correction(text="first line\nsecond line", created_at=NOW)

    assert correction.preview() == "first line second line"
    assert correction.preview(limit=10).endswith("\u2026")


def test_correction_identities_are_unique() -> None:
    assert new_correction_id() != new_correction_id()


# ------------------------------------------------------------------------------ keys


@pytest.mark.parametrize(
    "key",
    [
        "profile.full_name",
        "profile.student_id",
        "profile.office",
        "preferences.reply_signature",
        "a",
        "a.b-c_d.1",
    ],
)
def test_an_ordinary_fact_key_is_accepted(key: str) -> None:
    assert validate_fact_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "  ",
        "Profile.office",
        "1profile.office",
        ".profile",
        "profile office",
        "profile/office",
        "x" * 129,
    ],
)
def test_a_malformed_fact_key_is_refused(key: str) -> None:
    with pytest.raises(InvalidFactKey):
        validate_fact_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "profile.password",
        "profile.passwd",
        "mail.smtp_token",
        "api_key",
        "profile.api_key",
        "profile.apikey",
        "profile.private_key",
        "profile.secret",
        "profile.credential",
        "profile.credentials",
        "PROFILE.PASSWORD",
    ],
)
def test_a_credential_like_key_is_refused(key: str) -> None:
    with pytest.raises(ForbiddenFactKey):
        validate_fact_key(key)


@pytest.mark.parametrize(
    "key", ["profile.student_id", "profile.office", "preferences.signature"]
)
def test_a_key_that_merely_contains_a_ban_word_is_fine(key: str) -> None:
    """The ban is on whole segments: `tokenizer` is not a token."""
    assert validate_fact_key(key) == key
    assert forbidden_segment("profile.tokenizer") is None
    assert forbidden_segment("profile.password") == "password"


def test_the_secret_ban_names_the_offending_segment() -> None:
    with pytest.raises(ForbiddenFactKey) as raised:
        validate_fact_key("mail.smtp_token")

    assert raised.value.segment == "token"
    assert "credential store" in str(raised.value)


# ------------------------------------------------------------------------ candidates


def _candidate(**overrides: object) -> FactCandidate:
    values: dict[str, object] = {
        "fact_key": "profile.office",
        "value": "Room 302",
        "correction_id": new_correction_id(),
        "created_at": NOW,
    }
    values.update(overrides)
    return FactCandidate(**values)  # type: ignore[arg-type]


def test_a_candidate_starts_pending_and_untrusted() -> None:
    candidate = _candidate()

    assert candidate.status is FactCandidateStatus.PENDING
    assert candidate.is_pending is True
    assert candidate.resolved_at is None


def test_a_candidate_must_reference_the_correction_that_justifies_it() -> None:
    """There is no constructor that omits provenance, and no `None` provenance to write."""
    with pytest.raises(TypeError):
        FactCandidate(  # type: ignore[call-arg]
            fact_key="profile.office", value="Room 302", created_at=NOW
        )


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_a_blank_candidate_value_is_refused(value: str) -> None:
    with pytest.raises(InvalidFactCandidate):
        _candidate(value=value)


def test_a_candidate_value_longer_than_the_limit_is_refused() -> None:
    assert validate_fact_value("x" * FACT_VALUE_MAX_LENGTH)
    with pytest.raises(InvalidFactCandidate):
        _candidate(value="x" * (FACT_VALUE_MAX_LENGTH + 1))


def test_a_proposed_validity_window_must_be_in_the_future() -> None:
    with pytest.raises(InvalidFactCandidate):
        _candidate(proposed_valid_until=NOW)
    with pytest.raises(InvalidFactCandidate):
        _candidate(proposed_valid_until=NOW - timedelta(minutes=1))

    assert _candidate(proposed_valid_until=NOW + timedelta(hours=1))


def test_a_resolved_candidate_must_record_when_it_was_resolved() -> None:
    with pytest.raises(InvalidFactCandidate):
        _candidate(status=FactCandidateStatus.CONFIRMED)
    with pytest.raises(InvalidFactCandidate):
        _candidate(resolved_at=NOW)  # still pending


def test_confirming_and_rejecting_are_single_transitions() -> None:
    candidate = _candidate()
    confirmed = candidate.confirm(NOW + timedelta(minutes=1))
    rejected = candidate.reject(NOW + timedelta(minutes=1))

    assert confirmed.status is FactCandidateStatus.CONFIRMED
    assert confirmed.resolved_at == NOW + timedelta(minutes=1)
    assert rejected.status is FactCandidateStatus.REJECTED
    for resolved in (confirmed, rejected):
        assert resolved.is_pending is False
        with pytest.raises(InvalidFactCandidateTransition):
            resolved.confirm(NOW + timedelta(minutes=2))
        with pytest.raises(InvalidFactCandidateTransition):
            resolved.reject(NOW + timedelta(minutes=2))


def test_a_rejected_candidate_can_never_be_confirmed() -> None:
    rejected = _candidate().reject(NOW)

    with pytest.raises(InvalidFactCandidateTransition):
        rejected.confirm(NOW + timedelta(minutes=1))


def test_candidate_identities_are_unique() -> None:
    assert new_fact_candidate_id() != new_fact_candidate_id()


# --------------------------------------------------------------------------- facts


def _fact(**overrides: object) -> ConfirmedFact:
    values: dict[str, object] = {
        "candidate_id": new_fact_candidate_id(),
        "fact_key": "profile.office",
        "value": "Room 302",
        "valid_from": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    return ConfirmedFact(**values)  # type: ignore[arg-type]


def test_a_fact_without_an_expiry_is_active_forever() -> None:
    fact = _fact()

    assert fact.is_active_at(NOW + timedelta(days=3650)) is True
    assert fact.state_at(NOW) is FactState.ACTIVE


def test_expiry_is_derived_from_the_window() -> None:
    """T active, T+59m active, T+1h expired — with no job and no mutation in between."""
    fact = _fact(valid_until=NOW + timedelta(hours=1))

    assert fact.is_active_at(NOW) is True
    assert fact.state_at(NOW) is FactState.ACTIVE
    assert fact.is_active_at(NOW + timedelta(minutes=59)) is True
    assert fact.is_expired(NOW + timedelta(hours=1)) is True
    assert fact.state_at(NOW + timedelta(hours=1)) is FactState.EXPIRED
    assert fact.is_active_at(NOW + timedelta(hours=1)) is False


def test_supersession_outranks_expiry_in_the_displayed_state() -> None:
    fact = _fact(
        valid_until=NOW + timedelta(hours=1), superseded_at=NOW + timedelta(minutes=30)
    )

    assert fact.state_at(NOW + timedelta(hours=2)) is FactState.SUPERSEDED
    assert fact.is_active_at(NOW) is False


def test_a_validity_window_must_be_ordered() -> None:
    with pytest.raises(InvalidConfirmedFact):
        _fact(valid_until=NOW)
    with pytest.raises(InvalidConfirmedFact):
        _fact(valid_until=NOW - timedelta(seconds=1))


def test_supersession_cannot_precede_creation() -> None:
    with pytest.raises(InvalidConfirmedFact):
        _fact(superseded_at=NOW - timedelta(seconds=1))

    assert _fact(superseded_at=NOW)


def test_a_fact_carries_the_candidate_it_came_from() -> None:
    candidate_id = new_fact_candidate_id()

    assert _fact(candidate_id=candidate_id).candidate_id == candidate_id


def test_fact_identities_are_unique() -> None:
    assert new_confirmed_fact_id() != new_confirmed_fact_id()
