"""One human "可以" settles one pending local group (ADR-0040 §14-§19).

The defect this file pins was a real transcript: a three-class schedule was proposed, the user
revised one class's location, and the confirmation then applied *both* the original and the
revised proposal. The cause was structural — every waiting operation in the thread was treated as
one batch — so these tests are written at the runtime level, against a real migrated database,
with only the provider scripted.

The rule they enforce:

```text
one turn ──► one confirmation group
revision of the same kind ──► the older group stops being live
different kinds waiting at once ──► Tree asks which one, and applies neither
```
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from assistant.domain.conversation import (
    ConversationOperationStatus,
    ConversationTurnStatus,
)
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    operation,
    plan,
)

GENERATIVE_SE = "生成式软件工程（网课）"
ICS = "计算机系统基础（ICS）"
HISTORY = "中国近代史纲要（仙二-404）"
ICS_WITH_ROOM = "计算机系统基础（ICS）（仙一107）"


def weekly(title: str, *, weekday: int = 2, start: str, end: str) -> dict[str, object]:
    """One `calendar.recurring.create_weekly` operation, with every nullable field stated."""
    return operation(
        "calendar.recurring.create_weekly",
        {
            "title": title,
            "weekday": weekday,
            "start_local_time": start,
            "end_local_time": end,
            "timezone": None,
            "starts_on": None,
            "ends_on": None,
        },
    )


def three_classes(*, ics_title: str = ICS) -> str:
    """The exact three-course proposal, optionally with the revised ICS title."""
    return plan(
        weekly(GENERATIVE_SE, start="10:00", end="12:00"),
        weekly(ics_title, start="14:00", end="16:00"),
        weekly(HISTORY, start="18:30", end="21:20"),
    )


async def _thread(harness: ConversationHarness):
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    return thread


def _non_user_rows(harness: ConversationHarness) -> dict[str, int]:
    """Rows a local confirmation must never create."""
    connection = sqlite3.connect(str(harness.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("action_requests", "approvals", "execution_runs")
        }
    finally:
        connection.close()


async def _waiting(harness: ConversationHarness, thread_id) -> list:
    return await harness.conversations.operations_waiting_for_confirmation(thread_id)


# --------------------------------------------------- revising a recurring proposal (§14, §16)


async def test_revising_a_proposal_retires_the_older_group(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(three_classes())
    first = await harness.service.send(
        thread.id, "记录我周二的课表：生成式软件工程、ICS、中国近代史纲要"
    )
    assert first.waiting_for_confirmation is True
    first_turn = (await harness.conversations.list_turns(thread.id))[-1]
    original = await harness.conversations.list_operations(first_turn.id)
    assert len(original) == 3

    harness.queue(three_classes(ics_title=ICS_WITH_ROOM))
    revised = await harness.service.send(thread.id, "ics的地点是在仙一107")

    assert revised.waiting_for_confirmation is True
    waiting = await _waiting(harness, thread.id)
    assert len(waiting) == 3, "only the revised group is live"
    assert {str(entry.turn_id) for entry in waiting} == {revised.turn_id}
    for entry in original:
        stored = await harness.conversations.get_operation(entry.id)
        assert stored is not None
        assert stored.status is ConversationOperationStatus.REJECTED
    refreshed = await harness.conversations.get_turn(first_turn.id)
    assert refreshed is not None
    assert refreshed.status is ConversationTurnStatus.COMPLETED


async def test_confirming_the_revision_creates_only_the_revised_rules(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(three_classes())
    await harness.service.send(
        thread.id, "记录我周二的课表：生成式软件工程、ICS、中国近代史纲要"
    )
    harness.queue(three_classes(ics_title=ICS_WITH_ROOM))
    await harness.service.send(thread.id, "ics的地点是在仙一107")

    remaining = len(harness.model.responses)
    confirmed = await harness.service.send(thread.id, "可以")

    assert len(harness.model.responses) == remaining, "a confirmation never asks the model"
    assert confirmed.status is ConversationTurnStatus.COMPLETED
    rules = await harness.recurring.list_active()
    assert [rule.title for rule in rules] == [GENERATIVE_SE, ICS_WITH_ROOM, HISTORY]
    assert [rule.weekday for rule in rules] == [2, 2, 2]
    titles = {rule.title for rule in rules}
    assert ICS not in titles, "the superseded proposal must not have been applied"
    assert all(count == 0 for count in _non_user_rows(harness).values())


async def test_a_superseded_group_can_never_be_applied_later(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(three_classes())
    await harness.service.send(
        thread.id, "记录我周二的课表：生成式软件工程、ICS、中国近代史纲要"
    )
    harness.queue(three_classes(ics_title=ICS_WITH_ROOM))
    await harness.service.send(thread.id, "ics的地点是在仙一107")

    harness.queue(direct_reply("这个改动已经记下了。"))
    await harness.service.send(thread.id, "可以")
    second = await harness.service.send(thread.id, "可以")

    assert "已加入固定安排" not in second.text
    rules = await harness.recurring.list_active()
    assert len(rules) == 3


async def test_an_unrelated_confirmation_group_delays_nothing(
    harness: ConversationHarness,
) -> None:
    """A newer recurring proposal does not disturb a plan waiting for its own yes."""
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    offered = await harness.service.send(thread.id, "帮我安排一下这周")
    assert offered.waiting_for_confirmation is True

    harness.queue(three_classes())
    proposed = await harness.service.send(thread.id, "我周二十点到十二点有课")

    assert proposed.waiting_for_confirmation is True
    waiting = await _waiting(harness, thread.id)
    assert len(waiting) == 4, "one plan offer plus one three-rule proposal stay live"


# -------------------------------------------------------- two unrelated groups (§15, §19)


async def test_one_yes_cannot_settle_two_unrelated_groups(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    await harness.service.send(thread.id, "帮我安排一下这周")
    harness.queue(three_classes())
    await harness.service.send(thread.id, "我周二十点到十二点有课")
    assert len(await _waiting(harness, thread.id)) == 4

    remaining = len(harness.model.responses)
    answer = await harness.service.send(thread.id, "可以")

    assert len(harness.model.responses) == remaining, "ambiguity is resolved without the model"
    assert "请告诉我是哪一个" in answer.text
    assert await harness.recurring.list_active() == [], "nothing recurring was applied"
    blocks = await harness.commitments.list_plan_blocks_in_range(
        query_start=harness.clock.now(), query_end=harness.clock.now()
    )
    assert blocks == [], "nothing was applied to the calendar either"
    assert len(await _waiting(harness, thread.id)) == 4, "both groups are still live"


async def test_cancelling_clears_every_pending_group_without_applying_any(
    harness: ConversationHarness,
) -> None:
    """`取消` applies nothing, so it needs no target — it can only withdraw questions."""
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    await harness.service.send(thread.id, "帮我安排一下这周")
    harness.queue(three_classes())
    await harness.service.send(thread.id, "我周二十点到十二点有课")

    cancelled = await harness.service.send(thread.id, "取消")

    assert cancelled.status is ConversationTurnStatus.COMPLETED
    assert "没有应用" in cancelled.text
    assert await _waiting(harness, thread.id) == []
    assert await harness.recurring.list_active() == []
    assert _non_user_rows(harness) == {
        "action_requests": 0,
        "approvals": 0,
        "execution_runs": 0,
    }


# ------------------------------------------------------- plan-apply confirmation (§15, §18)


async def test_a_second_plan_offer_supersedes_the_first(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    first = await harness.service.send(thread.id, "帮我安排一下这周")
    assert first.waiting_for_confirmation is True
    first_turn = (await harness.conversations.list_turns(thread.id))[-1]
    first_offer = await harness.conversations.list_operations(first_turn.id)
    assert [entry.status.value for entry in first_offer] == ["applied", "waiting_confirmation"]

    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    second = await harness.service.send(thread.id, "再重新安排一次这周")

    assert second.waiting_for_confirmation is True
    waiting = await _waiting(harness, thread.id)
    assert len(waiting) == 1, "the newer offer replaced the older one"
    assert str(waiting[0].turn_id) == second.turn_id
    retired = await harness.conversations.get_operation(first_offer[1].id)
    assert retired is not None
    assert retired.status is ConversationOperationStatus.REJECTED


async def test_the_confirmed_plan_is_the_newest_offer(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    await harness.service.send(thread.id, "帮我安排一下这周")
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    await harness.service.send(thread.id, "再重新安排一次这周")
    waiting = await _waiting(harness, thread.id)

    applied = await harness.service.send(thread.id, "可以")

    assert applied.status is ConversationTurnStatus.COMPLETED
    assert "已应用周计划提案" in applied.text
    applied_row = await harness.conversations.get_operation(waiting[0].id)
    assert applied_row is not None
    assert applied_row.status is ConversationOperationStatus.APPLIED
    assert [entry.status.value for entry in await _waiting(harness, thread.id)] == []


# --------------------------------------------------------------- result rendering (§17)


async def test_confirming_a_group_reads_as_one_answer(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(three_classes())
    await harness.service.send(thread.id, "我周二十点、下午两点和晚上六点半都有课")

    confirmed = await harness.service.send(thread.id, "可以")

    lines = confirmed.text.splitlines()
    assert lines[0] == "已加入固定安排："
    assert lines[1:4] == [
        f"- 每周二 10:00–12:00 · {GENERATIVE_SE}",
        f"- 每周二 14:00–16:00 · {ICS}",
        f"- 每周二 18:30–21:20 · {HISTORY}",
    ]
    assert confirmed.text.count("已加入固定安排") == 1
    assert "从 2026-09-21 起持续到你删除。" in confirmed.text


async def test_a_partly_existing_group_says_so_in_one_sentence(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    # Two of the three rules already exist before the confirmation.
    await harness.recurring.create_weekly(
        title=GENERATIVE_SE, weekday=2, start="10:00", end="12:00"
    )
    await harness.recurring.create_weekly(title=ICS, weekday=2, start="14:00", end="16:00")
    harness.queue(three_classes())
    await harness.service.send(thread.id, "我周二十点、下午两点和晚上六点半都有课")

    confirmed = await harness.service.send(thread.id, "可以")

    assert "已加入固定安排：" in confirmed.text
    assert "其中 2 条已存在，没有重复添加。" in confirmed.text
    assert confirmed.text.count("已加入固定安排") == 1
    rules = await harness.recurring.list_active()
    assert len(rules) == 3
    assert date(2026, 9, 21) in {rule.starts_on for rule in rules}


async def test_a_group_that_already_existed_entirely_says_so_once(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    for title, start, end in (
        (GENERATIVE_SE, "10:00", "12:00"),
        (ICS, "14:00", "16:00"),
        (HISTORY, "18:30", "21:20"),
    ):
        await harness.recurring.create_weekly(
            title=title, weekday=2, start=start, end=end
        )
    harness.queue(three_classes())
    await harness.service.send(thread.id, "我周二十点、下午两点和晚上六点半都有课")

    confirmed = await harness.service.send(thread.id, "可以")

    assert confirmed.text.startswith("这些固定安排已经存在，我没有重复添加：")
    assert len(await harness.recurring.list_active()) == 3


# ------------------------------------------------------------------ facts and mail (§18)


async def test_a_fact_confirmation_still_needs_its_own_words(
    harness: ConversationHarness,
) -> None:
    """A generic `可以` neither confirms a fact nor is changed by this hotfix."""
    harness.queue(
        plan(
            operation(
                "fact.propose",
                {"key": "office", "value": "仙林", "correction_text": "我的办公室在仙林"},
            )
        )
    )
    thread = await _thread(harness)
    await harness.service.send(thread.id, "记住我的办公室在仙林")
    assert await harness.facts.pending() != []

    harness.queue(direct_reply("你想让我记住什么？"))
    await harness.service.send(thread.id, "可以")

    assert await harness.learning.get_active_fact("office") is None
    assert await harness.facts.pending() != [], "the proposal is still waiting for its own words"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path)
