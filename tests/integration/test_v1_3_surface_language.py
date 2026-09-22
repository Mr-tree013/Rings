"""What the surfaces may say out loud (Phase 11F; ADR-0035 §8, §32, ADR-0041 §78).

Two claims are pinned here, both about the boundary between the product and its machinery:

* **no internal vocabulary reaches a user** — not an operation name, not a status constant, not a
  class name, not a database column, not a debug marker, on any surface this release added;
* **bounded state stays bounded** — a long private summary is trimmed before it travels into a
  prompt, and a derived cache is never quoted back as if it were a document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.domain.attention import (
    AttentionItem,
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
)
from assistant.domain.notification import Notification, NotificationKind
from assistant.store.attention import SqliteAttentionRepository
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    operation,
    plan,
)
from tests.support.ehall import FakeEHallPage

INTERNAL_TOKENS = (
    "ActionRequest",
    "ExecutionRun",
    "ApprovalService",
    "ActionExecutionService",
    "ScheduledJob",
    "FactCandidate",
    "ConfirmedFact",
    "PlanBlock",
    "WorkSession",
    "conversation_request",
    "conversation_external_review",
    "waiting_confirmation",
    "WAITING_CONFIRMATION",
    "plan_ready",
    "Updated plan proposal is ready",
    "Traceback",
    "[debug]",
    "sqlite3",
    "jsonschema",
    "None",
)
"""Words a person should never have to read. They are what the surfaces are allowed to *call*
things internally, and the tests below are the reason they stay internal."""

ATTENTION_SENTINEL = "SENTINEL-PRIVATE-ATTENTION-PAYLOAD"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path, ehall_page=FakeEHallPage())


def _assert_no_internal_language(text: str) -> None:
    for token in INTERNAL_TOKENS:
        assert token not in text, token


# ------------------------------------------------------------------ the sweep


async def test_a_certificate_preparation_reads_as_product_language(
    harness: ConversationHarness,
) -> None:
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    harness.queue(plan(operation("ehall.status", {})))
    status = await harness.service.send(thread.id, "eHall 能用吗？")
    _assert_no_internal_language(status.text)

    harness.queue(
        plan(
            operation(
                "ehall.certificate.prepare",
                {
                    "fields": {
                        "applicant-name": "张三",
                        "certificate-type": "在读证明",
                    },
                    "case_id": None,
                },
            )
        )
    )
    preview = await harness.service.send(
        thread.id, "帮我申请在读证明，申请人姓名是张三，证明书类型是在读证明。"
    )
    _assert_no_internal_language(preview.text)
    assert "在读证明" in preview.text


async def test_a_generic_acknowledgement_and_its_answer_read_as_product_language(
    harness: ConversationHarness,
) -> None:
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    harness.queue(plan(operation("plan.propose_week", {"next_week": False})))
    proposed = await harness.service.send(thread.id, "帮我安排一下这周剩下的时间。")
    _assert_no_internal_language(proposed.text)

    harness.queue(direct_reply("要应用的话，请回复「可以」。"))
    answered = await harness.service.send(thread.id, "嗯……")
    _assert_no_internal_language(answered.text)


async def test_the_attention_inbox_reads_as_product_language(
    harness: ConversationHarness,
) -> None:
    """A real unread notification, projected by the real projector into the real inbox."""
    await harness.scheduler.create_notification_idempotent(
        Notification(
            kind=NotificationKind.SCHEDULER_WARNING,
            title="有一项后台任务需要你检查",
            body="页面内容发生了变化。",
            dedup_key="warning:1",
            created_at=harness.clock.now(),
        )
    )
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    harness.queue(plan(operation("attention.list", {"include_settled": False})))

    listed = await harness.service.send(thread.id, "有什么需要我处理的？")

    _assert_no_internal_language(listed.text)
    assert "需要你检查" in listed.text


async def test_capabilities_read_as_product_language(harness: ConversationHarness) -> None:
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    harness.queue(plan(operation("system.capabilities", {})))

    described = await harness.service.send(thread.id, "你现在能做什么？")

    _assert_no_internal_language(described.text)
    assert "eHall" in described.text


# --------------------------------------------------------------- bounded state


def test_a_large_private_attention_payload_cannot_even_be_stored() -> None:
    """The bound is in the domain object, so nothing downstream has to remember it."""
    from assistant.domain.errors import InvalidAttentionItem

    with pytest.raises(InvalidAttentionItem):
        AttentionItem(
            kind=AttentionKind.WATCHER_OBSERVATION,
            source_type=AttentionSourceType.WEB_OBSERVATION,
            source_id="observation-1",
            dedupe_key="observation:1",
            fingerprint="c" * 64,
            severity=AttentionSeverity.INFO,
            title="有一个页面变化值得看一下",
            summary=ATTENTION_SENTINEL * 200,
            created_at=harness_clock(),
            updated_at=harness_clock(),
        )


async def test_only_the_inbox_line_reaches_the_prompt(
    harness: ConversationHarness,
) -> None:
    """An item is recognisable in a prompt by its title; its private summary is not carried."""
    repository = SqliteAttentionRepository(harness.database)
    now = harness.clock.now()
    private = "摘录：" + "私密页面内容" * 40
    await repository.add_item(
        AttentionItem(
            kind=AttentionKind.WATCHER_OBSERVATION,
            source_type=AttentionSourceType.WEB_OBSERVATION,
            source_id="observation-1",
            dedupe_key="observation:1",
            fingerprint="c" * 64,
            severity=AttentionSeverity.INFO,
            title="有一个页面变化值得看一下",
            summary=private,
            created_at=now,
            updated_at=now,
        )
    )
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    harness.queue(direct_reply("好。"))
    await harness.service.send(thread.id, "知道了")

    context = harness.model.requests[-1].messages[0].content

    assert "有一个页面变化值得看一下" in context  # the item itself is recognisable
    assert "私密页面内容" not in context  # its private payload is not


def harness_clock() -> datetime:
    """A fixed instant for the domain-object test above."""
    return datetime(2026, 9, 21, tzinfo=UTC)
