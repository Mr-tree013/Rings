"""The mail analysis domain: candidates, categories, kinds and the input fingerprint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from assistant.domain.errors import InvalidMailAnalysis, InvalidMailMessage
from assistant.domain.mail_analysis import (
    ANALYZER_VERSION,
    MailActionCandidate,
    MailAnalysis,
    MailCategory,
    MailLinkStatus,
    MailTemporalKind,
    MailThread,
    MailThreadMember,
    MailThreadSummary,
    mail_analysis_input_fingerprint,
    new_mail_thread_id,
    parse_action_candidates,
    validate_thread_account,
)

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _analysis(**overrides: object) -> MailAnalysis:
    values: dict[str, object] = {
        "message_id": uuid4(),
        "analyzer_version": ANALYZER_VERSION,
        "input_fingerprint": FINGERPRINT,
        "category": MailCategory.ORDINARY_CORRESPONDENCE,
        "requires_reply": False,
        "summary": "A short note.",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return MailAnalysis(**values)  # type: ignore[arg-type]


def test_an_analysis_carries_its_category_verdict_and_candidates() -> None:
    analysis = _analysis(
        category=MailCategory.ACTIONABLE_NOTICE,
        requires_reply=True,
        action_candidates=(
            MailActionCandidate(text="Register for the course"),
            MailActionCandidate(text="Submit the form", time_text="by Friday"),
        ),
    )

    assert analysis.category is MailCategory.ACTIONABLE_NOTICE
    assert analysis.requires_reply is True
    assert len(analysis.action_candidates) == 2
    assert analysis.candidates_with_temporal_kind == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"analyzer_version": 0},
        {"input_fingerprint": "short"},
        {"input_fingerprint": "A" * 64},
        {"summary": "   "},
        {"summary": "x" * 801},
        {"created_at": datetime(2026, 9, 21, 0, 0)},
        {"updated_at": datetime(2026, 9, 21, 0, 0)},
        {"action_candidates": tuple(MailActionCandidate(text=f"item {i}") for i in range(11))},
    ],
)
def test_an_analysis_refuses_values_that_break_its_invariants(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(InvalidMailAnalysis):
        _analysis(**overrides)


def test_candidate_text_is_trimmed_and_bounded() -> None:
    candidate = MailActionCandidate(text="  do the thing  ")

    assert candidate.text == "do the thing"
    assert candidate.temporal_kind is MailTemporalKind.NONE
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(text="   ")
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(text="x" * 501)


def test_time_text_is_collapsed_and_blank_becomes_absent() -> None:
    assert MailActionCandidate(text="x", time_text="  by   Friday ").time_text == "by Friday"
    assert MailActionCandidate(text="x", time_text="   ").time_text is None
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(text="x", time_text="y" * 201)


def test_deadline_and_event_start_are_different_candidates() -> None:
    """The two kinds never collapse into one date field."""
    deadline = MailActionCandidate(
        text="Register", temporal_kind=MailTemporalKind.DEADLINE, time_text="Oct 20"
    )
    event_start = MailActionCandidate(
        text="Workshop", temporal_kind=MailTemporalKind.EVENT_START, time_text="Oct 25"
    )
    analysis = _analysis(
        category=MailCategory.ACTIONABLE_NOTICE,
        action_candidates=(deadline, event_start),
    )

    kinds = [item.temporal_kind for item in analysis.candidates_with_temporal_kind]
    assert kinds == [MailTemporalKind.DEADLINE, MailTemporalKind.EVENT_START]
    assert deadline != event_start


def test_a_timed_candidate_needs_evidence_of_its_time() -> None:
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(text="Register", temporal_kind=MailTemporalKind.DEADLINE)


def test_an_interpreted_instant_must_be_aware() -> None:
    aware = datetime(2026, 10, 20, 23, 59, tzinfo=timezone(timedelta(hours=8)))
    candidate = MailActionCandidate(
        text="Register",
        temporal_kind=MailTemporalKind.DEADLINE,
        interpreted_at=aware,
    )

    assert candidate.interpreted_at == aware
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(
            text="Register",
            temporal_kind=MailTemporalKind.DEADLINE,
            interpreted_at=datetime(2026, 10, 20, 23, 59),
        )


def test_an_instant_without_a_kind_is_refused() -> None:
    """An instant with no meaning attached would be a fact nobody can use."""
    with pytest.raises(InvalidMailAnalysis):
        MailActionCandidate(text="Register", interpreted_at=NOW)


def test_candidates_round_trip_through_their_stored_form() -> None:
    original = (
        MailActionCandidate(
            text="Register",
            temporal_kind=MailTemporalKind.DEADLINE,
            time_text="Oct 20",
            interpreted_at=datetime(2026, 10, 20, 23, 59, tzinfo=UTC),
        ),
        MailActionCandidate(text="Say hello", temporal_kind=MailTemporalKind.OTHER),
    )

    restored = parse_action_candidates([item.to_payload() for item in original])

    assert restored == original
    with pytest.raises(InvalidMailAnalysis):
        parse_action_candidates({"text": "not an array"})
    with pytest.raises(InvalidMailAnalysis):
        parse_action_candidates([{"temporal_kind": "none"}])


def test_the_fingerprint_is_stable_and_covers_its_inputs() -> None:
    message_id = uuid4()
    thread_id = uuid4()
    base = dict(
        analyzer_version=ANALYZER_VERSION,
        schema_version=1,
        message_id=message_id,
        content_fingerprint=FINGERPRINT,
        thread_message_ids=(thread_id, message_id),
        thread_content_fingerprints=(FINGERPRINT, FINGERPRINT),
        planning_timezone="Asia/Shanghai",
    )

    fingerprint = mail_analysis_input_fingerprint(**base)

    assert fingerprint == mail_analysis_input_fingerprint(**base)
    assert len(fingerprint) == 64
    for changed in (
        {**base, "analyzer_version": ANALYZER_VERSION + 1},
        {**base, "schema_version": 2},
        {**base, "content_fingerprint": "b" * 64},
        {**base, "thread_message_ids": (thread_id,)},
        {**base, "planning_timezone": None},
    ):
        assert mail_analysis_input_fingerprint(**changed) != fingerprint


def test_a_thread_member_cannot_disagree_with_itself() -> None:
    thread_id = new_mail_thread_id()
    message_id = uuid4()

    linked = MailThreadMember(
        message_id=message_id,
        thread_id=thread_id,
        link_status=MailLinkStatus.LINKED,
        parent_message_id=uuid4(),
        linked_at=NOW,
    )
    root = MailThreadMember(
        message_id=message_id,
        thread_id=thread_id,
        link_status=MailLinkStatus.ROOT,
        linked_at=NOW,
    )

    assert linked.parent_message_id is not None
    assert root.parent_message_id is None
    with pytest.raises(InvalidMailAnalysis):
        MailThreadMember(
            message_id=message_id,
            thread_id=thread_id,
            link_status=MailLinkStatus.LINKED,
            linked_at=NOW,
        )
    with pytest.raises(InvalidMailAnalysis):
        MailThreadMember(
            message_id=message_id,
            thread_id=thread_id,
            link_status=MailLinkStatus.UNRESOLVED,
            parent_message_id=uuid4(),
            linked_at=NOW,
        )
    with pytest.raises(InvalidMailAnalysis):
        MailThreadMember(
            message_id=message_id,
            thread_id=thread_id,
            link_status=MailLinkStatus.ROOT,
            linked_at=datetime(2026, 9, 21, 0, 0),
        )


def test_a_thread_summary_needs_an_addressable_thread() -> None:
    thread = MailThread(account_id="smail", created_at=NOW, updated_at=NOW)

    summary = MailThreadSummary(thread=thread, message_count=2, latest_at=NOW)

    assert summary.subject_preview is None
    with pytest.raises(InvalidMailAnalysis):
        MailThreadSummary(thread=thread, message_count=0, latest_at=NOW)
    with pytest.raises(InvalidMailMessage):
        MailThread(account_id="Not An Id", created_at=NOW, updated_at=NOW)


def test_threads_belong_to_exactly_one_account() -> None:
    validate_thread_account("smail", "smail")
    with pytest.raises(InvalidMailMessage):
        validate_thread_account("smail", "personal")
    with pytest.raises(InvalidMailMessage):
        validate_thread_account("personal", "smail")
