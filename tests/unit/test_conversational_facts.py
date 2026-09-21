"""The fact vocabulary and the conversation-side fact orchestration (ADR-0038 §6-§8, §16).

Pure policy plus a thin service over the real learning service and a real database: what counts as
a confirmation, what counts as a proposal, and what a correction does to an earlier pending value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.application.conversational_facts import (
    FACT_CANCEL_PHRASES,
    FACT_CONFIRM_PHRASES,
    FACT_CONFIRMATION_INTENT_CANCEL,
    FACT_CONFIRMATION_INTENT_CONFIRM,
    ConversationalFactService,
    fact_confirmation_intent,
)
from assistant.application.learning_service import LearningService
from assistant.domain.errors import ForbiddenFactKey, InvalidFactKey
from assistant.domain.fact import FactCandidateStatus
from assistant.store.db import Database
from assistant.store.learning import SqliteLearningRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def service(tmp_path: Path, clock: FakeClock) -> ConversationalFactService:
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    return ConversationalFactService(
        LearningService(SqliteLearningRepository(database), clock)
    )


# ------------------------------------------------------------------------- vocabulary


@pytest.mark.parametrize("phrase", sorted(FACT_CONFIRM_PHRASES))
def test_every_confirmation_phrase_is_recognised(phrase: str) -> None:
    assert fact_confirmation_intent(phrase) == (FACT_CONFIRMATION_INTENT_CONFIRM, None)
    assert fact_confirmation_intent(f"{phrase}。") == (
        FACT_CONFIRMATION_INTENT_CONFIRM,
        None,
    )


@pytest.mark.parametrize("phrase", sorted(FACT_CANCEL_PHRASES))
def test_every_cancellation_phrase_is_recognised(phrase: str) -> None:
    assert fact_confirmation_intent(phrase) == (FACT_CONFIRMATION_INTENT_CANCEL, None)


@pytest.mark.parametrize(
    "text",
    (
        "可以",
        "好",
        "好的",
        "嗯",
        "继续",
        "ok",
        "yes",
        "记住我的办公室在仙林",
        "确认记住 profile.office",  # a key on its own is allowed…
        "记住吧",
        "把它记住",
        "",
    ),
)
def test_generic_or_longer_messages_are_not_confirmations(text: str) -> None:
    if text == "确认记住 profile.office":
        assert fact_confirmation_intent(text) == (
            FACT_CONFIRMATION_INTENT_CONFIRM,
            "profile.office",
        )
        return
    assert fact_confirmation_intent(text) is None


def test_a_phrase_with_a_key_names_that_key() -> None:
    assert fact_confirmation_intent("确认记住 profile.office") == (
        FACT_CONFIRMATION_INTENT_CONFIRM,
        "profile.office",
    )
    assert fact_confirmation_intent("不要记 profile.office") == (
        FACT_CONFIRMATION_INTENT_CANCEL,
        "profile.office",
    )


# ------------------------------------------------------------------------- proposals


async def test_a_proposal_is_pending_and_never_confirmed(
    service: ConversationalFactService,
) -> None:
    result = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林校区"
    )

    assert result.candidate.status is FactCandidateStatus.PENDING
    assert result.correction_text == "记住我的办公室在仙林校区"
    assert await service.list_facts() == []
    pending = await service.pending()
    assert [(item.fact_key, item.candidate.value) for item in pending] == [
        ("profile.office", "仙林校区")
    ]


async def test_confirming_promotes_exactly_one_fact(
    service: ConversationalFactService,
) -> None:
    result = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林校区"
    )

    confirmation = await service.confirm(result.candidate.id)

    assert confirmation.fact.fact_key == "profile.office"
    assert confirmation.fact.value == "仙林校区"
    assert confirmation.superseded is None
    assert [fact.value for fact in await service.list_facts()] == ["仙林校区"]
    assert await service.pending() == []


async def test_confirming_a_resolved_candidate_is_refused(
    service: ConversationalFactService,
) -> None:
    result = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林校区"
    )
    await service.confirm(result.candidate.id)

    with pytest.raises(Exception):  # noqa: B017 - the domain error is asserted by the CLI suite
        await service.confirm(result.candidate.id)
    assert len(await service.list_facts(limit=None)) == 1


async def test_a_correction_replaces_the_pending_value_and_keeps_history(
    service: ConversationalFactService,
) -> None:
    first = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林校区"
    )
    await service.confirm(first.candidate.id)

    second = await service.propose(
        key="profile.office", value="鼓楼校区", correction_text="不是仙林，是鼓楼"
    )
    confirmation = await service.confirm(second.candidate.id)

    assert confirmation.fact.value == "鼓楼校区"
    assert confirmation.superseded is not None
    assert confirmation.superseded.value == "仙林校区"
    current = await service.list_facts()
    assert [fact.value for fact in current] == ["鼓楼校区"]
    # History is kept, never deleted.
    history = await service.list_facts(include_inactive=True, limit=None)
    assert len(history) == 2


async def test_a_second_proposal_for_the_same_key_resolves_the_first(
    service: ConversationalFactService,
) -> None:
    first = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林"
    )
    second = await service.propose(
        key="profile.office", value="鼓楼校区", correction_text="不是仙林，是鼓楼"
    )

    pending = await service.pending()

    assert [item.candidate.value for item in pending] == ["鼓楼校区"]
    assert [item.id for item in second.superseded_pending] == [first.candidate.id]
    resolved = await service._learning.get_candidate(first.candidate.id)
    assert resolved.candidate.status is FactCandidateStatus.REJECTED
    assert await service.list_facts() == []


async def test_rejecting_a_pending_candidate_keeps_it_as_history(
    service: ConversationalFactService,
) -> None:
    result = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林"
    )

    rejected = await service.reject(result.candidate.id)

    assert rejected.status is FactCandidateStatus.REJECTED
    assert await service.pending() == []
    assert await service.list_facts() == []


async def test_a_credential_shaped_key_is_refused(service: ConversationalFactService) -> None:
    with pytest.raises(ForbiddenFactKey):
        await service.propose(
            key="profile.password", value="hunter2", correction_text="记住我的密码"
        )
    with pytest.raises(InvalidFactKey):
        await service.propose(key="Office", value="仙林", correction_text="记住")


async def test_showing_a_fact_needs_an_exact_key(
    service: ConversationalFactService,
) -> None:
    result = await service.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林"
    )

    assert await service.show("profile.office") is None  # proposed, not confirmed
    await service.confirm(result.candidate.id)
    shown = await service.show("profile.office")
    assert shown is not None and shown.value == "仙林校区"
    assert await service.show("profile.major") is None
