"""The outbound payload, its link, its reconciliation and the derived state (ADR-0024)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.adapters.mail.message_ids import new_rfc_message_id
from assistant.application.mail_send_status import (
    MailDeliveryState,
    derive_delivery_state,
)
from assistant.domain.action import ActionRequest, ActionRequestStatus
from assistant.domain.approval import ApprovalRecord
from assistant.domain.errors import InvalidMailMessage, InvalidMailSend
from assistant.domain.execution import ExecutionOutcome, ExecutionRun, ExecutionRunStatus
from assistant.domain.mail_send import (
    MAIL_SEND_SCHEMA_VERSION,
    MailSendLink,
    MailSendPayload,
    MailSendReconciliation,
    MailSendReconciliationResult,
    validate_rfc_message_id,
)
from tests.support.mail_send import FROM_ADDRESS, SEND_ACCOUNT_ID, TO_ADDRESS

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def _payload(**overrides: object) -> MailSendPayload:
    values: dict[str, object] = {
        "draft_id": uuid4(),
        "draft_version": 1,
        "account_id": SEND_ACCOUNT_ID,
        "from_address": FROM_ADDRESS,
        "to_addresses": (TO_ADDRESS,),
        "subject": "Re:  SE lab   deadline",
        "body_text": "  Dear Ada, Friday works.  ",
        "rfc_message_id": "<abc@example.edu>",
        "date_header": "Tue, 22 Sep 2026 09:00:00 +0000",
    }
    values.update(overrides)
    return MailSendPayload(**values)  # type: ignore[arg-type]


def test_a_payload_normalises_what_it_can_and_keeps_the_rest_exact() -> None:
    payload = _payload()

    assert payload.subject == "Re: SE lab deadline"  # whitespace collapsed
    assert payload.body_text == "Dear Ada, Friday works."  # trimmed
    assert payload.schema_version == MAIL_SEND_SCHEMA_VERSION
    assert payload.in_reply_to_header is None
    assert payload.references == ()


def test_a_payload_round_trips_through_its_stored_form() -> None:
    original = _payload(
        to_addresses=(TO_ADDRESS, "second@example.edu"),
        in_reply_to_header="<original@example.edu>",
        references=("<root@example.edu>", "<original@example.edu>"),
    )

    restored = MailSendPayload.from_payload(original.to_payload())

    assert restored == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"draft_version": 0},
        {"account_id": "Not An Id"},
        {"from_address": "Ada <ada@example.edu>"},
        {"from_address": "not-an-address"},
        {"to_addresses": ()},
        {"to_addresses": ("Ada <ada@example.edu>",)},
        {"subject": "   "},
        {"body_text": "  "},
        {"rfc_message_id": "abc@example.edu"},
        {"rfc_message_id": "<no domain>"},
        {"rfc_message_id": "<a\rb@example.edu>"},
        {"date_header": "  "},
        {"schema_version": 99},
        {"references": tuple(f"<r{index}@example.edu>" for index in range(51))},
        {"references": ("",)},
        {"in_reply_to_header": " "},
    ],
)
def test_a_payload_refuses_what_could_not_be_sent(overrides: dict[str, object]) -> None:
    # An invalid account id fails through the shared mail identity rule, which is the same
    # rejection by a different (and equally specific) error type.
    with pytest.raises((InvalidMailSend, InvalidMailMessage)):
        _payload(**overrides)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 1},
        "not an object",
        {"schema_version": "1"},
    ],
)
def test_stored_payloads_are_read_strictly(payload: object) -> None:
    with pytest.raises(InvalidMailSend):
        MailSendPayload.from_payload(payload)


def test_an_unknown_field_in_a_stored_payload_is_refused() -> None:
    stored = _payload().to_payload()
    stored["smtp_password"] = "secret"

    with pytest.raises(InvalidMailSend):
        MailSendPayload.from_payload(stored)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<abc@example.edu>", "<abc@example.edu>"),
        ("  <abc@example.edu>  ", "<abc@example.edu>"),
        ("abc@example.edu", None),
        ("<abc>", None),
        ("<a b@example.edu>", None),
        ("<abc@example.edu>\nBcc: evil@example.com", None),
        ("", None),
    ],
)
def test_message_ids_are_validated_strictly(value: str, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(InvalidMailSend):
            validate_rfc_message_id(value)
    else:
        assert validate_rfc_message_id(value) == expected


def test_the_message_id_factory_is_random_and_domain_scoped() -> None:
    first = new_rfc_message_id("example.edu")
    second = new_rfc_message_id("example.edu")

    assert first != second
    assert first.startswith("<") and first.endswith("@example.edu>")
    assert len(first) > 32  # 128 bits of randomness plus the domain
    with pytest.raises(ValueError):
        new_rfc_message_id("")


def test_a_link_binds_one_action_to_one_draft_version() -> None:
    link = MailSendLink(
        action_id=uuid4(),
        draft_id=uuid4(),
        draft_version=3,
        rfc_message_id="<abc@example.edu>",
        created_at=NOW,
    )

    assert link.draft_version == 3
    with pytest.raises(InvalidMailSend):
        replace(link, draft_version=0)
    with pytest.raises(InvalidMailSend):
        replace(link, rfc_message_id="nonsense")
    with pytest.raises(InvalidMailSend):
        replace(link, created_at=datetime(2026, 9, 22, 9, 0))


def test_only_a_found_reconciliation_names_a_location() -> None:
    found = MailSendReconciliation(
        action_id=uuid4(),
        execution_run_id=uuid4(),
        result=MailSendReconciliationResult.FOUND,
        checked_at=NOW,
        mailbox_name="Sent",
        uidvalidity=7,
        uid=42,
    )
    not_found = MailSendReconciliation(
        action_id=uuid4(),
        execution_run_id=uuid4(),
        result=MailSendReconciliationResult.NOT_FOUND,
        checked_at=NOW,
    )

    assert found.uid == 42
    assert not_found.mailbox_name is None
    with pytest.raises(InvalidMailSend):
        MailSendReconciliation(
            action_id=uuid4(),
            execution_run_id=uuid4(),
            result=MailSendReconciliationResult.FOUND,
            checked_at=NOW,
        )
    with pytest.raises(InvalidMailSend):
        MailSendReconciliation(
            action_id=uuid4(),
            execution_run_id=uuid4(),
            result=MailSendReconciliationResult.AMBIGUOUS,
            checked_at=NOW,
            mailbox_name="Sent",
            uid=1,
        )
    with pytest.raises(InvalidMailSend):
        MailSendReconciliation(
            action_id=uuid4(),
            execution_run_id=uuid4(),
            result=MailSendReconciliationResult.NOT_FOUND,
            checked_at=datetime(2026, 9, 22, 9, 0),
        )


def _action(status: ActionRequestStatus = ActionRequestStatus.PREPARED) -> ActionRequest:
    action = ActionRequest.prepare(
        case_id=uuid4(),
        action_type="mail.send",
        payload=_payload().to_payload(),
        at=NOW,
    )
    if status is ActionRequestStatus.EXECUTED:
        return action.mark_executed(NOW)
    return action


def _approval(*, expired: bool = False) -> ApprovalRecord:
    """An approval that is either live at NOW, or already past its expiry at NOW."""
    if expired:
        return ApprovalRecord(
            action_id=uuid4(),
            action_fingerprint="a" * 64,
            approved_at=NOW - timedelta(minutes=30),
            expires_at=NOW - timedelta(minutes=20),
        )
    return ApprovalRecord(
        action_id=uuid4(),
        action_fingerprint="a" * 64,
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )


def _run(status: ExecutionRunStatus) -> ExecutionRun:
    run = ExecutionRun(action_id=uuid4(), approval_id=uuid4(), started_at=NOW)
    if status is ExecutionRunStatus.RUNNING:
        return run
    outcome = {
        ExecutionRunStatus.SUCCEEDED: ExecutionOutcome.succeeded(),
        ExecutionRunStatus.FAILED: ExecutionOutcome.failed("refused"),
        ExecutionRunStatus.UNKNOWN: ExecutionOutcome.unknown("no answer"),
    }[status]
    return run.finish(outcome, at=NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("action_status", "approval", "run_status", "expected"),
    [
        (ActionRequestStatus.PREPARED, None, None, MailDeliveryState.DRAFT),
        (
            ActionRequestStatus.PREPARED,
            _approval(),
            None,
            MailDeliveryState.APPROVED,
        ),
        (
            ActionRequestStatus.PREPARED,
            _approval(expired=True),
            None,
            MailDeliveryState.DRAFT,
        ),
        (
            ActionRequestStatus.PREPARED,
            _approval(),
            ExecutionRunStatus.RUNNING,
            MailDeliveryState.SENDING_UNKNOWN,
        ),
        (
            ActionRequestStatus.PREPARED,
            _approval(),
            ExecutionRunStatus.UNKNOWN,
            MailDeliveryState.SENDING_UNKNOWN,
        ),
        (
            ActionRequestStatus.PREPARED,
            _approval(),
            ExecutionRunStatus.FAILED,
            MailDeliveryState.FAILED,
        ),
        (
            ActionRequestStatus.PREPARED,
            _approval(),
            ExecutionRunStatus.SUCCEEDED,
            MailDeliveryState.SENT,
        ),
        (ActionRequestStatus.EXECUTED, None, None, MailDeliveryState.SENT),
    ],
)
def test_the_delivery_state_is_derived_from_the_records(
    action_status: ActionRequestStatus,
    approval: ApprovalRecord | None,
    run_status: ExecutionRunStatus | None,
    expected: MailDeliveryState,
) -> None:
    state = derive_delivery_state(
        action=_action(action_status),
        approval=approval,
        run=None if run_status is None else _run(run_status),
        now=NOW,
    )

    assert state is expected
