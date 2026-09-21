"""Attention as a conversation operation: list, acknowledge, dismiss (ADR-0042 §12).

The model may ask for the inbox and may settle one item the human pointed at. It may not execute,
approve, send or submit anything, and the vocabulary test at the bottom of this file is what keeps
that true as the operation list grows.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.application.conversation_capabilities import ConfirmationPolicy
from assistant.domain.attention import AttentionStatus
from assistant.domain.conversation_plan import ConversationOperationType
from tests.support.conversation import build_harness, operation, plan


@pytest.fixture
async def harness(tmp_path: Path):
    built = await build_harness(tmp_path)
    task = await built.create_task("写报告")
    await built.tasks.set_deadline(task.id, built.clock.now() - timedelta(hours=2))
    return built


async def _service(harness):
    return bootstrap.attention_service(harness.database, harness.clock)


async def test_listing_the_inbox_speaks_product_language(harness) -> None:
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("attention.list", {"include_settled": False})))

    reply = await harness.service.send(thread.id, "最近有什么需要我处理的？")

    assert "需要你处理" in reply.text
    assert "写报告" in reply.text
    # No internal vocabulary reaches the user: not a kind constant, not an English reminder title.
    for leak in ("task_overdue", "plan_ready", "WAITING_CONFIRMATION", "ActionRequest"):
        assert leak not in reply.text


async def test_an_empty_inbox_says_so(harness) -> None:
    for task in await harness.commitments.list_tasks(statuses=None):
        await harness.tasks.complete_task(task.id)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("attention.list", {"include_settled": False})))

    reply = await harness.service.send(thread.id, "有什么需要我处理的？")

    assert "没有需要你处理的事情" in reply.text


async def test_acknowledging_settles_the_reminder_and_not_the_task(harness) -> None:
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("attention.acknowledge", {"reference": "写报告"})))

    reply = await harness.service.send(thread.id, "这个我知道了")

    assert "标记为已经看过" in reply.text
    live = await (await _service(harness)).list_live()
    assert [item.status for item in live.items] == [AttentionStatus.ACKNOWLEDGED]
    # The task behind it is untouched: acknowledging a reminder is not doing the work.
    tasks = await harness.commitments.list_tasks(statuses=None)
    assert [task.status.value for task in tasks] == ["open"]


async def test_dismissing_stops_reminding_without_changing_the_source(harness) -> None:
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("attention.dismiss", {"reference": "写报告"})))

    reply = await harness.service.send(thread.id, "这个不用再提醒我了")

    assert "先不再提醒" in reply.text
    assert "没有变化" in reply.text
    live = await (await _service(harness)).list_live()
    assert [item.status for item in live.items] == [AttentionStatus.DISMISSED]
    tasks = await harness.commitments.list_tasks(statuses=None)
    assert [task.status.value for task in tasks] == ["open"]


async def test_an_ambiguous_reference_settles_nothing(harness) -> None:
    second = await harness.create_task("写周报")
    await harness.tasks.set_deadline(second.id, harness.clock.now() - timedelta(hours=1))
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("attention.acknowledge", {"reference": "写"})))

    reply = await harness.service.send(thread.id, "这个我知道了")

    assert "具体一点" in reply.text or "哪一条" in reply.text
    live = await (await _service(harness)).list_live()
    assert {item.status for item in live.items} == {AttentionStatus.OPEN}


async def test_the_vocabulary_cannot_express_an_attention_execution() -> None:
    """§12/§75: there is no `attention.execute`, and no policy that could mean one."""
    names = {operation.value for operation in ConversationOperationType}

    assert {"attention.list", "attention.acknowledge", "attention.dismiss"} <= names
    assert not {name for name in names if name.startswith("attention.") and "execute" in name}
    assert not {
        name
        for name in names
        if name.startswith(("action.", "approval.", "execution.", "browser.", "shell."))
    }


async def test_the_registry_keeps_the_three_attention_policies_closed(harness) -> None:
    registry = bootstrap.conversation_capabilities(
        harness.database, harness.clock, harness.config, model=harness.model
    )

    assert registry.policy_of(ConversationOperationType.ATTENTION_LIST) is ConfirmationPolicy.READ
    for settle_operation in (
        ConversationOperationType.ATTENTION_ACKNOWLEDGE,
        ConversationOperationType.ATTENTION_DISMISS,
    ):
        assert registry.policy_of(settle_operation) is ConfirmationPolicy.LOCAL_WRITE
