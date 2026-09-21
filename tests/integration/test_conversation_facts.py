"""Confirmed personal facts in a conversation (Phase 10F, ADR-0038).

Real runtime, real fact store, scripted provider. What is under test is the whole point of the
phase: a statement is not a memory, a proposal is not a fact, only the human's own explicit phrase
confirms one, and nothing about a fact can reach an external action.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assistant.domain.conversation import ConversationTurnStatus
from assistant.domain.fact import FactCandidateStatus
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    operation,
    plan,
)

OFFICE_KEY = "profile.office"
OFFICE_VALUE = "仙林校区"
REMEMBER_SENTENCE = "记住我的办公室在仙林校区。"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path)


async def _thread(harness: ConversationHarness):
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    return thread


def _counts(harness: ConversationHarness) -> dict[str, int]:
    connection = sqlite3.connect(str(harness.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "corrections",
                "fact_candidates",
                "confirmed_facts",
                "action_requests",
                "approvals",
                "execution_runs",
                "contacts",
            )
        }
    finally:
        connection.close()


def propose(**overrides: object) -> dict[str, object]:
    """One `fact.propose` operation with the schema's three fields."""
    arguments: dict[str, object] = {
        "key": OFFICE_KEY,
        "value": OFFICE_VALUE,
        "correction_text": REMEMBER_SENTENCE,
    }
    arguments.update(overrides)
    return operation("fact.propose", arguments)


async def _propose(harness: ConversationHarness, sentence: str = REMEMBER_SENTENCE):
    thread = await _thread(harness)
    harness.queue(plan(propose()))
    reply = await harness.service.send(thread.id, sentence)
    return thread, reply


# ------------------------------------------------------------------ proposal (§4, §7)


async def test_an_explicit_remember_creates_a_review_and_no_fact(
    harness: ConversationHarness,
) -> None:
    thread, reply = await _propose(harness)

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "我准备记录这条长期信息" in reply.text
    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in reply.text
    assert REMEMBER_SENTENCE.rstrip("。") in reply.text
    assert "确认记住" in reply.text
    counts = _counts(harness)
    assert counts["corrections"] == 1
    assert counts["fact_candidates"] == 1
    assert counts["confirmed_facts"] == 0
    assert counts["action_requests"] == 0
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    pending = await harness.facts.pending()
    assert [item.candidate.status for item in pending] == [
        FactCandidateStatus.PENDING
    ]
    assert thread.id is not None


async def test_the_preview_never_shows_internal_identifiers(
    harness: ConversationHarness,
) -> None:
    _, reply = await _propose(harness)
    candidate = (await harness.facts.pending())[0].candidate

    assert str(candidate.id) not in reply.text
    assert str(candidate.correction_id) not in reply.text
    assert str(candidate.id)[:8] not in reply.text


async def test_a_declarative_statement_creates_nothing(
    harness: ConversationHarness,
) -> None:
    """ "我的办公室在仙林" is a statement, not a memory request (ADR-0038 §2)."""
    thread = await _thread(harness)
    harness.queue(direct_reply("好的，我记下了这句话。"))

    await harness.service.send(thread.id, "我的办公室在仙林校区。")

    counts = _counts(harness)
    assert counts["corrections"] == 0
    assert counts["fact_candidates"] == 0
    assert counts["confirmed_facts"] == 0


async def test_a_forbidden_key_is_refused_before_anything_is_written(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(propose(key="profile.password", value="hunter2")))

    reply = await harness.service.send(thread.id, "记住我的密码是 hunter2。")

    assert reply.status is ConversationTurnStatus.FAILED
    assert "凭证" in reply.text
    counts = _counts(harness)
    assert counts["fact_candidates"] == 0
    assert counts["corrections"] == 0


# ---------------------------------------------------------------- confirmation (§5-§8)


async def test_a_generic_yes_does_not_confirm_a_fact(harness: ConversationHarness) -> None:
    thread, _ = await _propose(harness)
    harness.queue(direct_reply("要保存的话，请回复「确认记住」。"))

    reply = await harness.service.send(thread.id, "可以")

    assert _counts(harness)["confirmed_facts"] == 0
    assert len(await harness.facts.pending()) == 1
    assert "确认记住" in reply.text


async def test_an_explicit_phrase_confirms_exactly_one_fact(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _propose(harness)
    calls_before = len(harness.model.requests)

    reply = await harness.service.send(thread.id, "确认记住")

    assert "已记住" in reply.text
    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in reply.text
    # The confirmation is deterministic: no provider call was made for it.
    assert len(harness.model.requests) == calls_before
    counts = _counts(harness)
    assert counts["confirmed_facts"] == 1
    assert counts["action_requests"] == 0
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    assert await harness.facts.pending() == []

    harness.queue(direct_reply("这条已经保存过了。"))
    again = await harness.service.send(thread.id, "确认记住")

    assert _counts(harness)["confirmed_facts"] == 1
    assert "已经" in again.text or "没有" in again.text


async def test_cancelling_a_proposal_saves_nothing(harness: ConversationHarness) -> None:
    thread, _ = await _propose(harness)
    calls_before = len(harness.model.requests)

    reply = await harness.service.send(thread.id, "不要记")

    assert "没有保存" in reply.text
    assert len(harness.model.requests) == calls_before
    counts = _counts(harness)
    assert counts["confirmed_facts"] == 0
    assert await harness.facts.pending() == []
    history = await harness.learning.list_candidates(limit=None)
    assert [candidate.status for candidate in history] == [
        FactCandidateStatus.REJECTED
    ]


async def test_two_pending_facts_ask_which_one(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    await harness.facts.propose(
        key=OFFICE_KEY, value=OFFICE_VALUE, correction_text=REMEMBER_SENTENCE
    )
    await harness.facts.propose(
        key="profile.major",
        value="计算机科学",
        correction_text="记住我的专业是计算机科学",
    )
    calls_before = len(harness.model.requests)

    ambiguous = await harness.service.send(thread.id, "确认记住")

    assert "等待确认" in ambiguous.text
    assert OFFICE_KEY in ambiguous.text and "profile.major" in ambiguous.text
    assert _counts(harness)["confirmed_facts"] == 0
    assert len(harness.model.requests) == calls_before

    resolved = await harness.service.send(thread.id, f"确认记住 {OFFICE_KEY}")

    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in resolved.text
    facts = await harness.facts.list_facts()
    assert [fact.fact_key for fact in facts] == [OFFICE_KEY]
    assert len(harness.model.requests) == calls_before


async def test_a_key_that_is_not_waiting_is_reported(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _propose(harness)

    reply = await harness.service.send(thread.id, "确认记住 profile.major")

    assert "没有等待确认的" in reply.text
    assert OFFICE_KEY in reply.text
    assert _counts(harness)["confirmed_facts"] == 0


# ------------------------------------------------------------------- restart (§12)


async def test_a_pending_fact_survives_a_restart_and_is_never_auto_confirmed(
    tmp_path: Path,
) -> None:
    from assistant import bootstrap
    from assistant.adapters.model.fake import FakeModelAdapter

    harness = await build_harness(tmp_path)
    thread, _ = await _propose(harness)

    restarted = bootstrap.conversation_service(
        harness.database, harness.clock, harness.config, model=FakeModelAdapter()
    )
    resumed, was_resumed = await restarted.resume_or_start()
    notice = await restarted.pending_fact_review()

    assert was_resumed is True
    assert resumed.id == thread.id
    assert notice is not None
    assert OFFICE_VALUE in notice
    assert "确认记住" in notice
    assert _counts(harness)["confirmed_facts"] == 0

    confirmed = await restarted.send(thread.id, "确认记住")

    assert "已记住" in confirmed.text
    assert _counts(harness)["confirmed_facts"] == 1


# ------------------------------------------------------------------- correction (§8)


async def test_a_correction_supersedes_only_after_confirmation(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _propose(harness)
    await harness.service.send(thread.id, "确认记住")
    harness.queue(
        plan(
            propose(value="鼓楼校区", correction_text="不是仙林，是鼓楼。"),
        )
    )

    proposed = await harness.service.send(thread.id, "不是仙林，是鼓楼。")

    assert "我准备记录这条长期信息" in proposed.text
    assert "鼓楼校区" in proposed.text
    # Nothing changed yet: the old value is still the confirmed one.
    current = await harness.facts.list_facts()
    assert [fact.value for fact in current] == [OFFICE_VALUE]

    confirmed = await harness.service.send(thread.id, "确认记住")

    assert "已记住" in confirmed.text
    assert "原来的值" in confirmed.text
    current = await harness.facts.list_facts()
    assert [fact.value for fact in current] == ["鼓楼校区"]
    history = await harness.facts.list_facts(include_inactive=True, limit=None)
    assert {fact.value for fact in history} == {OFFICE_VALUE, "鼓楼校区"}
    assert _counts(harness)["confirmed_facts"] == 2


async def test_cancelling_a_correction_keeps_the_confirmed_value(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _propose(harness)
    await harness.service.send(thread.id, "确认记住")
    harness.queue(
        plan(propose(value="鼓楼校区", correction_text="不是仙林，是鼓楼。"))
    )
    await harness.service.send(thread.id, "不是仙林，是鼓楼。")

    reply = await harness.service.send(thread.id, "取消")

    assert "没有保存" in reply.text
    current = await harness.facts.list_facts()
    assert [fact.value for fact in current] == [OFFICE_VALUE]
    assert await harness.facts.pending() == []


# ------------------------------------------------------------------- queries (§9)


async def test_a_fact_question_is_answered_from_confirmed_rows_only(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("fact.show", {"key": OFFICE_KEY})))

    missing = await harness.service.send(thread.id, "你记得我的办公室在哪里吗？")

    assert "没有确认过" in missing.text

    await harness.facts.propose(
        key=OFFICE_KEY, value=OFFICE_VALUE, correction_text=REMEMBER_SENTENCE
    )
    harness.queue(plan(operation("fact.show", {"key": OFFICE_KEY})))

    proposed_only = await harness.service.send(thread.id, "你记得我的办公室在哪里吗？")

    # A *proposal* is not knowledge: the query reads confirmed facts only.
    assert "没有确认过" in proposed_only.text
    assert OFFICE_VALUE not in proposed_only.text

    await harness.facts.confirm((await harness.facts.pending())[0].candidate.id)
    harness.queue(plan(operation("fact.show", {"key": OFFICE_KEY})))

    answered = await harness.service.send(thread.id, "你记得我的办公室在哪里吗？")

    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in answered.text


async def test_listing_facts_reports_what_is_confirmed(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("fact.list", {})))

    empty = await harness.service.send(thread.id, "我有哪些长期信息？")

    assert "没有确认过任何长期信息" in empty.text

    await harness.facts.propose(
        key=OFFICE_KEY, value=OFFICE_VALUE, correction_text=REMEMBER_SENTENCE
    )
    await harness.facts.confirm((await harness.facts.pending())[0].candidate.id)
    harness.queue(plan(operation("fact.list", {})))

    listed = await harness.service.send(thread.id, "我有哪些长期信息？")

    assert f"{OFFICE_KEY}：{OFFICE_VALUE}" in listed.text


# ------------------------------------------------------------- boundaries (§10, §11)


async def test_a_contact_and_a_fact_are_different_records(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "contact.create",
                {"display_name": "张老师", "email_address": "zhang@example.edu"},
            )
        )
    )
    await harness.service.send(thread.id, "张老师邮箱是 zhang@example.edu，记成联系人。")

    counts = _counts(harness)
    assert counts["contacts"] == 1
    assert counts["fact_candidates"] == 0
    assert counts["confirmed_facts"] == 0

    harness.queue(plan(propose()))
    await harness.service.send(thread.id, "记住我的办公室在仙林校区。")

    counts = _counts(harness)
    assert counts["contacts"] == 1
    assert counts["fact_candidates"] == 1


async def test_a_fact_never_reaches_a_mail_recipient(
    harness: ConversationHarness,
) -> None:
    """Nothing reads facts into an outbound surface: the address still has to come from the user."""
    thread = await _thread(harness)
    await harness.facts.propose(
        key="profile.office_email",
        value="office@example.edu",
        correction_text="记住我的办公室邮箱是 office@example.edu",
    )
    await harness.facts.confirm((await harness.facts.pending())[0].candidate.id)
    harness.queue(plan(propose()))
    await harness.service.send(thread.id, "记住我的办公室在仙林校区。")
    await harness.service.send(thread.id, "确认记住")
    harness.queue(
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "测试",
                    "body": "你好",
                    "recipient_kind": "explicit_email",
                    "recipient_address": "office@example.edu",
                    "recipient_name": None,
                    "sender_account": None,
                    "draft_id": None,
                },
            )
        )
    )

    refused = await harness.service.send(thread.id, "给办公室发封邮件，说你好。")

    # The stored fact is not a recipient source: the address is not in the human's own words.
    assert refused.status is ConversationTurnStatus.FAILED
    assert _counts(harness)["action_requests"] == 0


async def test_the_model_cannot_confirm_a_fact_through_any_operation(
    harness: ConversationHarness,
) -> None:
    """`fact.confirm` is not in the vocabulary, so a model naming it is a refused answer."""
    thread = await _thread(harness)
    harness.queue(
        plan(operation("fact.confirm", {"candidate_id": "deadbeef"}))
    )

    reply = await harness.service.send(thread.id, "把这条长期信息保存下来。")

    assert reply.status is ConversationTurnStatus.FAILED
    assert _counts(harness)["confirmed_facts"] == 0


async def test_a_malformed_proposal_cannot_mutate_anything(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(
        plan(operation("fact.propose", {"key": OFFICE_KEY, "value": OFFICE_VALUE})),
        plan(operation("fact.propose", {"key": OFFICE_KEY, "value": OFFICE_VALUE})),
    )

    reply = await harness.service.send(thread.id, "记住我的办公室在仙林校区。")

    assert reply.status is ConversationTurnStatus.FAILED
    counts = _counts(harness)
    assert counts["fact_candidates"] == 0
    assert counts["confirmed_facts"] == 0
