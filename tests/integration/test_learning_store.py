"""Corrections, candidates and confirmed facts against real SQLite (ADR-0027).

The cases that matter here are the ones only a database can answer: a candidate written without
its correction, a key that briefly has two current facts, a confirmation that fails halfway, and
two callers racing on the same key. sqlite3 is never mocked.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.correction import Correction
from assistant.domain.errors import (
    AmbiguousId,
    ConfirmedFactNotFound,
    CorrectionNotFound,
    ExpiredFactCandidate,
    FactCandidateNotFound,
    InvalidFactCandidateTransition,
)
from assistant.domain.fact import (
    ConfirmedFact,
    FactCandidate,
    FactCandidateStatus,
    FactState,
    new_confirmed_fact_id,
)
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.learning import SqliteLearningRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def learning(database: Database) -> SqliteLearningRepository:
    return SqliteLearningRepository(database)


async def _candidate(
    learning: SqliteLearningRepository,
    *,
    key: str = "profile.office",
    value: str = "Room 302",
    note: str = "My office is Room 302",
    valid_until: datetime | None = None,
    at: datetime = NOW,
) -> FactCandidate:
    correction = Correction(text=note, created_at=at)
    candidate = FactCandidate(
        fact_key=key,
        value=value,
        correction_id=correction.id,
        created_at=at,
        proposed_valid_until=valid_until,
    )
    _, stored = await learning.create_candidate_with_correction(
        correction=correction, candidate=candidate
    )
    return stored


async def _current_facts(
    database: Database, key: str | None = None
) -> list[tuple[str, str, str | None]]:
    """Raw read: every non-superseded fact row, so tests never trust the repository's own query."""
    with database.connect() as connection:
        statement = (
            "SELECT fact_key, value, superseded_at FROM confirmed_facts "
            "WHERE superseded_at IS NULL"
        )
        parameters: tuple[object, ...] = ()
        if key is not None:
            statement += " AND fact_key = ?"
            parameters = (key,)
        rows = connection.execute(statement, parameters).fetchall()
    return [(str(row["fact_key"]), str(row["value"]), row["superseded_at"]) for row in rows]


# ------------------------------------------------------- candidate + correction atomicity


async def test_a_candidate_is_stored_with_the_correction_that_justifies_it(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)

    stored = await learning.get_candidate(candidate.id)
    correction = await learning.get_correction(candidate.correction_id)

    assert stored == candidate
    assert correction is not None
    assert correction.text == "My office is Room 302"


async def test_a_candidate_that_does_not_match_its_correction_commits_neither(
    learning: SqliteLearningRepository, database: Database
) -> None:
    """§10: the correction must not survive a candidate the database refused."""
    correction = Correction(text="This must not be committed", created_at=NOW)
    candidate = FactCandidate(
        fact_key="profile.office",
        value="Room 302",
        correction_id=uuid4(),  # nothing points at the correction above
        created_at=NOW,
    )

    with pytest.raises(CommitmentStoreError):
        await learning.create_candidate_with_correction(
            correction=correction, candidate=candidate
        )

    assert await learning.get_correction(correction.id) is None
    assert await learning.list_corrections(limit=None) == []


async def test_a_correction_can_exist_without_a_candidate(
    learning: SqliteLearningRepository,
) -> None:
    correction = Correction(text="I prefer short replies", created_at=NOW)

    stored = await learning.add_correction(correction)

    assert stored == correction
    assert await learning.get_correction(correction.id) == correction


# ------------------------------------------------------------------------- confirmation


async def test_confirming_creates_the_fact_and_resolves_the_candidate(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)

    confirmation = await learning.confirm_candidate(
        candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
    )

    assert confirmation.superseded is None
    assert confirmation.fact.candidate_id == candidate.id
    assert confirmation.fact.fact_key == "profile.office"
    assert confirmation.fact.value == "Room 302"
    resolved = await learning.get_candidate(candidate.id)
    assert resolved is not None
    assert resolved.status is FactCandidateStatus.CONFIRMED
    assert resolved.resolved_at == NOW


async def test_a_candidate_can_only_be_confirmed_once(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)
    await learning.confirm_candidate(
        candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
    )

    with pytest.raises(InvalidFactCandidateTransition):
        await learning.confirm_candidate(
            candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
        )

    assert len(await learning.list_confirmed_facts(limit=None)) == 1


async def test_rejecting_keeps_the_candidate_and_creates_no_fact(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)

    rejected = await learning.reject_candidate(candidate_id=candidate.id, now=NOW)

    assert rejected.status is FactCandidateStatus.REJECTED
    assert rejected.resolved_at == NOW
    assert await learning.list_confirmed_facts(limit=None) == []
    assert await learning.get_candidate(candidate.id) == rejected


async def test_a_rejected_candidate_cannot_then_be_confirmed(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)
    await learning.reject_candidate(candidate_id=candidate.id, now=NOW)

    with pytest.raises(InvalidFactCandidateTransition):
        await learning.confirm_candidate(
            candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
        )


async def test_confirming_or_rejecting_an_unknown_candidate_fails(
    learning: SqliteLearningRepository,
) -> None:
    with pytest.raises(FactCandidateNotFound):
        await learning.confirm_candidate(
            candidate_id=uuid4(), fact_id=new_confirmed_fact_id(), now=NOW
        )
    with pytest.raises(FactCandidateNotFound):
        await learning.reject_candidate(candidate_id=uuid4(), now=NOW)


# ------------------------------------------------------------------------ supersession


async def test_a_new_value_supersedes_the_current_one_and_keeps_history(
    learning: SqliteLearningRepository, database: Database
) -> None:
    first = await _candidate(learning, value="Room 302")
    await learning.confirm_candidate(
        candidate_id=first.id, fact_id=new_confirmed_fact_id(), now=NOW
    )
    second = await _candidate(
        learning, value="Room 320", note="I moved", at=NOW + timedelta(days=1)
    )

    confirmation = await learning.confirm_candidate(
        candidate_id=second.id,
        fact_id=new_confirmed_fact_id(),
        now=NOW + timedelta(days=1),
    )

    assert confirmation.superseded is not None
    assert confirmation.superseded.value == "Room 302"
    assert confirmation.superseded.superseded_at == NOW + timedelta(days=1)
    history = await learning.list_confirmed_facts(limit=None)
    assert sorted(item.value for item in history) == ["Room 302", "Room 320"]
    assert await _current_facts(database, "profile.office") == [
        ("profile.office", "Room 320", None)
    ]


async def test_an_expired_but_current_fact_is_still_retired_by_the_next_value(
    learning: SqliteLearningRepository, database: Database
) -> None:
    """The partial index does not care about expiry, so confirmation has to."""
    first = await _candidate(
        learning, value="Room 302", valid_until=NOW + timedelta(hours=1)
    )
    await learning.confirm_candidate(
        candidate_id=first.id, fact_id=new_confirmed_fact_id(), now=NOW
    )
    later = NOW + timedelta(days=1)
    second = await _candidate(learning, value="Room 320", at=later)

    confirmation = await learning.confirm_candidate(
        candidate_id=second.id, fact_id=new_confirmed_fact_id(), now=later
    )

    assert confirmation.superseded is not None
    assert confirmation.superseded.value == "Room 302"
    assert len(await _current_facts(database, "profile.office")) == 1


async def test_a_candidate_whose_window_has_closed_cannot_be_confirmed(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning, valid_until=NOW + timedelta(hours=1))

    with pytest.raises(ExpiredFactCandidate):
        await learning.confirm_candidate(
            candidate_id=candidate.id,
            fact_id=new_confirmed_fact_id(),
            now=NOW + timedelta(hours=2),
        )

    unresolved = await learning.get_candidate(candidate.id)
    assert unresolved is not None and unresolved.is_pending
    assert await learning.list_confirmed_facts(limit=None) == []


async def test_a_failing_insert_rolls_the_supersession_back(
    learning: SqliteLearningRepository, database: Database
) -> None:
    """§30: the old fact must survive a confirmation that could not finish."""
    first = await _candidate(learning, value="Room 302")
    used_fact_id = new_confirmed_fact_id()
    await learning.confirm_candidate(
        candidate_id=first.id, fact_id=used_fact_id, now=NOW
    )
    second = await _candidate(learning, value="Room 320", at=NOW + timedelta(minutes=5))

    with pytest.raises(CommitmentStoreError):
        # The id is already taken, so the insert fails *after* the supersession ran.
        await learning.confirm_candidate(
            candidate_id=second.id,
            fact_id=used_fact_id,
            now=NOW + timedelta(minutes=5),
        )

    unfinished = await learning.get_candidate(second.id)
    assert unfinished is not None and unfinished.is_pending
    assert await _current_facts(database, "profile.office") == [
        ("profile.office", "Room 302", None)
    ]
    still_first = await learning.get_confirmed_fact(used_fact_id)
    assert still_first is not None and still_first.superseded_at is None


# ------------------------------------------------------------------- active read model


async def test_active_reads_ignore_expired_and_superseded_rows(
    learning: SqliteLearningRepository,
) -> None:
    expiring = await _candidate(
        learning, key="profile.office", value="Room 302", valid_until=NOW + timedelta(hours=1)
    )
    await learning.confirm_candidate(
        candidate_id=expiring.id, fact_id=new_confirmed_fact_id(), now=NOW
    )

    assert (await learning.get_active_fact_by_key("profile.office", now=NOW)).value == "Room 302"
    assert await learning.get_active_fact_by_key(
        "profile.office", now=NOW + timedelta(hours=1)
    ) is None
    assert [item.value for item in await learning.list_active_facts(now=NOW)] == ["Room 302"]
    assert await learning.list_active_facts(now=NOW + timedelta(hours=1)) == []
    # The row is still there, still current, and simply not active any more.
    history = await learning.list_confirmed_facts(limit=None)
    assert len(history) == 1
    assert history[0].state_at(NOW + timedelta(hours=1)) is FactState.EXPIRED


async def test_an_unknown_fact_or_key_resolves_to_nothing(
    learning: SqliteLearningRepository,
) -> None:
    assert await learning.get_confirmed_fact(uuid4()) is None
    assert await learning.get_active_fact_by_key("profile.office", now=NOW) is None


# -------------------------------------------------------------------------- lookups


async def test_candidates_can_be_filtered_and_listed(
    learning: SqliteLearningRepository,
) -> None:
    pending = await _candidate(learning, value="Room 302")
    rejected = await _candidate(learning, value="Room 320", at=NOW + timedelta(minutes=1))
    await learning.reject_candidate(
        candidate_id=rejected.id, now=NOW + timedelta(minutes=2)
    )

    everything = await learning.list_candidates(limit=None)
    only_pending = await learning.list_candidates(
        statuses=(FactCandidateStatus.PENDING,), limit=None
    )

    assert {item.id for item in everything} == {pending.id, rejected.id}
    assert [item.id for item in only_pending] == [pending.id]


async def test_a_prefix_resolves_a_correction_and_is_refused_when_ambiguous(
    learning: SqliteLearningRepository,
) -> None:
    """0 matches, 1 match and >1 matches behave the way every other id in this project does."""
    shared = "11111111"
    first_correction = Correction(text="first", created_at=NOW)
    second_correction = Correction(text="second", created_at=NOW)
    await learning.create_candidate_with_correction(
        correction=first_correction,
        candidate=FactCandidate(
            id=UUID(f"{shared}-0000-0000-0000-000000000001"),
            fact_key="profile.office",
            value="Room 302",
            correction_id=first_correction.id,
            created_at=NOW,
        ),
    )
    await learning.create_candidate_with_correction(
        correction=second_correction,
        candidate=FactCandidate(
            id=UUID(f"{shared}-0000-0000-0000-000000000002"),
            fact_key="profile.office",
            value="Room 320",
            correction_id=second_correction.id,
            created_at=NOW,
        ),
    )

    assert await learning.resolve_correction_id(str(first_correction.id)[:8]) == (
        first_correction.id
    )
    with pytest.raises(AmbiguousId):
        await learning.resolve_candidate_id(shared)
    with pytest.raises(FactCandidateNotFound):
        await learning.resolve_candidate_id("ffffffff")
    with pytest.raises(CorrectionNotFound):
        await learning.resolve_correction_id("ffffffff")
    with pytest.raises(FactCandidateNotFound):
        await learning.resolve_candidate_id(str(uuid4()))


# ---------------------------------------------------------------------- concurrency


async def test_two_callers_confirming_the_same_candidate_settle_on_one_fact(
    learning: SqliteLearningRepository, database: Database
) -> None:
    candidate = await _candidate(learning)

    async def attempt() -> object:
        return await learning.confirm_candidate(
            candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], InvalidFactCandidateTransition)
    assert len(await learning.list_confirmed_facts(limit=None)) == 1


async def test_two_callers_confirming_different_candidates_for_one_key_serialise(
    learning: SqliteLearningRepository, database: Database
) -> None:
    """§28: whichever lands second becomes the current fact; history keeps both."""
    first = await _candidate(learning, value="Room 302")
    second = await _candidate(learning, value="Room 320", at=NOW + timedelta(minutes=1))

    async def attempt(candidate: FactCandidate) -> object:
        return await learning.confirm_candidate(
            candidate_id=candidate.id,
            fact_id=new_confirmed_fact_id(),
            now=NOW + timedelta(minutes=2),
        )

    results = await asyncio.gather(attempt(first), attempt(second), return_exceptions=True)

    assert not [item for item in results if isinstance(item, BaseException)]
    current = await _current_facts(database, "profile.office")
    assert len(current) == 1
    assert len(await learning.list_confirmed_facts(limit=None)) == 2
    # Whichever won, exactly one fact is active for the key.
    active = await learning.get_active_fact_by_key(
        "profile.office", now=NOW + timedelta(minutes=3)
    )
    assert active is not None
    assert active.value == current[0][1]


async def test_the_database_refuses_a_second_current_row_for_one_key(
    learning: SqliteLearningRepository, database: Database
) -> None:
    """The partial unique index is the last line of defence, not just a comment."""
    candidate = await _candidate(learning)
    await learning.confirm_candidate(
        candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
    )
    twin_candidate = await _candidate(learning, value="Room 320", at=NOW + timedelta(minutes=1))
    with (
        database.connect() as connection,
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
    ):
        connection.execute(
            "INSERT INTO confirmed_facts (id, candidate_id, fact_key, value, valid_from, "
            "valid_until, created_at, superseded_at) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            (
                str(uuid4()),
                str(twin_candidate.id),
                "profile.office",
                "Room 320",
                "2026-09-20T12:01:00.000000+00:00",
                "2026-09-20T12:01:00.000000+00:00",
            ),
        )


async def test_a_confirmed_fact_id_resolves_by_prefix(
    learning: SqliteLearningRepository,
) -> None:
    candidate = await _candidate(learning)
    fact: ConfirmedFact = (
        await learning.confirm_candidate(
            candidate_id=candidate.id, fact_id=new_confirmed_fact_id(), now=NOW
        )
    ).fact

    assert await learning.resolve_confirmed_fact_id(str(fact.id)[:8]) == fact.id
    with pytest.raises(ConfirmedFactNotFound):
        await learning.resolve_confirmed_fact_id("ffffffff")
