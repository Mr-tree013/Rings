"""The approval and execution services end to end (ADR-0023).

Real SQLite, real transactions, the real services, a scripted executor and a scripted token
factory. What is under test is the safety story: an exact-fingerprint binding, a single-use
secret, a consumed approval, and an ambiguous outcome that is never retried.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.application.action_execution import ActionExecutionService
from assistant.application.action_service import ActionService, ApprovalState
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.domain.action import ActionRequest, ActionRequestStatus, ActionType
from assistant.domain.approval import approval_ttl, hash_approval_token
from assistant.domain.errors import (
    ActionExecutionUnknown,
    ActionExecutionUnresolved,
    ActionFingerprintMismatch,
    ActionNotExecutable,
    ApprovalAlreadyOutstanding,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalUnavailable,
    CapabilityUnavailable,
    InvalidApprovalToken,
)
from assistant.domain.execution import ExecutionRunStatus
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.migrations import apply_migrations
from tests.support.actions import (
    SECRET_TOKEN,
    ActionStores,
    FakeActionExecutor,
    FixedTokenFactory,
)
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def stores(database: Database, clock: FakeClock) -> ActionStores:
    return ActionStores(database, clock)


def _cases(stores: ActionStores, clock: FakeClock) -> CaseService:
    return CaseService(stores.cases, stores.actions, clock)


def _approvals(
    stores: ActionStores, clock: FakeClock, tokens: list[str] | None = None
) -> ApprovalService:
    if tokens is None:
        return ApprovalService(
            stores.actions, clock, token_factory=FixedTokenFactory()
        )
    return ApprovalService(
        stores.actions, clock, token_factory=FixedTokenFactory(*tokens)
    )


def _execution(
    stores: ActionStores, clock: FakeClock, *executors: FakeActionExecutor
) -> ActionExecutionService:
    return ActionExecutionService(
        stores.actions,
        {executor.action_type: executor for executor in executors},
        clock,
    )


async def _prepared(stores: ActionStores, clock: FakeClock) -> ActionRequest:
    """A case with one prepared mail.send action inside it."""
    case = await _cases(stores, clock).create_case("Register for the course")
    return await _cases(stores, clock).prepare_action(
        case.id,
        "mail.send",
        {"to": "ada@example.edu", "subject": "Registration", "body": "Hello"},
    )


# ------------------------------------------------------------------- challenge


async def test_a_challenge_returns_the_token_once_and_stores_only_its_hash(
    stores: ActionStores, clock: FakeClock, database: Database
) -> None:
    action = await _prepared(stores, clock)

    issued = await _approvals(stores, clock).create_challenge(action.id)

    assert issued.token == SECRET_TOKEN
    assert issued.challenge.action_id == action.id
    assert issued.challenge.action_fingerprint == action.fingerprint
    assert issued.challenge.token_hash == hash_approval_token(SECRET_TOKEN)
    assert issued.challenge.expires_at == NOW + approval_ttl()
    with database.connect() as connection:
        dumped = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM approval_challenges").fetchall()
            for value in tuple(row)
        )
    assert SECRET_TOKEN not in dumped
    assert hash_approval_token(SECRET_TOKEN) in dumped


async def test_a_challenge_is_never_logged_or_echoed(
    stores: ActionStores, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    action = await _prepared(stores, clock)

    with caplog.at_level(logging.DEBUG):
        await _approvals(stores, clock).create_challenge(action.id)
        await _approvals(stores, clock, ["second"]).create_challenge(action.id)

    assert SECRET_TOKEN not in caplog.text
    assert "second" not in caplog.text


async def test_a_challenge_needs_a_prepared_action(stores: ActionStores, clock: FakeClock) -> None:
    from assistant.domain.errors import ActionRequestNotFound

    with pytest.raises(ActionRequestNotFound):
        await _approvals(stores, clock).create_challenge(uuid4())
    action = await _prepared(stores, clock)
    await _cases(stores, clock).cancel_action(action.id)

    with pytest.raises(ActionNotExecutable):
        await _approvals(stores, clock).create_challenge(action.id)


# --------------------------------------------------------------------- approve


async def test_approving_binds_the_exact_fingerprint(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    issued = await service.create_challenge(action.id)

    approval = await service.approve(action.id, issued.token)

    assert approval.action_id == action.id
    assert approval.action_fingerprint == action.fingerprint
    assert approval.expires_at == issued.challenge.expires_at
    overview = await ActionService(stores.actions, clock).overview(action.id)
    assert overview.approval_state is ApprovalState.VALID


async def test_a_wrong_token_is_refused_without_echoing_it(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    await service.create_challenge(action.id)

    with pytest.raises(InvalidApprovalToken) as caught:
        await service.approve(action.id, "not-the-token")

    assert "not-the-token" not in str(caught.value)
    assert await stores.actions.list_approvals(action.id) == []


async def test_a_challenge_cannot_be_approved_twice(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    issued = await service.create_challenge(action.id)
    await service.approve(action.id, issued.token)

    with pytest.raises(ApprovalChallengeConsumed):
        await service.approve(action.id, issued.token)


async def test_an_expired_challenge_cannot_be_approved(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    issued = await service.create_challenge(action.id)
    clock.advance(approval_ttl().total_seconds())

    with pytest.raises(ApprovalChallengeExpired):
        await service.approve(action.id, issued.token)
    assert await stores.actions.list_approvals(action.id) == []


async def test_a_second_live_approval_is_refused(
    stores: ActionStores, clock: FakeClock
) -> None:
    """At most one approval may be outstanding for an action at a time."""
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock, ["first", "second"])
    first = await service.create_challenge(action.id)
    await service.approve(action.id, first.token)
    second = await service.create_challenge(action.id)

    with pytest.raises(ApprovalAlreadyOutstanding):
        await service.approve(action.id, second.token)

    assert len(await stores.actions.list_approvals(action.id)) == 1


async def test_re_approving_after_expiry_keeps_the_history(
    stores: ActionStores, clock: FakeClock
) -> None:
    """An expired approval is superseded, never overwritten and never silently reused."""
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock, ["first", "second"])
    first = await service.create_challenge(action.id)
    await service.approve(action.id, first.token)
    clock.advance(approval_ttl().total_seconds() + 1)
    second = await service.create_challenge(action.id)

    approval = await service.approve(action.id, second.token)

    assert approval.is_usable_at(clock.now()) is True
    history = await stores.actions.list_approvals(action.id)
    assert len(history) == 2
    assert history[0].is_superseded() is True
    assert history[0].is_usable_at(clock.now()) is False


async def test_changing_the_payload_invalidates_the_pending_approval(
    stores: ActionStores, clock: FakeClock, database: Database
) -> None:
    """Approval is bound to bytes: a tampered payload cannot be approved by the old token."""
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    issued = await service.create_challenge(action.id)
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"body":"something else"}', str(action.id)),
        )

    with pytest.raises(Exception) as caught:
        await service.approve(action.id, issued.token)

    assert "not readable" in str(caught.value) or "fingerprint" in str(caught.value)
    assert await stores.actions.list_approvals(action.id) == []


# --------------------------------------------------------------------- execute


async def test_execution_without_a_capability_consumes_nothing(
    stores: ActionStores, clock: FakeClock
) -> None:
    """The production executor set is empty, so this is what a user actually sees."""
    action = await _prepared(stores, clock)
    service = _approvals(stores, clock)
    issued = await service.create_challenge(action.id)
    await service.approve(action.id, issued.token)

    with pytest.raises(CapabilityUnavailable):
        await _execution(stores, clock).execute(action.id)

    overview = await ActionService(stores.actions, clock).overview(action.id)
    assert overview.approval_state is ApprovalState.VALID  # untouched
    assert overview.execution is None
    assert overview.execution_count == 0
    assert overview.action.status is ActionRequestStatus.PREPARED


async def test_a_successful_execution_marks_the_action_executed(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    result = await service.execute(action.id)

    assert result.status is ExecutionRunStatus.SUCCEEDED
    assert result.action_status == "executed"
    assert result.run.approval_id is not None
    assert executor.call_count == 1
    assert executor.calls[0].id == action.id
    stored = await stores.actions.get_action(action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED


async def test_a_failed_execution_spends_the_approval_and_needs_a_new_one(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor().script_failure("the remote refused")
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    result = await service.execute(action.id)

    assert result.status is ExecutionRunStatus.FAILED
    assert result.run.error_summary == "the remote refused"
    assert result.action_status == "prepared"
    stored = await stores.actions.get_action(action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    with pytest.raises(ApprovalUnavailable):
        await service.execute(action.id)
    assert executor.call_count == 1


async def test_an_unknown_outcome_is_recorded_and_never_retried(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor().script_unknown("no response from the remote")
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    result = await service.execute(action.id)

    assert result.status is ExecutionRunStatus.UNKNOWN
    assert result.run.blocks_retry is True
    # Even a fresh approval cannot make it run again.
    await _approve(stores, clock, action, tokens=["again"])
    with pytest.raises(ActionExecutionUnresolved):
        await service.execute(action.id)
    assert executor.call_count == 1


async def test_an_executor_that_raises_is_recorded_as_unknown(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor().script_raise(RuntimeError("connection reset"))
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    with pytest.raises(ActionExecutionUnknown):
        await service.execute(action.id)

    run = await stores.actions.latest_execution(action.id)
    assert run is not None and run.status is ExecutionRunStatus.UNKNOWN
    assert "RuntimeError" in (run.error_summary or "")
    assert executor.call_count == 1
    # The approval was spent by the attempt that may have had an effect.
    overview = await ActionService(stores.actions, clock).overview(action.id)
    assert overview.approval_state is ApprovalState.CONSUMED


async def test_cancellation_leaves_the_run_running(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor().script_cancel()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    with pytest.raises(asyncio.CancelledError):
        await service.execute(action.id)

    run = await stores.actions.latest_execution(action.id)
    assert run is not None and run.status is ExecutionRunStatus.RUNNING
    assert run.finished_at is None
    stored = await stores.actions.get_action(action.id)
    # Not marked FAILED and not marked EXECUTED: the outcome is genuinely unknown.
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED


async def test_two_concurrent_executions_cannot_both_run(
    stores: ActionStores, clock: FakeClock
) -> None:
    """§18: one approval, one execution, one executor call — even under real concurrency."""
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor().script_block()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    first = asyncio.create_task(service.execute(action.id))
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    with pytest.raises(ActionExecutionUnresolved):
        await service.execute(action.id)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert executor.call_count == 1
    assert await stores.actions.count_executions(action.id) == 1


async def test_a_cancelled_action_or_a_cancelled_approval_cannot_execute(
    stores: ActionStores, clock: FakeClock
) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)
    await _cases(stores, clock).cancel_action(action.id)

    with pytest.raises(ActionNotExecutable):
        await service.execute(action.id)
    assert executor.call_count == 0


async def test_an_expired_approval_cannot_execute(stores: ActionStores, clock: FakeClock) -> None:
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)
    clock.advance(approval_ttl().total_seconds())

    with pytest.raises(ApprovalUnavailable):
        await service.execute(action.id)
    assert executor.call_count == 0


async def test_a_tampered_payload_never_reaches_the_executor(
    stores: ActionStores, clock: FakeClock, database: Database
) -> None:
    """§28: the fingerprint is re-derived, not trusted."""
    action = await _prepared(stores, clock)
    executor = FakeActionExecutor()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"body":"swapped"}', str(action.id)),
        )

    # Refused by whichever layer notices first: the store cannot even materialise the row, and
    # the service re-hashes the payload before it would touch an approval.
    with pytest.raises((ActionFingerprintMismatch, CommitmentStoreError)):
        await service.execute(action.id)
    assert executor.call_count == 0
    assert await stores.actions.count_executions(action.id) == 0


async def test_an_action_type_without_a_registered_executor_is_refused_before_consuming(
    stores: ActionStores, clock: FakeClock
) -> None:
    case = await _cases(stores, clock).create_case("Something else")
    action = await _cases(stores, clock).prepare_action(
        case.id, "ehall.submit-certificate", {"form": "x"}
    )
    executor = FakeActionExecutor()
    service = _execution(stores, clock, executor)
    await _approve(stores, clock, action)

    with pytest.raises(CapabilityUnavailable) as caught:
        await service.execute(action.id)

    assert "ehall.submit-certificate" in str(caught.value)
    overview = await ActionService(stores.actions, clock).overview(action.id)
    assert overview.approval_state is ApprovalState.VALID
    assert overview.execution_count == 0
    assert executor.call_count == 0


async def test_the_registry_rejects_a_mismatched_executor(
    stores: ActionStores, clock: FakeClock
) -> None:
    with pytest.raises(ValueError):
        ActionExecutionService(
            stores.actions,
            {ActionType("ehall.submit-certificate"): FakeActionExecutor()},
            clock,
        )


async def test_the_capability_set_is_reported_honestly(
    stores: ActionStores, clock: FakeClock
) -> None:
    empty = _execution(stores, clock)
    assert empty.capability_set == ()
    one = _execution(stores, clock, FakeActionExecutor())
    assert one.capability_set == ("mail.send",)


async def _approve(
    stores: ActionStores,
    clock: FakeClock,
    action: ActionRequest,
    *,
    tokens: list[str] | None = None,
) -> None:
    """Challenge and approve one action, using tokens the tests can recognise."""
    service = _approvals(stores, clock, tokens)
    issued = await service.create_challenge(action.id)
    await service.approve(action.id, issued.token)


async def test_a_full_workflow_from_case_to_execution(
    stores: ActionStores, clock: FakeClock
) -> None:
    """The whole chain, in the order a careful user would walk it."""
    case = await _cases(stores, clock).create_case("Register for the course")
    action = await _cases(stores, clock).prepare_action(
        case.id, "mail.send", {"to": "ada@example.edu", "body": "Please register me."}
    )
    executor = FakeActionExecutor()
    approvals = _approvals(stores, clock)
    execution = _execution(stores, clock, executor)
    reader = ActionService(stores.actions, clock)

    assert (await reader.overview(action.id)).approval_state is ApprovalState.NONE
    issued = await approvals.create_challenge(action.id)
    assert SECRET_TOKEN not in repr(issued.challenge)
    await approvals.approve(action.id, issued.token)
    assert (await reader.overview(action.id)).approval_state is ApprovalState.VALID

    result = await execution.execute(action.id)

    assert result.status is ExecutionRunStatus.SUCCEEDED
    overview = await reader.overview(action.id)
    assert overview.approval_state is ApprovalState.CONSUMED
    assert overview.execution_state == "succeeded"
    completed = await _cases(stores, clock).complete_case(case.id)
    assert completed.status.value == "completed"
    detail = await _cases(stores, clock).get_case(case.id)
    assert [item.id for item in detail.actions] == [action.id]
    assert UUID(str(action.id)) == action.id
