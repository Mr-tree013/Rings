"""The reply-draft domain: recipients, subjects, invariants and the audit fingerprint."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.domain.errors import (
    InvalidMailDraft,
    MailReplyRecipientUnavailable,
)
from assistant.domain.knowledge import SourceSpan
from assistant.domain.mail_draft import (
    MailDraft,
    MailDraftEvidenceIdentity,
    MailDraftOrigin,
    MailDraftSource,
    mail_draft_from_plan,
    mail_draft_input_fingerprint,
    mailbox_address,
    reply_subject,
    resolve_reply_recipients,
    span_from_payload,
    span_to_payload,
)
from assistant.domain.storage import StorageUri
from tests.support.mail_intelligence import build_message

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (None, "Re:"),
        ("", "Re:"),
        ("   ", "Re:"),
        ("SE lab deadline", "Re: SE lab deadline"),
        ("Re: SE lab deadline", "Re: SE lab deadline"),
        ("RE: SE lab deadline", "RE: SE lab deadline"),
        ("re:SE lab deadline", "re:SE lab deadline"),
        ("  SE   lab  ", "Re: SE lab"),
    ],
)
def test_the_reply_subject_is_derived_locally(original: str | None, expected: str) -> None:
    assert reply_subject(original) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ada@example.edu", "ada@example.edu"),
        ("Ada Lovelace <ada@example.edu>", "ada@example.edu"),
        ('"Doe, Jane" <jane@example.edu>', "jane@example.edu"),
        ("<ada@example.edu>", "ada@example.edu"),
        ("not-an-address", None),
        ("two@example.edu, three@example.edu", None),
        ("ada@example.edu\nBcc: evil@example.com", None),
        ("", None),
        ("spaced address@example.edu", None),
        ("ada@", None),
        ("@example.edu", None),
    ],
)
def test_a_mailbox_is_extracted_strictly(value: str, expected: str | None) -> None:
    assert mailbox_address(value) == expected


def test_reply_to_wins_over_from() -> None:
    message = build_message(
        from_address="ada@example.edu",
        reply_to_addresses=("replies@lists.example.edu",),
    )

    assert resolve_reply_recipients(message) == ("replies@lists.example.edu",)


def test_from_is_used_when_there_is_no_reply_to() -> None:
    message = build_message(from_address="Ada <ada@example.edu>", reply_to_addresses=())

    assert resolve_reply_recipients(message) == ("ada@example.edu",)


def test_multiple_reply_to_addresses_keep_their_header_order() -> None:
    message = build_message(
        reply_to_addresses=("first@example.edu", "Second Person <second@example.edu>")
    )

    assert resolve_reply_recipients(message) == ("first@example.edu", "second@example.edu")


def test_a_reply_with_no_usable_address_is_refused_before_anything_else() -> None:
    message = build_message(from_address=None, reply_to_addresses=("not-an-address",))
    with pytest.raises(MailReplyRecipientUnavailable):
        resolve_reply_recipients(message)
    with pytest.raises(MailReplyRecipientUnavailable):
        resolve_reply_recipients(build_message(from_address=None))


def test_the_parser_never_truncates_an_address_into_something_else() -> None:
    """An over-long header is not a usable mailbox, so it is not silently accepted."""
    message = build_message(from_address=f"{'a' * 600}@example.edu")

    with pytest.raises(MailReplyRecipientUnavailable):
        resolve_reply_recipients(message)


def _draft(**overrides: object) -> MailDraft:
    values: dict[str, object] = {
        "account_id": "smail",
        "reply_to_message_id": uuid4(),
        "to_addresses": ("ada@example.edu",),
        "subject": "Re: hello",
        "body_text": "A body.",
        "generation_input_fingerprint": "b" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return MailDraft(**values)  # type: ignore[arg-type]


def test_a_draft_needs_recipients_a_subject_a_body_and_a_version() -> None:
    draft = _draft()

    assert draft.origin is MailDraftOrigin.MODEL_GENERATED
    assert draft.version == 1
    assert draft.is_edited is False
    for overrides in (
        {"to_addresses": ()},
        {"subject": "   "},
        {"body_text": "\n  "},
        {"version": 0},
        {"prompt_version": 0},
        {"generation_input_fingerprint": "short"},
        {"generation_input_fingerprint": "B" * 64},
        {"created_at": datetime(2026, 9, 22, 9, 0)},
        {"updated_at": datetime(2026, 9, 22, 9, 0)},
        {"needs_user_input": ("x" * 501,)},
        {"needs_user_input": ("   ",)},
        {"needs_user_input": tuple(f"q{index}" for index in range(9))},
    ):
        with pytest.raises(InvalidMailDraft):
            _draft(**overrides)


def test_a_draft_has_no_sending_state() -> None:
    """The vocabulary of sending does not exist on the entity."""
    for forbidden in ("sent_at", "approved", "approval", "action_request", "smtp_status"):
        assert not hasattr(_draft(), forbidden), forbidden


def test_open_questions_are_normalised_and_bounded() -> None:
    draft = _draft(needs_user_input=("  What   is your student number?  ",))

    assert draft.needs_user_input == ("What is your student number?",)


def test_a_plan_plus_a_body_makes_a_draft() -> None:
    plan = _draft_plan()
    draft = mail_draft_from_plan(plan, body_text="Hello.", at=NOW)

    assert draft.account_id == plan.account_id
    assert draft.reply_to_message_id == plan.reply_to_message_id
    assert draft.subject == plan.subject
    assert draft.body_text == "Hello."
    assert draft.thread_id == plan.thread_id


def _draft_plan(**overrides: object):
    from assistant.domain.mail_draft import MailDraftPlan

    values: dict[str, object] = {
        "account_id": "smail",
        "reply_to_message_id": uuid4(),
        "to_addresses": ("ada@example.edu",),
        "subject": "Re: hello",
        "generation_input_fingerprint": "c" * 64,
        "thread_id": uuid4(),
    }
    values.update(overrides)
    return MailDraftPlan(**values)  # type: ignore[arg-type]


def test_the_fingerprint_is_stable_and_covers_its_inputs() -> None:
    message_id = uuid4()
    thread_id = uuid4()
    base: dict[str, object] = {
        "prompt_version": 1,
        "schema_version": 1,
        "reply_to_message_id": message_id,
        "to_addresses": ("ada@example.edu",),
        "subject": "Re: hello",
        "thread_message_ids": (thread_id, message_id),
        "thread_content_fingerprints": ("a" * 64, "b" * 64),
        "context_query": None,
        "root_id": None,
        "evidence": (),
    }
    fingerprint = mail_draft_input_fingerprint(**base)  # type: ignore[arg-type]

    assert fingerprint == mail_draft_input_fingerprint(**base)  # type: ignore[arg-type]
    assert len(fingerprint) == 64
    for changed in (
        {**base, "prompt_version": 2},
        {**base, "schema_version": 2},
        {**base, "to_addresses": ("someone@example.edu",)},
        {**base, "subject": "Re: something else"},
        {**base, "context_query": "my office hours"},
        {**base, "thread_message_ids": (message_id,)},
        {
            **base,
            "evidence": (
                MailDraftEvidenceIdentity(
                    root_id="university",
                    chunk_id="c1",
                    logical_uri="vault://university/notes/office-hours.md",
                    source_span="lines 18-31",
                    content_sha256="d" * 64,
                ),
            ),
        },
    ):
        assert mail_draft_input_fingerprint(**changed) != fingerprint  # type: ignore[arg-type]


def test_a_source_round_trips_through_its_stored_form() -> None:
    draft_id = uuid4()
    source = MailDraftSource(
        draft_id=draft_id,
        root_id="university",
        entry_id=uuid4(),
        chunk_id=uuid4(),
        logical_uri=StorageUri.parse("vault://university/notes/hours.md"),
        source_span=SourceSpan.page(4),
        ordinal=1,
    )

    restored = MailDraftSource.from_payload(
        draft_id, source.ordinal, source.root_id, source.to_payload()
    )

    assert restored == source
    assert "hours.md" in str(restored.logical_uri)
    with pytest.raises(InvalidMailDraft):
        MailDraftSource(
            draft_id=draft_id,
            root_id="university",
            entry_id=uuid4(),
            chunk_id=uuid4(),
            logical_uri=StorageUri.parse("vault://other/notes/hours.md"),
            source_span=SourceSpan.page(4),
        )


def test_source_spans_survive_their_json_form() -> None:
    assert span_from_payload(span_to_payload(SourceSpan.page(3))) == SourceSpan.page(3)
    assert span_from_payload(span_to_payload(SourceSpan.lines(4, 9))) == SourceSpan.lines(4, 9)
    with pytest.raises(InvalidMailDraft):
        span_from_payload("page 3")
