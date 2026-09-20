"""Candidates, replay tests and playbooks against real SQLite (ADR-0028).

What is under test here is what only a database can answer: one candidate per source action, one
playbook per candidate, a promotion that cannot half-happen, and two callers racing to promote the
same candidate. sqlite3 is never mocked.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.errors import (
    AmbiguousId,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookTransition,
    PlaybookCandidateExists,
    PlaybookCandidateNotFound,
    PlaybookCandidateNotTested,
    PlaybookNotFound,
)
from assistant.domain.playbook import (
    PlaybookCandidate,
    PlaybookCandidateStatus,
    PlaybookReplayTest,
    PlaybookStatus,
    ReplayTestStatus,
    replay_input_fingerprint,
)
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.migrations import apply_migrations
from assistant.store.playbooks import SqlitePlaybookRepository

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=_clock())
    return db


@pytest.fixture
def playbooks(database: Database) -> SqlitePlaybookRepository:
    return SqlitePlaybookRepository(database)


def _clock() -> object:
    from tests.support.fakes import FakeClock

    return FakeClock(start=NOW)


async def _source(database: Database) -> tuple[UUID, UUID]:
    """One stored action and one stored execution run, as real rows the FKs can point at."""
    from assistant.domain.action import ActionRequest, ActionRequestStatus
    from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
    from assistant.store.actions import SqliteActionRepository
    from assistant.store.cases import SqliteCaseRepository

    clock = _clock()
    cases = SqliteCaseRepository(database)
    actions = SqliteActionRepository(database)
    from assistant.application.case_service import CaseService

    case = await CaseService(cases, actions, clock).create_case("A success")  # type: ignore[arg-type]
    action = await CaseService(cases, actions, clock).prepare_action(  # type: ignore[arg-type]
        case.id, "mail.send", {"to": "ada@example.edu"}
    )
    executed = ActionRequest(
        id=action.id,
        case_id=action.case_id,
        action_type=action.action_type,
        payload_json=action.payload_json,
        fingerprint=action.fingerprint,
        status=ActionRequestStatus.EXECUTED,
        created_at=action.created_at,
        executed_at=NOW,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET status = 'executed', executed_at = ? WHERE id = ?",
            (NOW.isoformat(timespec="microseconds"), str(action.id)),
        )
        run = ExecutionRun(
            action_id=action.id,
            approval_id=uuid4(),
            started_at=NOW,
            status=ExecutionRunStatus.SUCCEEDED,
            finished_at=NOW,
        )
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, "
            "expires_at, consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                str(run.approval_id),
                str(action.id),
                FINGERPRINT,
                NOW.isoformat(timespec="microseconds"),
                (NOW + timedelta(minutes=10)).isoformat(timespec="microseconds"),
                NOW.isoformat(timespec="microseconds"),
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
            "finished_at, error_summary) VALUES (?, ?, ?, 'succeeded', ?, ?, NULL)",
            (
                str(run.id),
                str(action.id),
                str(run.approval_id),
                NOW.isoformat(timespec="microseconds"),
                NOW.isoformat(timespec="microseconds"),
            ),
        )
    assert executed.status is ActionRequestStatus.EXECUTED
    return action.id, run.id


async def _candidate(
    playbooks: SqlitePlaybookRepository,
    database: Database,
    *,
    name: str = "Approved mail shape",
    action_id: UUID | None = None,
    run_id: UUID | None = None,
) -> PlaybookCandidate:
    stored_action, stored_run = await _source(database)
    candidate = PlaybookCandidate(
        name=name,
        note="Reviewed the run.",
        source_action_id=stored_action if action_id is None else action_id,
        source_execution_run_id=stored_run if run_id is None else run_id,
        source_action_type="mail.send",
        source_action_fingerprint=FINGERPRINT,
        created_at=NOW,
    )
    return await playbooks.create_candidate(candidate)


async def _passing_test(
    playbooks: SqlitePlaybookRepository,
    candidate: PlaybookCandidate,
    *,
    contract_version: int = 1,
    input_fingerprint: str | None = None,
    at: datetime = NOW,
) -> PlaybookReplayTest:
    return await playbooks.add_replay_test(
        PlaybookReplayTest(
            candidate_id=candidate.id,
            action_type=candidate.source_action_type,
            contract_version=contract_version,
            input_fingerprint=input_fingerprint or "b" * 64,
            status=ReplayTestStatus.PASSED,
            tested_at=at,
        )
    )


def _fingerprint(candidate: PlaybookCandidate, contract_version: int = 1) -> str:
    return replay_input_fingerprint(
        candidate_id=candidate.id,
        source_action_id=candidate.source_action_id,
        source_action_fingerprint=candidate.source_action_fingerprint,
        source_action_type=candidate.source_action_type,
        validator_action_type=candidate.source_action_type,
        contract_version=contract_version,
    )


# -------------------------------------------------------------------- candidates


async def test_a_candidate_is_stored_with_its_source(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)

    stored = await playbooks.get_candidate(candidate.id)

    assert stored == candidate
    assert stored is not None and stored.is_pending


async def test_one_successful_action_can_seed_one_candidate_only(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    """The UNIQUE constraint is the audit rule: no recreate-after-reject loophole."""
    action_id, run_id = await _source(database)
    first = await _candidate(playbooks, database, action_id=action_id, run_id=run_id)

    with pytest.raises(PlaybookCandidateExists) as raised:
        await _candidate(
            playbooks, database, name="A second try", action_id=action_id, run_id=run_id
        )

    assert raised.value.action_id == action_id
    assert [item.id for item in await playbooks.list_candidates(limit=None)] == [first.id]


async def test_a_candidate_must_point_at_a_real_action_and_run(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    from assistant.domain.errors import DomainError

    with pytest.raises((CommitmentStoreError, DomainError)):
        await _candidate(playbooks, database, action_id=uuid4(), run_id=uuid4())


async def test_candidates_can_be_filtered_and_resolved_by_prefix(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    first = await _candidate(playbooks, database)
    second = await _candidate(playbooks, database)
    await playbooks.reject_candidate(candidate_id=second.id, now=NOW)

    pending = await playbooks.list_candidates(
        statuses=(PlaybookCandidateStatus.PENDING,), limit=None
    )

    assert [item.id for item in pending] == [first.id]
    assert await playbooks.resolve_candidate_id(str(first.id)[:8]) == first.id
    with pytest.raises(PlaybookCandidateNotFound):
        await playbooks.resolve_candidate_id(str(uuid4()))


async def test_an_ambiguous_candidate_prefix_is_refused(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    shared = "22222222"
    for index in (1, 2):
        action_id, run_id = await _source(database)
        await playbooks.create_candidate(
            PlaybookCandidate(
                id=UUID(f"{shared}-0000-0000-0000-00000000000{index}"),
                name=f"candidate {index}",
                note="reviewed",
                source_action_id=action_id,
                source_execution_run_id=run_id,
                source_action_type="mail.send",
                source_action_fingerprint=FINGERPRINT,
                created_at=NOW,
            )
        )

    with pytest.raises(AmbiguousId):
        await playbooks.resolve_candidate_id(shared)


async def test_rejecting_keeps_the_candidate(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)

    rejected = await playbooks.reject_candidate(candidate_id=candidate.id, now=NOW)

    assert rejected.status is PlaybookCandidateStatus.REJECTED
    assert await playbooks.get_candidate(candidate.id) == rejected
    assert await playbooks.list_playbooks(limit=None) == []
    with pytest.raises(InvalidPlaybookCandidateTransition):
        await playbooks.reject_candidate(candidate_id=candidate.id, now=NOW)


async def test_an_unknown_candidate_cannot_be_resolved_or_rejected(
    playbooks: SqlitePlaybookRepository,
) -> None:
    with pytest.raises(PlaybookCandidateNotFound):
        await playbooks.reject_candidate(candidate_id=uuid4(), now=NOW)


# ------------------------------------------------------------------- replay tests


async def test_a_replay_test_is_appended_and_listed_newest_first(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    older = await _passing_test(playbooks, candidate, at=NOW)
    newer = await _passing_test(
        playbooks, candidate, at=NOW + timedelta(minutes=1)
    )

    tests = await playbooks.list_replay_tests(candidate.id)

    assert [item.id for item in tests] == [newer.id, older.id]
    assert await playbooks.get_replay_test(older.id) == older


async def test_a_failing_test_is_stored_with_its_bounded_codes(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    test = await playbooks.add_replay_test(
        PlaybookReplayTest(
            candidate_id=candidate.id,
            action_type=candidate.source_action_type,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            status=ReplayTestStatus.FAILED,
            issue_codes=("payload-invalid",),
            tested_at=NOW,
        )
    )

    stored = await playbooks.get_replay_test(test.id)

    assert stored is not None
    assert stored.issue_codes == ("payload-invalid",)
    assert stored.passed is False


async def test_only_a_matching_pass_qualifies_for_promotion(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(playbooks, candidate, input_fingerprint="c" * 64)
    await playbooks.add_replay_test(
        PlaybookReplayTest(
            candidate_id=candidate.id,
            action_type=candidate.source_action_type,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            status=ReplayTestStatus.FAILED,
            issue_codes=("payload-invalid",),
            tested_at=NOW,
        )
    )

    qualifying = await playbooks.latest_qualifying_test(
        candidate_id=candidate.id,
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(candidate),
    )

    assert qualifying is None


async def test_the_newest_qualifying_pass_wins(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    fingerprint = _fingerprint(candidate)
    await _passing_test(playbooks, candidate, input_fingerprint=fingerprint, at=NOW)
    newest = await _passing_test(
        playbooks,
        candidate,
        input_fingerprint=fingerprint,
        at=NOW + timedelta(minutes=5),
    )

    qualifying = await playbooks.latest_qualifying_test(
        candidate_id=candidate.id,
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=fingerprint,
    )

    assert qualifying is not None and qualifying.id == newest.id


async def test_a_pass_under_another_contract_version_does_not_qualify(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(
        playbooks, candidate, contract_version=1, input_fingerprint=_fingerprint(candidate, 2)
    )

    assert (
        await playbooks.latest_qualifying_test(
            candidate_id=candidate.id,
            action_type=candidate.source_action_type.value,
            contract_version=2,
            input_fingerprint=_fingerprint(candidate, 2),
        )
        is None
    )


# -------------------------------------------------------------------- promotion


async def test_promotion_creates_the_playbook_and_resolves_the_candidate(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    test = await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))
    playbook_id = uuid4()

    playbook = await playbooks.promote_candidate(
        candidate_id=candidate.id,
        playbook_id=playbook_id,
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(candidate),
        now=NOW + timedelta(minutes=1),
    )

    assert playbook.id == playbook_id
    assert playbook.promoted_from_test_id == test.id
    assert playbook.status is PlaybookStatus.ACTIVE
    assert playbook.source_action_id == candidate.source_action_id
    resolved = await playbooks.get_candidate(candidate.id)
    assert resolved is not None
    assert resolved.status is PlaybookCandidateStatus.PROMOTED
    assert resolved.resolved_at == NOW + timedelta(minutes=1)
    assert await playbooks.playbook_for_candidate(candidate.id) == playbook


async def test_promotion_without_a_passing_test_is_refused(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)

    with pytest.raises(PlaybookCandidateNotTested):
        await playbooks.promote_candidate(
            candidate_id=candidate.id,
            playbook_id=uuid4(),
            action_type=candidate.source_action_type.value,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            now=NOW,
        )

    still_pending = await playbooks.get_candidate(candidate.id)
    assert still_pending is not None and still_pending.is_pending
    assert await playbooks.list_playbooks(limit=None) == []


async def test_a_failing_playbook_insert_leaves_the_candidate_pending(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    """§41: the promotion transaction is all-or-nothing, so "promoted" cannot be a lie."""
    first = await _candidate(playbooks, database)
    await _passing_test(playbooks, first, input_fingerprint=_fingerprint(first))
    existing = await playbooks.promote_candidate(
        candidate_id=first.id,
        playbook_id=uuid4(),
        action_type=first.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(first),
        now=NOW,
    )
    second = await _candidate(playbooks, database)
    await _passing_test(playbooks, second, input_fingerprint=_fingerprint(second))

    with pytest.raises(CommitmentStoreError):
        # The playbook id is already taken, so the insert fails after the checks passed.
        await playbooks.promote_candidate(
            candidate_id=second.id,
            playbook_id=existing.id,
            action_type=second.source_action_type.value,
            contract_version=1,
            input_fingerprint=_fingerprint(second),
            now=NOW,
        )

    unfinished = await playbooks.get_candidate(second.id)
    assert unfinished is not None and unfinished.is_pending
    assert len(await playbooks.list_playbooks(limit=None)) == 1


async def test_promoting_a_resolved_candidate_is_refused(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))
    await playbooks.promote_candidate(
        candidate_id=candidate.id,
        playbook_id=uuid4(),
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(candidate),
        now=NOW,
    )

    with pytest.raises(InvalidPlaybookCandidateTransition):
        await playbooks.promote_candidate(
            candidate_id=candidate.id,
            playbook_id=uuid4(),
            action_type=candidate.source_action_type.value,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            now=NOW,
        )

    assert len(await playbooks.list_playbooks(limit=None)) == 1


async def test_a_rejected_candidate_can_never_be_promoted(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))
    await playbooks.reject_candidate(candidate_id=candidate.id, now=NOW)

    with pytest.raises(InvalidPlaybookCandidateTransition):
        await playbooks.promote_candidate(
            candidate_id=candidate.id,
            playbook_id=uuid4(),
            action_type=candidate.source_action_type.value,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            now=NOW,
        )


async def test_two_callers_promoting_one_candidate_produce_one_playbook(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))

    async def attempt() -> object:
        return await playbooks.promote_candidate(
            candidate_id=candidate.id,
            playbook_id=uuid4(),
            action_type=candidate.source_action_type.value,
            contract_version=1,
            input_fingerprint=_fingerprint(candidate),
            now=NOW,
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], InvalidPlaybookCandidateTransition)
    assert len(await playbooks.list_playbooks(limit=None)) == 1


# --------------------------------------------------------------------- playbooks


async def test_retiring_keeps_the_playbook_and_its_history(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    test = await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))
    playbook = await playbooks.promote_candidate(
        candidate_id=candidate.id,
        playbook_id=uuid4(),
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(candidate),
        now=NOW,
    )

    retired = await playbooks.retire_playbook(
        playbook_id=playbook.id, now=NOW + timedelta(days=1)
    )

    assert retired.status is PlaybookStatus.RETIRED
    assert retired.retired_at == NOW + timedelta(days=1)
    assert await playbooks.get_playbook(playbook.id) == retired
    assert await playbooks.get_replay_test(test.id) == test
    with pytest.raises(InvalidPlaybookTransition):
        await playbooks.retire_playbook(playbook_id=playbook.id, now=NOW)


async def test_an_unknown_playbook_cannot_be_retired(
    playbooks: SqlitePlaybookRepository,
) -> None:
    with pytest.raises(PlaybookNotFound):
        await playbooks.retire_playbook(playbook_id=uuid4(), now=NOW)


async def test_playbooks_can_be_filtered_and_resolved_by_prefix(
    playbooks: SqlitePlaybookRepository, database: Database
) -> None:
    candidate = await _candidate(playbooks, database)
    await _passing_test(playbooks, candidate, input_fingerprint=_fingerprint(candidate))
    playbook = await playbooks.promote_candidate(
        candidate_id=candidate.id,
        playbook_id=uuid4(),
        action_type=candidate.source_action_type.value,
        contract_version=1,
        input_fingerprint=_fingerprint(candidate),
        now=NOW,
    )
    await playbooks.retire_playbook(playbook_id=playbook.id, now=NOW)

    assert await playbooks.list_playbooks(statuses=(PlaybookStatus.ACTIVE,), limit=None) == []
    everything = await playbooks.list_playbooks(limit=None)
    assert [item.id for item in everything] == [playbook.id]
    assert everything[0].status is PlaybookStatus.RETIRED
    assert await playbooks.resolve_playbook_id(str(playbook.id)[:8]) == playbook.id
    with pytest.raises(PlaybookNotFound):
        await playbooks.resolve_playbook_id(str(uuid4()))
