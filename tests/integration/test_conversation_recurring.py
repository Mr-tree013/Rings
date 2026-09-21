"""Weekly commitments as a conversation (Phase 10D, ADR-0036).

The whole phase in one file: a sentence becomes one durable rule, a *statement* becomes a
question instead of a rule, a recurring commitment the build cannot express is refused, the
calendar listing shows derived occurrences, the deterministic planner arranges the week around
them, and nothing here creates an `ActionRequest`, an `Approval`, an `ExecutionRun`, a
`PlanBlock` chosen by the model, or a network connection — the suite-wide `no_network` guard in
`tests/conftest.py` makes the last one a hard failure rather than a promise.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.conversation_capabilities.registry import PreflightContext
from assistant.domain.conversation import ConversationTurnStatus
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.conversation_plan import ConversationOperationType, build_arguments
from assistant.domain.planning import PlanProposalStatus
from assistant.domain.recurring_calendar import RecurringCalendarRuleStatus
from tests.support.conversation import (
    CONFIG_WITHOUT_MAIL,
    CONFIG_WITHOUT_TIMEZONE,
    NOW,
    ConversationHarness,
    build_harness,
    clarification,
    direct_reply,
    operation,
    plan,
)

CLASS_TITLE = "计算机系统基础课"
CLASS_ARGS = {
    "title": CLASS_TITLE,
    "weekday": 1,
    "start_local_time": "10:00",
    "end_local_time": "12:00",
}
CLASS_SENTENCE = "每周一早上十点到十二点是计算机系统基础课，记下来。"

MONDAY_10_SHANGHAI = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
MONDAY_12_SHANGHAI = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
"""Monday 10:00-12:00 Asia/Shanghai is 02:00-04:00 UTC — the class in every assertion below."""


def create_weekly(**overrides: object) -> dict[str, object]:
    """One `calendar.recurring.create_weekly` operation, as the provider's schema requires it.

    Every nullable field is required by the wire schema, so a scripted answer states them all
    explicitly — exactly as the real provider does.
    """
    return {
        "title": CLASS_TITLE,
        "weekday": 1,
        "start_local_time": "10:00",
        "end_local_time": "12:00",
        "timezone": None,
        "starts_on": None,
        "ends_on": None,
        **overrides,
    }


def edit_rule(rule_id: str, **overrides: object) -> dict[str, object]:
    """One `calendar.recurring.edit` operation with the fields the user did not change as null."""
    return {
        "rule_id": rule_id,
        "title": None,
        "weekday": None,
        "start_local_time": None,
        "end_local_time": None,
        **overrides,
    }


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path)


async def _thread(harness: ConversationHarness):
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    return thread


def _counts(harness: ConversationHarness) -> dict[str, int]:
    """Row counts for the state a local recurring write must never touch."""
    connection = sqlite3.connect(str(harness.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "action_requests",
                "approvals",
                "execution_runs",
                "corrections",
                "confirmed_facts",
                "playbooks",
            )
        }
    finally:
        connection.close()


def _context_json(harness: ConversationHarness, index: int = -1) -> str:
    """The context the model was given on one turn, exactly as it was sent."""
    return harness.model.requests[index].messages[0].content


# ------------------------------------------------------------------ creating a rule (§14)


async def test_an_explicit_instruction_creates_exactly_one_weekly_rule(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.status is ConversationTurnStatus.COMPLETED
    rules = await harness.recurring.list_active()
    assert len(rules) == 1
    rule = rules[0]
    assert rule.title == CLASS_TITLE
    assert rule.weekday == 1
    assert rule.start_time.strftime("%H:%M") == "10:00"
    assert rule.end_time.strftime("%H:%M") == "12:00"
    # The runtime-owned planning timezone, and no host timezone anywhere near it.
    assert rule.timezone == "Asia/Shanghai"
    assert rule.starts_on == date(2026, 9, 21)
    assert rule.ends_on is None
    assert rule.status is RecurringCalendarRuleStatus.ACTIVE
    assert "已加入固定安排" in reply.text
    assert "每周一 10:00–12:00" in reply.text
    assert CLASS_TITLE in reply.text
    assert "从 2026-09-21 起持续到你删除" in reply.text
    assert all(count == 0 for count in _counts(harness).values())


async def test_the_same_exact_request_twice_leaves_one_rule(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    first = await harness.service.send(thread.id, CLASS_SENTENCE)
    second = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert "已加入固定安排" in first.text
    assert len(await harness.recurring.list_active()) == 1
    assert "已经存在" in second.text
    assert "没有重复添加" in second.text


async def test_two_weekdays_are_two_rules_and_repeating_adds_none(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    sentence = "每周一和周三下午两点到四点都有软件工程课，记录下来。"
    scripts = [
        plan(
            *(
                operation(
                    "calendar.recurring.create_weekly",
                    create_weekly(
                        title="软件工程课",
                        weekday=weekday,
                        start_local_time="14:00",
                        end_local_time="16:00",
                    ),
                )
                for weekday in (1, 3)
            )
        )
    ]
    for script in scripts * 2:
        harness.queue(script)

    await harness.service.send(thread.id, sentence)
    again = await harness.service.send(thread.id, sentence)

    rules = await harness.recurring.list_active()
    assert [rule.weekday for rule in rules] == [1, 3]
    assert {rule.title for rule in rules} == {"软件工程课"}
    assert {rule.timezone for rule in rules} == {"Asia/Shanghai"}
    assert "已经存在" in again.text
    assert len(rules) == 2


# -------------------------------------------------------- a statement is not a mutation (§19)


async def test_a_declared_class_asks_before_it_becomes_durable(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    asked = await harness.service.send(thread.id, "我每周一十点到十二点有课。")

    assert asked.waiting_for_confirmation is True
    assert asked.status is ConversationTurnStatus.WAITING_CONFIRMATION
    assert "要把这些加入固定安排吗" in asked.text
    assert "每周一 10:00–12:00" in asked.text
    assert await harness.recurring.list_active() == []

    remaining = len(harness.model.responses)
    confirmed = await harness.service.send(thread.id, "可以")

    assert len(harness.model.responses) == remaining, "a confirmation is settled without a model"
    assert confirmed.status is ConversationTurnStatus.COMPLETED
    assert "已加入固定安排" in confirmed.text
    assert len(await harness.recurring.list_active()) == 1


async def test_a_declared_class_can_also_be_declined(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    await harness.service.send(thread.id, "我每周一十点到十二点有课。")
    rejected = await harness.service.send(thread.id, "取消")

    assert await harness.recurring.list_active() == []
    assert rejected.status is ConversationTurnStatus.COMPLETED
    assert "没有" in rejected.text or "取消" in rejected.text


async def test_an_explicit_instruction_is_not_held_for_a_second_yes(
    harness: ConversationHarness,
) -> None:
    """「记下来」 is the user's own instruction, so the local write happens in that turn."""
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.waiting_for_confirmation is False
    assert len(await harness.recurring.list_active()) == 1


# --------------------------------------------------------------- unsupported recurrence (§12)


@pytest.mark.parametrize(
    ("sentence", "named"),
    (
        ("每周一和周三的单双周课，记下来。", "单双周"),
        ("每两周一次计算机系统基础课，记下来。", "两周一次"),
        ("每个月第一周周一十点有课，记下来。", "每个月"),
        ("每周一十点有课，节假日除外，记下来。", "节假日"),
        ("考试周除外，每周一十点有课，记下来。", "考试周"),
        ("每周一十点有课，月末那周不一定，记下来。", "月末"),
    ),
)
async def test_unsupported_recurrence_is_refused_with_no_mutation(
    harness: ConversationHarness, sentence: str, named: str
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    reply = await harness.service.send(thread.id, sentence)

    assert reply.status is ConversationTurnStatus.FAILED
    assert reply.error_code is ConversationErrorCode.UNSUPPORTED_SEMANTICS
    assert named in reply.text
    assert "还不能记" in reply.text
    assert await harness.recurring.list_active() == []
    assert await harness.recurring.list_rules(include_retired=True) == []
    turns = await harness.conversations.list_turns(thread.id)
    stored = await harness.conversations.list_operations(turns[-1].id)
    assert [entry.status.value for entry in stored] == ["rejected"], "nothing ran"


# ----------------------------------------------------------------- missing timezone (§6-§8)


async def test_missing_planning_timezone_asks_instead_of_guessing(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path, config_body=CONFIG_WITHOUT_TIMEZONE)
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "时区" in reply.text
    assert await harness.recurring.list_active() == []
    assert all(count == 0 for count in _counts(harness).values())


async def test_a_stated_timezone_works_without_a_planning_timezone(tmp_path: Path) -> None:
    """An explicit IANA zone is complete on its own; nothing about the host is consulted."""
    harness = await build_harness(tmp_path, config_body=CONFIG_WITHOUT_TIMEZONE)
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "calendar.recurring.create_weekly",
                create_weekly(timezone="Asia/Tokyo"),
            )
        )
    )

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.status is ConversationTurnStatus.COMPLETED
    rules = await harness.recurring.list_active()
    assert [rule.timezone for rule in rules] == ["Asia/Tokyo"]


async def test_the_preflight_for_a_new_rule_needs_a_timezone(tmp_path: Path) -> None:
    """The refusal is the runtime's own check, not the model's good manners."""
    harness = await build_harness(tmp_path, config_body=CONFIG_WITHOUT_TIMEZONE)
    registry = bootstrap.conversation_capabilities(
        harness.database, harness.clock, harness.config, model=FakeModelAdapter()
    )
    capability = registry.require(
        ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY
    )
    assert capability.preflight is not None

    refusal = await capability.preflight(
        build_arguments("calendar.recurring.create_weekly", CLASS_ARGS),
        PreflightContext(),
    )

    assert refusal is not None
    assert "时区" in refusal
    assert await harness.recurring.list_active() == []


async def test_an_unknown_rule_reference_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    registry = bootstrap.conversation_capabilities(
        harness.database, harness.clock, harness.config, model=FakeModelAdapter()
    )
    capability = registry.require(ConversationOperationType.CALENDAR_RECURRING_RETIRE)
    assert capability.preflight is not None

    refusal = await capability.preflight(
        build_arguments("calendar.recurring.retire", {"rule_id": "deadbeef"}),
        PreflightContext(),
    )

    assert refusal is not None
    assert "找不到" in refusal


# --------------------------------------------------------------------- listing (§10, §18)


async def test_listing_fixed_arrangements_reports_the_rule_not_its_occurrences(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    harness.queue(plan(operation("calendar.recurring.list", {"include_retired": False})))

    reply = await harness.service.send(thread.id, "我有哪些固定安排？")

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "1 条固定安排" in reply.text
    assert "每周一 10:00–12:00" in reply.text
    assert CLASS_TITLE in reply.text
    rule = (await harness.recurring.list_active())[0]
    assert str(rule.id) not in reply.text
    assert rule.fingerprint not in reply.text


async def test_calendar_listing_shows_derived_weekly_occurrences(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    harness.queue(plan(operation("calendar.list", {"days": 7})))

    reply = await harness.service.send(thread.id, "下周一上午有什么安排？")

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "周一 10:00–12:00" in reply.text
    assert CLASS_TITLE in reply.text
    assert "（每周）" in reply.text
    # Derived, never stored: no occurrence row exists to be stale or to leak an identity.
    assert (
        await harness.commitments.list_calendar_events(
            query_start=NOW, query_end=NOW + timedelta(days=60)
        )
        == []
    )


async def test_the_recent_entities_bound_and_shape_are_what_a_follow_up_needs(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    created = await harness.service.send(thread.id, CLASS_SENTENCE)
    rule = (await harness.recurring.list_active())[0]
    short_id = str(rule.id)[:8]
    harness.queue(plan(operation("calendar.recurring.list", {"include_retired": False})))

    await harness.service.send(thread.id, "我有哪些固定安排？")

    context = json.loads(_context_json(harness))["context"]
    entities = [
        entry
        for entry in context["recent_entities"]
        if entry["kind"] == "recurring_calendar_rule"
    ]
    assert len(entities) == 1
    entry = entities[0]
    assert entry["id"] == short_id
    assert entry["label"] == CLASS_TITLE
    assert "weekday=1" in entry["detail"]
    assert "start=10:00" in entry["detail"]
    assert "end=12:00" in entry["detail"]
    assert "timezone=Asia/Shanghai" in entry["detail"]
    assert "starts_on=2026-09-21" in entry["detail"]
    assert "status=active" in entry["detail"]
    # No occurrences, no fingerprint, no database internals travel with the rule.
    assert rule.fingerprint not in _context_json(harness)
    assert "2026-09-28" not in _context_json(harness)
    assert "已加入固定安排" in created.text


# ------------------------------------------------------------------------- edit (§15)


async def test_editing_a_rule_keeps_its_identity_and_moves_its_future(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    before = (await harness.recurring.list_active())[0]
    future_before = before.expand(
        window_start=NOW + timedelta(days=1), window_end=NOW + timedelta(days=40)
    )
    short_id = str(before.id)[:8]
    harness.queue(
        plan(
            operation(
                "calendar.recurring.edit",
                edit_rule(short_id, start_local_time="09:00", end_local_time="11:00"),
            )
        )
    )

    reply = await harness.service.send(thread.id, "把刚才那门课改成九点到十一点。")

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "已更新固定安排" in reply.text
    after = (await harness.recurring.list_active())[0]
    assert after.id == before.id
    assert after.created_at == before.created_at
    assert after.fingerprint != before.fingerprint
    assert after.start_time.strftime("%H:%M") == "09:00"
    assert after.end_time.strftime("%H:%M") == "11:00"
    assert after.title == CLASS_TITLE
    assert after.timezone == before.timezone
    future_after = after.expand(
        window_start=NOW + timedelta(days=1), window_end=NOW + timedelta(days=40)
    )
    assert [item.starts_at for item in future_after] != [
        item.starts_at for item in future_before
    ]
    assert future_after[0].starts_at.astimezone(UTC).hour == 1  # 09:00 +08:00 is 01:00 UTC


async def test_an_edit_never_rewrites_history(harness: ConversationHarness) -> None:
    """Work already recorded keeps the minutes it was recorded with."""
    thread = await _thread(harness)
    task = await harness.create_task("写报告", estimated_minutes=120)
    work = bootstrap.work_service(harness.database, harness.clock, harness.config)
    session = await work.record_session(
        task_id=task.id,
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=1),
    )
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    rule = (await harness.recurring.list_active())[0]

    harness.queue(
        plan(
            operation(
                "calendar.recurring.edit",
                edit_rule(
                    str(rule.id)[:8], start_local_time="09:00", end_local_time="11:00"
                ),
            )
        )
    )
    await harness.service.send(thread.id, "把刚才那门课改成九点到十一点。")

    assert await work.list_task_sessions(task.id) == [session]


# ------------------------------------------------------------------------ retire (§16)


async def test_retiring_a_rule_stops_the_future_and_keeps_the_rule(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    rule = (await harness.recurring.list_active())[0]
    harness.queue(
        plan(
            operation(
                "calendar.recurring.retire", {"rule_id": str(rule.id)[:8]}
            )
        )
    )

    reply = await harness.service.send(thread.id, "以后周一没有这门课了。")

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "以后周一不再有" in reply.text
    assert await harness.recurring.list_active() == []
    retired = await harness.recurring.list_rules(include_retired=True)
    assert len(retired) == 1
    assert retired[0].id == rule.id
    assert retired[0].status is RecurringCalendarRuleStatus.RETIRED
    assert retired[0].retired_at is not None
    assert (
        await harness.recurring.expand_range(
            window_start=NOW, window_end=NOW + timedelta(days=60)
        )
        == ()
    )


# ---------------------------------------------------------------- planner integration (§11)


async def test_a_plan_block_is_never_proposed_over_a_weekly_class(
    harness: ConversationHarness,
) -> None:
    await harness.create_task("写报告", estimated_minutes=600)
    await harness.recurring.create_weekly(
        title=CLASS_TITLE, weekday=1, start="10:00", end="12:00"
    )

    detail = await harness.planner.create_week_proposal(next_week=True)

    blocks = [
        (block.starts_at, block.ends_at)
        for block in detail.blocks
    ]
    assert blocks, "the planner had a task to place"
    class_start = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    class_end = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
    assert [block for block in blocks if block[0] < class_end and block[1] > class_start] == []


async def test_the_planner_merges_recurring_time_with_overlapping_events(
    harness: ConversationHarness,
) -> None:
    """An event that repeats the class exactly is one busy interval, not two."""
    from assistant.application.calendar_service import CreateCalendarEvent
    from assistant.application.planner_service import busy_intervals
    from assistant.domain.planning import PlanningWindow

    await harness.calendar.create_event(
        CreateCalendarEvent(
            title=CLASS_TITLE,
            starts_at=MONDAY_10_SHANGHAI + timedelta(minutes=30),
            ends_at=MONDAY_12_SHANGHAI - timedelta(minutes=30),
        )
    )
    await harness.recurring.create_weekly(
        title=CLASS_TITLE, weekday=1, start="10:00", end="12:00"
    )
    snapshot = await harness.planning.load_snapshot(
        PlanningWindow(
            starts_at=datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
            timezone="Asia/Shanghai",
        )
    )

    merged = busy_intervals(snapshot, ((MONDAY_10_SHANGHAI, MONDAY_12_SHANGHAI),))

    assert len(merged) == 1, "an overlapping event and class are one busy interval"
    assert merged[0][0] == MONDAY_10_SHANGHAI
    assert merged[0][1] == MONDAY_12_SHANGHAI


# --------------------------------------------------------- the real course + plan workflow


async def test_the_original_described_workflow_end_to_end(harness: ConversationHarness) -> None:
    """§12: one message creates the class *and* proposes the week, with one exact yes at the end."""
    thread = await _thread(harness)
    await harness.create_task("写报告", estimated_minutes=180)
    harness.queue(
        plan(
            operation("calendar.recurring.create_weekly", create_weekly()),
            operation("plan.propose_week", {"next_week": True}),
        )
    )

    reply = await harness.service.send(
        thread.id,
        "做一下周计划，把我的每周的课放进去。\n周一早上十点到十二点是计算机系统基础课",
    )

    assert reply.status is ConversationTurnStatus.WAITING_CONFIRMATION
    assert reply.waiting_for_confirmation is True
    assert "已加入固定安排" in reply.text
    assert "每周一 10:00–12:00" in reply.text
    assert "我已经据此生成下周计划" not in reply.text  # the runtime does not narrate a promise
    assert "要应用这个计划吗" in reply.text
    assert len(await harness.recurring.list_active()) == 1
    proposals = [
        summary
        for summary in await harness.planning.list_proposals()
        if summary.status is PlanProposalStatus.PENDING
    ]
    assert len(proposals) == 1
    before = await harness.commitments.list_plan_blocks_in_range(
        query_start=NOW, query_end=NOW + timedelta(days=21)
    )
    assert before == []

    remaining = len(harness.model.responses)
    applied = await harness.service.send(thread.id, "可以")

    assert len(harness.model.responses) == remaining
    assert applied.status is ConversationTurnStatus.COMPLETED
    assert "已应用周计划提案" in applied.text
    blocks = await harness.commitments.list_plan_blocks_in_range(
        query_start=NOW, query_end=NOW + timedelta(days=21)
    )
    assert blocks
    class_start = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    class_end = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
    assert [b for b in blocks if b.starts_at < class_end and b.ends_at > class_start] == []
    assert all(count == 0 for count in _counts(harness).values())

    harness.queue(direct_reply("这个计划已经应用过了。"))
    second_yes = await harness.service.send(thread.id, "可以")

    assert "已应用周计划提案" not in second_yes.text
    assert len(
        await harness.commitments.list_plan_blocks_in_range(
            query_start=NOW, query_end=NOW + timedelta(days=21)
        )
    ) == len(blocks)


async def test_a_plan_that_cannot_be_carried_out_writes_no_rule(
    harness: ConversationHarness,
) -> None:
    """Preflight is whole-plan: the class is not saved when the rest cannot run (§13)."""
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation("calendar.recurring.create_weekly", create_weekly()),
            operation("plan.apply_proposal", {"proposal_id": None}),
        )
    )

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.status is ConversationTurnStatus.FAILED
    assert "没有待审阅的周计划提案" in reply.text
    assert await harness.recurring.list_active() == []


async def test_a_failure_after_a_successful_write_is_reported_as_partial(
    tmp_path: Path,
) -> None:
    """§13: never claim nothing happened when the class was already saved."""
    harness = await build_harness(tmp_path, config_body=CONFIG_WITHOUT_MAIL)
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation("calendar.recurring.create_weekly", create_weekly()),
            operation("mail.sync", {"account_id": None}),
        )
    )

    reply = await harness.service.send(thread.id, CLASS_SENTENCE)

    assert reply.status is ConversationTurnStatus.FAILED
    assert len(await harness.recurring.list_active()) == 1
    assert "已加入固定安排" in reply.text
    assert "mail.sync" in reply.text


# ---------------------------------------------------------------- restart and timezone


async def test_a_rule_survives_a_restart_and_still_expands(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)

    restarted = bootstrap.conversation_service(
        harness.database, harness.clock, harness.config, model=FakeModelAdapter()
    )
    resumed, was_resumed = await restarted.resume_or_start()
    recurring = bootstrap.recurring_calendar_service(
        harness.database, harness.clock, harness.config
    )

    assert was_resumed is True
    assert resumed.id == thread.id
    rules = await recurring.list_active()
    assert len(rules) == 1
    occurrences = await recurring.expand_range(
        window_start=NOW, window_end=NOW + timedelta(days=14)
    )
    assert [item.starts_at for item in occurrences] == [
        MONDAY_10_SHANGHAI,
        MONDAY_10_SHANGHAI + timedelta(days=7),
    ]
    assert [item.ends_at for item in occurrences] == [
        MONDAY_12_SHANGHAI,
        MONDAY_12_SHANGHAI + timedelta(days=7),
    ]


async def test_the_host_timezone_cannot_change_what_a_rule_means(
    harness: ConversationHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = await _thread(harness)
    harness.queue(plan(operation("calendar.recurring.create_weekly", create_weekly())))
    await harness.service.send(thread.id, CLASS_SENTENCE)
    window_start = NOW
    window_end = NOW + timedelta(days=14)
    observed: list[list[datetime]] = []

    for host_zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        monkeypatch.setenv("TZ", host_zone)
        occurrences = await harness.recurring.expand_range(
            window_start=window_start, window_end=window_end
        )
        observed.append([item.starts_at.astimezone(UTC) for item in occurrences])

    assert observed[0] == observed[1] == observed[2]
    assert observed[0] == [MONDAY_10_SHANGHAI, MONDAY_10_SHANGHAI + timedelta(days=7)]
    assert all(rule.timezone == "Asia/Shanghai" for rule in await harness.recurring.list_active())


async def test_a_clarification_about_a_rule_is_never_a_write(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue(clarification("你说的「刚才那门课」是哪一条固定安排？"))

    reply = await harness.service.send(thread.id, "把刚才那门课改成九点到十一点。")

    assert reply.status is ConversationTurnStatus.COMPLETED
    assert "固定安排" in reply.text
    assert await harness.recurring.list_rules(include_retired=True) == []


async def test_a_direct_reply_about_a_rule_creates_nothing(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue(direct_reply("好的，我记住了。"))

    await harness.service.send(thread.id, "我每周一十点到十二点有课。")

    assert await harness.recurring.list_rules(include_retired=True) == []
