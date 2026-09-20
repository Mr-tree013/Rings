"""The learning service over real SQLite: corrections, promotion and derived expiry (ADR-0027).

Two things are being proven here. First, that a fact only ever exists because a human promoted a
candidate with the value they saw. Second, and just as important, that learning touches nothing
else: after adding, confirming and rejecting facts, every other table in the database still holds
exactly the rows it held before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.application.learning_service import LearningService
from assistant.domain.errors import (
    ExpiredFactCandidate,
    ForbiddenFactKey,
    InvalidFactCandidateTransition,
)
from assistant.domain.fact import FactCandidateStatus, FactState
from assistant.store.db import Database
from assistant.store.learning import SqliteLearningRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

LEARNING_TABLES = frozenset({"corrections", "fact_candidates", "confirmed_facts"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def service(database: Database, clock: FakeClock) -> LearningService:
    return LearningService(SqliteLearningRepository(database), clock)


# -------------------------------------------------------------------- corrections


async def test_a_correction_is_stored_on_its_own(service: LearningService) -> None:
    correction = await service.add_correction("  Deadline is the 23rd, not the 25th.  ")

    assert correction.text == "Deadline is the 23rd, not the 25th."
    listed = await service.list_corrections(limit=None)
    assert [item.id for item in listed] == [correction.id]


async def test_a_correction_can_be_found_by_prefix(service: LearningService) -> None:
    correction = await service.add_correction("I read my mail on the phone.")

    detail = await service.get_correction(str(correction.id)[:8])

    assert detail.correction.id == correction.id
    assert detail.candidates == ()


# --------------------------------------------------------------------- proposals


async def test_proposing_a_fact_stores_the_note_and_the_candidate_together(
    service: LearningService, database: Database
) -> None:
    proposal = await service.propose_fact(
        "profile.office", "Room 302", "My office is Room 302 in the SE building"
    )

    assert proposal.candidate.fact_key == "profile.office"
    assert proposal.candidate.value == "Room 302"
    assert proposal.candidate.correction_id == proposal.correction.id
    assert proposal.candidate.status is FactCandidateStatus.PENDING
    counts = await _counts(database)
    assert counts["corrections"] == 1
    assert counts["fact_candidates"] == 1
    assert counts["confirmed_facts"] == 0


async def test_a_credential_like_key_is_refused_before_anything_is_written(
    service: LearningService, database: Database
) -> None:
    with pytest.raises(ForbiddenFactKey):
        await service.propose_fact("mail.smtp_token", "abc123", "the SMTP token")

    counts = await _counts(database)
    assert counts["corrections"] == 0
    assert counts["fact_candidates"] == 0


async def test_the_candidate_detail_carries_the_users_own_words(
    service: LearningService,
) -> None:
    proposal = await service.propose_fact(
        "preferences.reply_signature", "Best, Ada", "I sign my replies this way"
    )

    detail = await service.get_candidate(str(proposal.candidate.id)[:8])

    assert detail.candidate.id == proposal.candidate.id
    assert detail.correction.text == "I sign my replies this way"
    assert detail.fact is None


# ------------------------------------------------------------------ confirmation


async def test_confirming_promotes_the_candidate_into_a_fact(
    service: LearningService,
) -> None:
    proposal = await service.propose_fact("profile.office", "Room 302", "my office")

    confirmation = await service.confirm_fact(proposal.candidate.id)

    assert confirmation.superseded is None
    assert confirmation.fact.value == "Room 302"
    assert confirmation.fact.fact_key == "profile.office"
    assert confirmation.fact.valid_from == NOW
    detail = await service.get_candidate(proposal.candidate.id)
    assert detail.candidate.status is FactCandidateStatus.CONFIRMED
    assert detail.fact is not None and detail.fact.id == confirmation.fact.id


async def test_confirming_twice_is_refused(service: LearningService) -> None:
    proposal = await service.propose_fact("profile.office", "Room 302", "my office")
    await service.confirm_fact(proposal.candidate.id)

    with pytest.raises(InvalidFactCandidateTransition):
        await service.confirm_fact(proposal.candidate.id)


async def test_a_new_value_supersedes_the_old_one_and_keeps_the_history(
    service: LearningService, clock: FakeClock
) -> None:
    """The §27 example: Room 302, then Room 320, both still on the record."""
    first = await service.propose_fact("profile.office", "Room 302", "my office is 302")
    await service.confirm_fact(first.candidate.id)
    clock.advance(86400)
    second = await service.propose_fact("profile.office", "Room 320", "I moved to 320")

    confirmation = await service.confirm_fact(second.candidate.id)

    assert confirmation.superseded is not None
    assert confirmation.superseded.value == "Room 302"
    assert confirmation.superseded.superseded_at == NOW + timedelta(days=1)
    history = await service.list_facts(include_inactive=True, limit=None)
    assert sorted(item.value for item in history) == ["Room 302", "Room 320"]
    active = await service.get_active_fact("profile.office")
    assert active is not None and active.value == "Room 320"


async def test_rejecting_keeps_the_candidate_and_creates_no_fact(
    service: LearningService,
) -> None:
    proposal = await service.propose_fact("profile.office", "Room 302", "my office")

    rejected = await service.reject_fact(proposal.candidate.id)

    assert rejected.status is FactCandidateStatus.REJECTED
    assert await service.list_facts(include_inactive=True, limit=None) == []
    assert [item.id for item in await service.list_candidates(limit=None)] == [
        proposal.candidate.id
    ]


async def test_a_candidate_whose_window_closed_cannot_be_confirmed(
    service: LearningService, clock: FakeClock
) -> None:
    proposal = await service.propose_fact(
        "profile.course",
        "SE-2026",
        "I am taking SE this term",
        valid_until=NOW + timedelta(hours=1),
    )
    clock.advance(3600)

    with pytest.raises(ExpiredFactCandidate):
        await service.confirm_fact(proposal.candidate.id)

    still_pending = await service.get_candidate(proposal.candidate.id)
    assert still_pending.candidate.is_pending is True
    assert await service.get_active_fact("profile.course") is None


# ------------------------------------------------------------------------ expiry


async def test_expiry_is_derived_at_read_time_with_no_job(
    service: LearningService, clock: FakeClock, database: Database
) -> None:
    """§26: active at T and T+59m, expired at T+1h, and no row was ever rewritten."""
    proposal = await service.propose_fact(
        "preferences.library_seat",
        "Third floor",
        "I prefer the third floor",
        valid_until=NOW + timedelta(hours=1),
    )
    await service.confirm_fact(proposal.candidate.id)

    assert (await service.get_active_fact("preferences.library_seat")) is not None
    clock.advance(59 * 60)
    at_59 = await service.get_active_fact("preferences.library_seat")
    assert at_59 is not None and at_59.value == "Third floor"
    clock.advance(60)

    assert await service.get_active_fact("preferences.library_seat") is None
    assert await service.list_facts(limit=None) == []
    with_history = await service.list_facts(include_inactive=True, limit=None)
    assert [item.state_at(clock.now()) for item in with_history] == [FactState.EXPIRED]
    # Nothing mutated the row: expiry is a read-time judgement.
    stored = await service.get_fact(with_history[0].id)
    assert stored.fact.superseded_at is None


# ------------------------------------------------------------------------ lookups


async def test_listing_facts_hides_inactive_rows_by_default(
    service: LearningService, clock: FakeClock
) -> None:
    expiring = await service.propose_fact(
        "profile.office", "Room 302", "my office", valid_until=NOW + timedelta(hours=1)
    )
    await service.confirm_fact(expiring.candidate.id)
    forever = await service.propose_fact("profile.full_name", "Ada Lovelace", "my name")
    await service.confirm_fact(forever.candidate.id)
    clock.advance(7200)

    active = await service.list_facts(limit=None)
    everything = await service.list_facts(include_inactive=True, limit=None)

    assert [item.fact_key for item in active] == ["profile.full_name"]
    assert len(everything) == 2
    states = {item.fact_key: item.state_at(clock.now()) for item in everything}
    assert states["profile.office"] is FactState.EXPIRED
    assert states["profile.full_name"] is FactState.ACTIVE


async def test_a_fact_carries_its_provenance(service: LearningService) -> None:
    proposal = await service.propose_fact("profile.office", "Room 302", "my office is 302")
    fact = (await service.confirm_fact(proposal.candidate.id)).fact

    detail = await service.get_fact(str(fact.id)[:8])

    assert detail.fact.id == fact.id
    assert detail.candidate.id == proposal.candidate.id
    assert detail.correction.id == proposal.correction.id
    assert detail.correction.text == "my office is 302"


async def test_fact_overviews_pair_each_fact_with_its_correction(
    service: LearningService,
) -> None:
    first = await service.propose_fact("profile.office", "Room 302", "office")
    await service.confirm_fact(first.candidate.id)

    overviews = await service.list_fact_overviews(limit=None)

    assert len(overviews) == 1
    assert overviews[0].correction is not None
    assert overviews[0].correction.id == first.correction.id


async def test_listing_candidates_can_be_narrowed_to_pending(
    service: LearningService,
) -> None:
    pending = await service.propose_fact("profile.office", "Room 302", "office")
    rejected = await service.propose_fact("profile.office", "Room 320", "old office")
    await service.reject_fact(rejected.candidate.id)

    only_pending = await service.list_candidates(
        statuses=(FactCandidateStatus.PENDING,), limit=None
    )

    assert [item.id for item in only_pending] == [pending.candidate.id]
    assert len(await service.list_candidates(limit=None)) == 2


# --------------------------------------------------------------- no-mutation guarantee


async def test_learning_touches_only_its_own_three_tables(
    service: LearningService, database: Database, clock: FakeClock
) -> None:
    """§38: a fact is not a task, a message, an action or a session — and it never becomes one."""
    before = await _counts(database)
    assert set(before) - LEARNING_TABLES

    first = await service.propose_fact("profile.office", "Room 302", "my office")
    await service.confirm_fact(first.candidate.id)
    clock.advance(60)
    second = await service.propose_fact("profile.office", "Room 320", "I moved")
    await service.confirm_fact(second.candidate.id)
    third = await service.propose_fact("profile.full_name", "Ada", "my name")
    await service.reject_fact(third.candidate.id)
    await service.add_correction("A note with no candidate")

    after = await _counts(database)

    changed = {table for table in before if before[table] != after[table]}
    assert changed == LEARNING_TABLES
    assert after["corrections"] == 4
    assert after["fact_candidates"] == 3
    assert after["confirmed_facts"] == 2


async def test_learning_logs_neither_the_note_nor_the_value(
    service: LearningService, caplog: pytest.LogCaptureFixture
) -> None:
    """§34: a fact value may be sensitive, so the default log says nothing about it."""
    with caplog.at_level("DEBUG"):
        proposal = await service.propose_fact(
            "profile.student_id",
            "MG2300001",
            "my student id is on the card",
        )
        await service.confirm_fact(proposal.candidate.id)

    logged = caplog.text
    assert "MG2300001" not in logged
    assert "my student id is on the card" not in logged
    assert "profile.student_id" not in logged


async def _counts(database: Database) -> dict[str, int]:
    """Row counts for every table, so a stray write anywhere shows up as a diff."""
    with database.connect() as connection:
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counted: dict[str, int] = {}
        for table in sorted(tables):
            row = connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()
            counted[table] = int(row["total"])
    return counted
