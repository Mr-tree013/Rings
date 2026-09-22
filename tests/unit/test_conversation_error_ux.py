"""The nine refusal states, said differently and without any internal machinery (Phase 11F).

Collapsing every failure into one apology is not a rough edge: it hides which of nine very
different situations the user is in. This module pins the closed vocabulary and the sentences that
carry it, and it pins the other half of the promise — that a normal reply never reprints a
traceback, a schema, a path or a provider message.
"""

from __future__ import annotations

import pytest

from assistant.application import conversation_render as render
from assistant.domain.conversation_errors import (
    ConversationRefusalCode,
    refusal_code_for,
)
from assistant.domain.errors import (
    ActionExecutionUnknown,
    AmbiguousId,
    ApprovalUnavailable,
    CannotCancelSafely,
    CapabilityUnavailable,
    EHallBrowserUnavailable,
    EHallDisabled,
    EHallLoginRequired,
    MailAuthenticationError,
    MailConnectionError,
    MailCredentialsMissing,
    MailSendNotConfigured,
    ModelCredentialsMissing,
    ModelNotConfigured,
    ModelTransientError,
    PlanningNotConfigured,
    StalePlanProposal,
)

EXPECTED_CODES = {
    ConversationRefusalCode.NOT_CONFIGURED,
    ConversationRefusalCode.AUTH_REQUIRED,
    ConversationRefusalCode.CONNECTION_FAILED,
    ConversationRefusalCode.CREDENTIAL_MISSING,
    ConversationRefusalCode.UNSUPPORTED_CAPABILITY,
    ConversationRefusalCode.AMBIGUOUS_REFERENCE,
    ConversationRefusalCode.UNKNOWN_EXTERNAL_RESULT,
    ConversationRefusalCode.STALE_CONFIRMATION,
    ConversationRefusalCode.CANNOT_CANCEL_SAFELY,
}
"""The states a person has to be able to tell apart (ADR-0045 §4)."""

INTERNAL_MARKERS = (
    "traceback",
    "jsonschema",
    "sqlite3",
    "keyerror",
    "assertionerror",
    "model output violates",
    "[ehall] enabled",
)


def test_the_refusal_vocabulary_is_the_reviewed_set() -> None:
    assert set(ConversationRefusalCode) >= EXPECTED_CODES


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EHallDisabled("disabled"), ConversationRefusalCode.NOT_CONFIGURED),
        (ModelNotConfigured("no model"), ConversationRefusalCode.NOT_CONFIGURED),
        (PlanningNotConfigured("no timezone"), ConversationRefusalCode.NOT_CONFIGURED),
        (MailSendNotConfigured("smail", "no smtp"), ConversationRefusalCode.NOT_CONFIGURED),
        (EHallLoginRequired("login"), ConversationRefusalCode.AUTH_REQUIRED),
        (MailAuthenticationError("rejected"), ConversationRefusalCode.AUTH_REQUIRED),
        (MailConnectionError("refused"), ConversationRefusalCode.CONNECTION_FAILED),
        (ModelTransientError("timeout"), ConversationRefusalCode.CONNECTION_FAILED),
        (EHallBrowserUnavailable("no chromium"), ConversationRefusalCode.CONNECTION_FAILED),
        (MailCredentialsMissing("no password"), ConversationRefusalCode.CREDENTIAL_MISSING),
        (ModelCredentialsMissing("no key"), ConversationRefusalCode.CREDENTIAL_MISSING),
        (CapabilityUnavailable("nope"), ConversationRefusalCode.UNSUPPORTED_CAPABILITY),
        (AmbiguousId("ab", 2), ConversationRefusalCode.AMBIGUOUS_REFERENCE),
        (
            ActionExecutionUnknown("unknown"),
            ConversationRefusalCode.UNKNOWN_EXTERNAL_RESULT,
        ),
        (ApprovalUnavailable("spent"), ConversationRefusalCode.STALE_CONFIRMATION),
        (StalePlanProposal("moved on"), ConversationRefusalCode.STALE_CONFIRMATION),
        (CannotCancelSafely("executing"), ConversationRefusalCode.CANNOT_CANCEL_SAFELY),
    ],
)
def test_every_known_failure_maps_to_its_own_state(error, expected) -> None:
    assert refusal_code_for(error) is expected


def test_an_unknown_error_is_not_given_a_made_up_state() -> None:
    assert refusal_code_for(ValueError("something else")) is None


def test_each_state_reads_differently() -> None:
    sentences = {
        code: render.render_refusal(code, subject="这一步") for code in EXPECTED_CODES
    }

    assert len(set(sentences.values())) == len(EXPECTED_CODES)
    for sentence in sentences.values():
        assert sentence.strip()
        for marker in INTERNAL_MARKERS:
            assert marker not in sentence.lower()


def test_a_named_refusal_never_reprints_the_exception_text() -> None:
    text = render.render_operation_failure(
        "ehall.certificate.prepare",
        "the eHall pipeline is disabled; set [ehall] enabled = true to use it",
        refusal=ConversationRefusalCode.NOT_CONFIGURED,
    )

    assert "还没有配置" in text
    assert "[ehall] enabled" not in text
    assert "traceback" not in text.lower()


def test_an_unnamed_failure_is_shown_only_when_it_carries_no_machinery() -> None:
    friendly = render.render_operation_failure(
        "mail.sync", "the mailbox rejected the password"
    )
    internal = render.render_operation_failure(
        "mail.sync", "sqlite3.OperationalError: no such table: mail_messages"
    )

    assert "the mailbox rejected the password" in friendly
    assert "sqlite3" not in internal
    assert "没有完成" in internal


def test_a_refusal_names_the_operation_a_person_asked_for() -> None:
    text = render.render_operation_failure(
        "ehall.certificate.prepare", "ignored", refusal=ConversationRefusalCode.AUTH_REQUIRED
    )

    assert "ehall.certificate.prepare" in text
    assert "登录" in text
