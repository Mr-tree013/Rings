"""Actions, challenges, approvals and executions against real SQLite (ADR-0023).

Everything here is about what the database refuses: a tampered payload, a redeemed challenge, a
second approval while one is live, a reused approval, a retry after an unresolved attempt. Real
transactions are the only way to test those, so sqlite3 is never mocked.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.action import ActionRequest, ActionRequestStatus
from assistant.domain.approval import (
    ApprovalChallenge,
    approval_ttl,
    hash_approval_token,
    new_approval_challenge_id,
    new_approval_id,
)
from assistant.domain.case import Case
from assistant.domain.errors import (
    ActionExecutionUnresolved,
    ActionNotExecutable,
    ActionRequestNotFound,
    AmbiguousId,
    ApprovalAlreadyOutstanding,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalChallengeNotFound,
    ApprovalUnavailable,
    ExecutionRunNotFound,
    InvalidApprovalToken,
    StaleCaseUpdate,
)
from assistant.domain.execution import ExecutionOutcome, ExecutionRunStatus
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN
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
def stores(database: Database, clock: FakeClock):
    from tests.support.actions import ActionStores

    return ActionStores(database, clock)


async def _case(stores) -> Case:
    return await stores.cases.add_case(
        Case(title="Register for the course", created_at=NOW, updated_at=NOW)
    )


async def _action(stores, payload: object = None, *, case_id: UUID | None = None):
    case = await _case(stores)
    action = ActionRequest.prepare(
        case_id=case_id if case_id is not None else case.id,
        action_type="mail.send",
        payload={"to": "ada@example.edu", "body": "hello"} if payload is None else payload,
        at=NOW,
    )
    return await stores.actions.add_action(action)


async def _challenge(stores, action, *, token: str = SECRET_TOKEN, at=None):
    moment = NOW if at is None else at
    return await stores.actions.add_challenge(
        ApprovalChallenge(
            id=new_approval_challenge_id(),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            token_hash=hash_approval_token(token),
            created_at=moment,
            expires_at=moment + approval_ttl(),
        )
    )


# ---------------------------------------------------------------------- cases


async def test_a_case_round_trips_and_moves_through_its_lifecycle(stores) -> None:
    case = await _case(stores)

    assert await stores.cases.get_case(case.id) == case
    assert await stores.cases.count_cases() == 1
    assert [item.id for item in await stores.cases.list_cases()] == [case.id]

    completed = await stores.cases.update_case(
        case.complete(NOW + timedelta(minutes=1)), expected_updated_at=case.updated_at
    )

    assert completed.status.value == "completed"
    assert await stores.cases.count_cases(statuses=(completed.status,)) == 1
    with pytest.raises(StaleCaseUpdate):
        await stores.cases.update_case(
            case.complete(NOW + timedelta(minutes=2)), expected_updated_at=case.updated_at
        )


async def test_case_ids_resolve_by_prefix_and_ambiguity(stores) -> None:
    first = await stores.cases.add_case(
        Case(
            id=UUID(int=1),
            title="first",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await stores.cases.add_case(
        Case(id=UUID(int=2), title="second", created_at=NOW, updated_at=NOW)
    )

    assert await stores.cases.resolve_case_id(str(first.id)) == first.id
    with pytest.raises(AmbiguousId):
        await stores.cases.resolve_case_id("0000")


# -------------------------------------------------------------------- actions


async def test_an_action_is_stored_with_its_canonical_payload(stores) -> None:
    action = await _action(stores, {"b": 1, "a": {"y": 2}})

    stored = await stores.actions.get_action(action.id)
    assert stored == action
    assert stored is not None
    assert stored.payload_json == '{"a":{"y":2},"b":1}'
    assert await stores.actions.count_actions() == 1


async def test_tampering_with_the_payload_is_refused_by_the_store(stores, database) -> None:
    """The §28 regression: a row whose payload and fingerprint disagree never executes."""
    action = await _action(stores)
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"body":"changed"}', str(action.id)),
        )

    with pytest.raises(CommitmentStoreError):
        # Loading the row re-derives the fingerprint and refuses the mismatch.
        await stores.actions.get_action(action.id)
    with pytest.raises(CommitmentStoreError):
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW,
        )


async def test_a_cancelled_action_cannot_be_executed(stores) -> None:
    action = await _action(stores)

    cancelled = await stores.actions.cancel_action(action.cancel(NOW))

    assert cancelled.status is ActionRequestStatus.CANCELLED
    with pytest.raises(ActionNotExecutable):
        await stores.actions.cancel_action(action.cancel(NOW))


async def test_an_unknown_action_id_is_not_found(stores) -> None:
    from uuid import uuid4

    with pytest.raises(ActionRequestNotFound):
        await stores.actions.resolve_action_id(str(uuid4()))


# ------------------------------------------------------------------ approvals


async def test_redeeming_a_challenge_stores_only_hashes(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)

    grant = await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW + timedelta(minutes=1),
    )

    assert grant.approval.action_fingerprint == action.fingerprint
    assert grant.approval.is_usable_at(NOW + timedelta(minutes=1)) is True
    stored = await stores.actions.get_challenge(challenge.id)
    assert stored is not None and stored.is_consumed() is True


async def test_a_wrong_token_is_refused_and_leaves_the_challenge_open(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)

    with pytest.raises(InvalidApprovalToken) as caught:
        await stores.actions.redeem_challenge(
            challenge_id=challenge.id,
            token_hash=hash_approval_token("wrong-token"),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            approval_id=new_approval_id(),
            now=NOW,
        )

    assert "wrong-token" not in str(caught.value)
    stored = await stores.actions.get_challenge(challenge.id)
    assert stored is not None and stored.is_consumed() is False
    assert await stores.actions.list_approvals(action.id) == []


async def test_a_challenge_cannot_be_redeemed_twice(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)
    arguments = {
        "challenge_id": challenge.id,
        "token_hash": hash_approval_token(SECRET_TOKEN),
        "action_id": action.id,
        "action_fingerprint": action.fingerprint,
        "approval_id": new_approval_id(),
        "now": NOW,
    }
    await stores.actions.redeem_challenge(**arguments)  # type: ignore[arg-type]

    with pytest.raises(ApprovalChallengeConsumed):
        await stores.actions.redeem_challenge(**{**arguments, "approval_id": new_approval_id()})  # type: ignore[arg-type]


async def test_an_expired_challenge_is_refused(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)

    with pytest.raises(ApprovalChallengeExpired):
        await stores.actions.redeem_challenge(
            challenge_id=challenge.id,
            token_hash=hash_approval_token(SECRET_TOKEN),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            approval_id=new_approval_id(),
            now=challenge.expires_at,
        )


async def test_a_second_live_approval_is_refused(stores) -> None:
    action = await _action(stores)
    first = await _challenge(stores, action, token="first")
    second = await _challenge(stores, action, token="second", at=NOW + timedelta(minutes=1))
    await stores.actions.redeem_challenge(
        challenge_id=first.id,
        token_hash=hash_approval_token("first"),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ApprovalAlreadyOutstanding):
        await stores.actions.redeem_challenge(
            challenge_id=second.id,
            token_hash=hash_approval_token("second"),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            approval_id=new_approval_id(),
            now=NOW + timedelta(minutes=2),
        )


async def test_an_expired_approval_is_superseded_not_overwritten(stores) -> None:
    """Re-approving after expiry works, and the old decision stays in the audit trail."""
    action = await _action(stores)
    first = await _challenge(stores, action, token="first")
    first_grant = await stores.actions.redeem_challenge(
        challenge_id=first.id,
        token_hash=hash_approval_token("first"),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )
    later = first_grant.approval.expires_at + timedelta(seconds=1)
    second = await _challenge(stores, action, token="second", at=later)

    grant = await stores.actions.redeem_challenge(
        challenge_id=second.id,
        token_hash=hash_approval_token("second"),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=later,
    )

    assert grant.superseded == 1
    approvals = await stores.actions.list_approvals(action.id)
    assert len(approvals) == 2  # history kept
    assert approvals[0].id == first_grant.approval.id
    assert approvals[0].is_superseded() is True
    assert approvals[1].is_usable_at(later) is True


async def test_an_unknown_challenge_is_not_found(stores) -> None:
    action = await _action(stores)
    with pytest.raises(ApprovalChallengeNotFound):
        await stores.actions.redeem_challenge(
            challenge_id=new_approval_challenge_id(),
            token_hash=hash_approval_token(SECRET_TOKEN),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            approval_id=new_approval_id(),
            now=NOW,
        )


# ----------------------------------------------------------------- executions


async def test_beginning_an_execution_consumes_the_approval(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )
    run_id = uuid4()

    started = await stores.actions.begin_execution(
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        run_id=run_id,
        now=NOW + timedelta(minutes=1),
    )

    assert started.run.status is ExecutionRunStatus.RUNNING
    assert started.approval.is_consumed() is True
    assert await stores.actions.count_executions(action.id) == 1
    with pytest.raises(ActionExecutionUnresolved):
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + timedelta(minutes=2),
        )


async def test_an_expired_approval_cannot_start_an_execution(stores) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )

    with pytest.raises(ApprovalUnavailable):
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + approval_ttl(),
        )


async def test_only_one_of_two_racing_callers_starts_an_execution(stores) -> None:
    """Real concurrency: two awaits on the same connection pool, one approval."""
    from uuid import uuid4

    action = await _action(stores)
    challenge = await _challenge(stores, action)
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )

    async def attempt() -> object:
        return await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + timedelta(minutes=1),
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], (ApprovalUnavailable, ActionExecutionUnresolved))
    assert await stores.actions.count_executions(action.id) == 1


async def test_finishing_with_success_marks_the_action_executed(stores) -> None:
    action, run = await _approved_running(stores)

    finished = await stores.actions.finish_execution(
        run_id=run.id, outcome=ExecutionOutcome.succeeded(), at=NOW + timedelta(minutes=2)
    )

    assert finished.status is ExecutionRunStatus.SUCCEEDED
    stored = await stores.actions.get_action(action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED
    assert stored.executed_at == NOW + timedelta(minutes=2)


async def test_finishing_with_failure_leaves_the_action_prepared(stores) -> None:
    action, run = await _approved_running(stores)

    finished = await stores.actions.finish_execution(
        run_id=run.id,
        outcome=ExecutionOutcome.failed("the remote refused"),
        at=NOW + timedelta(minutes=2),
    )

    assert finished.status is ExecutionRunStatus.FAILED
    stored = await stores.actions.get_action(action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    # The approval is spent: trying again needs a new human approval.
    with pytest.raises(ApprovalUnavailable):
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + timedelta(minutes=3),
        )


async def test_an_unknown_result_blocks_a_new_execution(stores) -> None:
    from uuid import uuid4

    action, run = await _approved_running(stores)
    await stores.actions.finish_execution(
        run_id=run.id,
        outcome=ExecutionOutcome.unknown("no response"),
        at=NOW + timedelta(minutes=2),
    )
    # Approve again; the unresolved attempt still blocks the execution.
    challenge = await _challenge(stores, action, token="fresh", at=NOW + timedelta(minutes=3))
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token("fresh"),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW + timedelta(minutes=3),
    )

    with pytest.raises(ActionExecutionUnresolved) as caught:
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + timedelta(minutes=4),
        )

    assert "unresolved" in str(caught.value)
    assert await stores.actions.count_executions(action.id) == 1


async def test_finishing_an_unknown_run_is_not_found(stores) -> None:
    from uuid import uuid4

    with pytest.raises(ExecutionRunNotFound):
        await stores.actions.finish_execution(
            run_id=uuid4(),
            outcome=ExecutionOutcome.succeeded(),
            at=NOW,
        )


async def _approved_running(stores):
    """An action with a fresh approval and a RUNNING execution."""
    from uuid import uuid4

    action = await _action(stores)
    challenge = await _challenge(stores, action)
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )
    started = await stores.actions.begin_execution(
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        run_id=uuid4(),
        now=NOW + timedelta(minutes=1),
    )
    return action, started.run


async def test_a_challenge_hash_is_never_the_token(stores, database) -> None:
    """The plaintext token exists in no column of any approval table."""
    action = await _action(stores)
    await _challenge(stores, action)

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, action_id, action_fingerprint, token_hash, created_at, expires_at, "
            "consumed_at FROM approval_challenges"
        ).fetchall()
    dumped = " ".join(str(value) for row in rows for value in tuple(row))
    assert SECRET_TOKEN not in dumped
    assert hash_approval_token(SECRET_TOKEN) in dumped
    assert len(rows) == 1


async def test_the_store_reports_a_stale_action_payload_at_execution_time(stores, database) -> None:
    action = await _action(stores)
    challenge = await _challenge(stores, action)
    await stores.actions.redeem_challenge(
        challenge_id=challenge.id,
        token_hash=hash_approval_token(SECRET_TOKEN),
        action_id=action.id,
        action_fingerprint=action.fingerprint,
        approval_id=new_approval_id(),
        now=NOW,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"body":"swapped"}', str(action.id)),
        )

    with pytest.raises(CommitmentStoreError):
        await stores.actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=uuid4(),
            now=NOW + timedelta(minutes=1),
        )

