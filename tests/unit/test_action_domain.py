"""The case, action, approval and execution value objects (ADR-0023)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.action import (
    ActionRequest,
    ActionRequestStatus,
    ActionType,
    action_payload_fingerprint,
    canonical_action_payload,
    fingerprint_of_canonical_json,
)
from assistant.domain.approval import (
    ApprovalChallenge,
    ApprovalRecord,
    approval_ttl,
    hash_approval_token,
)
from assistant.domain.case import CASE_TITLE_MAX_LENGTH, Case, CaseStatus
from assistant.domain.errors import (
    InvalidActionPayload,
    InvalidActionRequest,
    InvalidApproval,
    InvalidCase,
    InvalidCaseTransition,
    InvalidExecutionRun,
)
from assistant.domain.execution import (
    ExecutionOutcome,
    ExecutionRun,
    ExecutionRunStatus,
)

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- cases


def test_a_case_opens_once_and_closes_once() -> None:
    case = Case(title="  Register for the course  ", created_at=NOW, updated_at=NOW)

    assert case.title == "Register for the course"
    assert case.status is CaseStatus.OPEN
    assert case.is_open is True
    completed = case.complete(NOW + timedelta(minutes=1))
    assert completed.status is CaseStatus.COMPLETED
    assert completed.completed_at == NOW + timedelta(minutes=1)
    with pytest.raises(InvalidCaseTransition):
        completed.cancel(NOW + timedelta(minutes=2))
    with pytest.raises(InvalidCaseTransition):
        completed.complete(NOW + timedelta(minutes=2))


def test_a_case_refuses_broken_values() -> None:
    with pytest.raises(InvalidCase):
        Case(title="   ", created_at=NOW, updated_at=NOW)
    with pytest.raises(InvalidCase):
        Case(title="x" * (CASE_TITLE_MAX_LENGTH + 1), created_at=NOW, updated_at=NOW)
    with pytest.raises(InvalidCase):
        Case(title="ok", created_at=datetime(2026, 9, 23, 9, 0), updated_at=NOW)
    with pytest.raises(InvalidCase):
        Case(
            title="ok",
            created_at=NOW,
            updated_at=NOW,
            status=CaseStatus.COMPLETED,
        )
    with pytest.raises(InvalidCase):
        Case(
            title="ok",
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )


# ------------------------------------------------------------------------- actions


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"b": 1, "a": 2}, '{"a":2,"b":1}'),
        ({"nested": {"y": [1, 2], "x": None}}, '{"nested":{"x":null,"y":[1,2]}}'),
        ({"text": "\u4e2d\u6587", "flag": True}, '{"flag":true,"text":"\u4e2d\u6587"}'),
        ([3, 2, 1], "[3,2,1]"),
        (1.5, "1.5"),
    ],
)
def test_canonical_payloads_are_order_and_whitespace_independent(
    payload: object, expected: str
) -> None:
    assert canonical_action_payload(payload) == expected
    # The tuple form canonicalises the same way a list does.
    if isinstance(payload, list):
        assert canonical_action_payload(tuple(payload)) == expected


def test_key_order_does_not_change_the_fingerprint() -> None:
    first = action_payload_fingerprint({"to": "a@example.edu", "subject": "hi"})
    second = action_payload_fingerprint({"subject": "hi", "to": "a@example.edu"})

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert action_payload_fingerprint({"to": "b@example.edu", "subject": "hi"}) != first


@pytest.mark.parametrize(
    "payload",
    [
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {"value": b"bytes"},
        {"value": bytearray(b"bytes")},
        {"value": NOW},
        {"value": object()},
        {"value": {1, 2}},
        {1: "non-string key"},
        {"value": {"deeper": b"bytes"}},
    ],
)
def test_only_json_data_can_be_a_payload(payload: object) -> None:
    with pytest.raises(InvalidActionPayload):
        canonical_action_payload(payload)


def test_a_prepared_action_is_immutable_and_fingerprinted() -> None:
    case_id = uuid4()
    action = ActionRequest.prepare(
        case_id=case_id,
        action_type="mail.send",
        payload={"to": "ada@example.edu", "body": "hello"},
        at=NOW,
    )

    assert action.status is ActionRequestStatus.PREPARED
    assert action.is_prepared is True
    assert action.fingerprint == fingerprint_of_canonical_json(action.payload_json)
    assert action.fingerprint_matches() is True
    assert action.payload == {"to": "ada@example.edu", "body": "hello"}
    assert action.action_type.namespace == "mail"
    # There is no edit path at all: the dataclass is frozen and exposes no mutator.
    with pytest.raises(FrozenInstanceError):
        action.payload_json = "{}"  # type: ignore[misc]
    for name in ("update_payload", "set_payload", "edit", "with_payload"):
        assert not hasattr(action, name), name


def test_a_changed_payload_is_a_different_action() -> None:
    case_id = uuid4()
    first = ActionRequest.prepare(
        case_id=case_id, action_type="mail.send", payload={"body": "A"}, at=NOW
    )
    second = ActionRequest.prepare(
        case_id=case_id, action_type="mail.send", payload={"body": "B"}, at=NOW
    )

    assert first.fingerprint != second.fingerprint


def test_a_tampered_payload_cannot_even_be_loaded() -> None:
    """The invariant is checked on construction, so a mismatched row never becomes an object."""
    case_id = uuid4()
    original = ActionRequest.prepare(
        case_id=case_id, action_type="mail.send", payload={"body": "A"}, at=NOW
    )
    tampered = canonical_action_payload({"body": "B"})

    with pytest.raises(InvalidActionRequest):
        ActionRequest(
            id=original.id,
            case_id=case_id,
            action_type=original.action_type,
            payload_json=tampered,
            fingerprint=original.fingerprint,
            created_at=NOW,
        )
    with pytest.raises(InvalidActionRequest):
        # Non-canonical text is refused too: it would fingerprint differently once normalised.
        ActionRequest(
            id=original.id,
            case_id=case_id,
            action_type=original.action_type,
            payload_json='{"body": "A"}',
            fingerprint=original.fingerprint,
            created_at=NOW,
        )


@pytest.mark.parametrize(
    "action_type",
    ["mail", "Mail.Send", "mail.", ".send", "mail..send", "mail send", "x" * 200, ""],
)
def test_an_action_type_must_be_a_namespaced_identifier(action_type: str) -> None:
    with pytest.raises(InvalidActionRequest):
        ActionType(action_type)


def test_naming_a_type_grants_no_capability() -> None:
    """Every type validates; none of them implies an executor exists."""
    for name in ("mail.send", "ehall.submit-certificate", "future.anything-at-all"):
        assert ActionType(name).value == name


def test_an_action_moves_to_terminal_states_only_from_prepared() -> None:
    action = ActionRequest.prepare(
        case_id=uuid4(), action_type="mail.send", payload={}, at=NOW
    )

    executed = action.mark_executed(NOW + timedelta(minutes=1))
    assert executed.status is ActionRequestStatus.EXECUTED
    assert executed.executed_at == NOW + timedelta(minutes=1)
    with pytest.raises(InvalidActionRequest):
        executed.cancel(NOW + timedelta(minutes=2))

    cancelled = action.cancel(NOW + timedelta(minutes=1))
    assert cancelled.status is ActionRequestStatus.CANCELLED
    with pytest.raises(InvalidActionRequest):
        cancelled.mark_executed(NOW + timedelta(minutes=2))


# ----------------------------------------------------------------------- approvals


def test_a_token_hash_is_a_sha256_of_the_plaintext() -> None:
    digest = hash_approval_token("SECRET-APPROVAL-TOKEN")

    assert digest == hashlib_sha256("SECRET-APPROVAL-TOKEN")
    assert digest != hash_approval_token("SECRET-APPROVAL-TOKEN ")
    with pytest.raises(InvalidApproval):
        hash_approval_token("")


def hashlib_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_a_challenge_is_single_use_and_expires() -> None:
    challenge = ApprovalChallenge(
        action_id=uuid4(),
        action_fingerprint="a" * 64,
        token_hash=hash_approval_token("token"),
        created_at=NOW,
        expires_at=NOW + approval_ttl(),
    )

    assert approval_ttl().total_seconds() == 600
    assert challenge.is_expired(NOW) is False
    assert challenge.is_expired(challenge.expires_at) is True
    assert challenge.is_consumed() is False
    assert challenge.matches_token("token") is True
    assert challenge.matches_token("other") is False
    assert challenge.matches_token("") is False


def test_an_approval_is_bound_to_one_fingerprint_and_expires() -> None:
    approval = ApprovalRecord(
        action_id=uuid4(),
        action_fingerprint="b" * 64,
        approved_at=NOW,
        expires_at=NOW + approval_ttl(),
    )

    assert approval.matches_fingerprint("b" * 64) is True
    assert approval.matches_fingerprint("c" * 64) is False
    assert approval.is_usable_at(NOW) is True
    assert approval.is_usable_at(approval.expires_at) is False


def test_broken_approval_values_are_refused() -> None:
    with pytest.raises(InvalidApproval):
        ApprovalChallenge(
            action_id=uuid4(),
            action_fingerprint="short",
            token_hash="a" * 64,
            created_at=NOW,
            expires_at=NOW + approval_ttl(),
        )
    with pytest.raises(InvalidApproval):
        ApprovalRecord(
            action_id=uuid4(),
            action_fingerprint="a" * 64,
            approved_at=NOW,
            expires_at=NOW,
        )
    with pytest.raises(InvalidApproval):
        ApprovalRecord(
            action_id=uuid4(),
            action_fingerprint="a" * 64,
            approved_at=datetime(2026, 9, 23, 9, 0),
            expires_at=NOW + approval_ttl(),
        )


# ---------------------------------------------------------------------- executions


def test_an_executor_outcome_can_never_be_running() -> None:
    with pytest.raises(InvalidExecutionRun):
        ExecutionOutcome(status=ExecutionRunStatus.RUNNING)


def test_a_failed_or_unknown_outcome_always_carries_a_reason() -> None:
    failed = ExecutionOutcome(status=ExecutionRunStatus.FAILED)
    unknown = ExecutionOutcome.unknown("no answer")

    assert failed.error_summary is not None
    assert unknown.status is ExecutionRunStatus.UNKNOWN
    assert unknown.error_summary == "no answer"
    assert ExecutionOutcome.succeeded().error_summary is None
    assert ExecutionOutcome.unknown("x" * 2000).error_summary is not None
    assert len(ExecutionOutcome.unknown("x" * 2000).error_summary or "") == 1000


def test_an_execution_run_must_be_consistent_with_its_status() -> None:
    approval_id = uuid4()
    run = ExecutionRun(action_id=uuid4(), approval_id=approval_id, started_at=NOW)

    assert run.is_running is True
    assert run.blocks_retry is True
    with pytest.raises(InvalidExecutionRun):
        ExecutionRun(
            action_id=uuid4(),
            approval_id=approval_id,
            started_at=NOW,
            status=ExecutionRunStatus.SUCCEEDED,
        )
    with pytest.raises(InvalidExecutionRun):
        ExecutionRun(
            action_id=uuid4(),
            approval_id=approval_id,
            started_at=NOW,
            finished_at=NOW,
        )


@pytest.mark.parametrize(
    ("outcome", "blocks_retry"),
    [
        (ExecutionOutcome.succeeded(), False),
        (ExecutionOutcome.failed("no"), False),
        (ExecutionOutcome.unknown("maybe"), True),
    ],
)
def test_finishing_a_run_records_the_outcome(
    outcome: ExecutionOutcome, blocks_retry: bool
) -> None:
    run = ExecutionRun(action_id=uuid4(), approval_id=uuid4(), started_at=NOW)

    finished = run.finish(outcome, at=NOW + timedelta(seconds=5))

    assert finished.status is outcome.status
    assert finished.finished_at == NOW + timedelta(seconds=5)
    assert finished.blocks_retry is blocks_retry
    with pytest.raises(InvalidExecutionRun):
        finished.finish(outcome, at=NOW + timedelta(seconds=6))
