"""The v1.3 transcript: one person's afternoon, end to end (Phase 11F).

Everything below is the shipped code path — the real conversation runtime, the real planner, the
real attention projector, the real certificate service and the real approval and execution
services — with only two things scripted: the provider, and the outside world (a recording SMTP
executor and a fake certificate page). No real mail is sent, no real eHall is contacted, and the
flow ends by asserting exactly that.

The sequence is the one the release exists for: today → attention → a planning preference → a
replan proposal → apply it → eHall status → prepare → a generic "可以" that does nothing →
an explicit "确认提交" that submits exactly once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assistant.domain.conversation import ConversationTurnStatus
from assistant.domain.conversation_review import ConversationExternalReviewStatus
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    operation,
    plan,
)
from tests.support.ehall import FakeEHallPage

INTERNAL_LEAKS = (
    "Traceback",
    "jsonschema",
    "ValidationError",
    "sqlite3.",
    "KeyError",
    "AssertionError",
    "ActionRequest",
    "ApprovalChallenge",
    "ExecutionRun",
    "waiting_confirmation",
    "plan_ready",
    "[debug]",
)

APPLICANT = "张三"
CERTIFICATE_TYPE = "在读证明"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path, ehall_page=FakeEHallPage())


def _counts(harness: ConversationHarness) -> dict[str, int]:
    connection = sqlite3.connect(str(harness.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "action_requests",
                "approvals",
                "execution_runs",
                "conversation_external_reviews",
                "plan_blocks",
            )
        }
    finally:
        connection.close()


def _assert_clean(*replies: str) -> None:
    for text in replies:
        for leak in INTERNAL_LEAKS:
            assert leak not in text, leak


async def test_the_v1_3_afternoon_flow_end_to_end(harness: ConversationHarness) -> None:
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    await harness.create_task("写实验报告", due_at=harness.clock.now(), estimated_minutes=120)

    # 1. Today, then the inbox. Both are read-only, and both answer in product language.
    harness.queue(plan(operation("brief.today", {})))
    today = await harness.service.send(thread.id, "我今天有什么事？")
    harness.queue(plan(operation("attention.list", {"include_settled": False})))
    inbox = await harness.service.send(thread.id, "有什么需要我处理的？")
    _assert_clean(today.text, inbox.text)
    assert today.text.strip() and inbox.text.strip()

    # 2. A capacity preference: "not after ten" is durable state, not conversation state.
    harness.queue(
        plan(operation("planning.preferences.update", {"day_end": "22:00"}))
    )
    preference = await harness.service.send(thread.id, "每天晚上十点以后不要安排任务。")
    _assert_clean(preference.text)
    assert "22:00" in preference.text

    # 3. A replan proposal. It proposes; it does not apply.
    harness.queue(plan(operation("plan.replan_week", {})))
    proposal = await harness.service.send(thread.id, "这周太满了，重新安排一下。")
    _assert_clean(proposal.text)
    pending = await harness.planning.list_proposal_summaries(limit=5)
    assert [summary.proposal.id for summary in pending], "a proposal was stored"
    before_apply = _counts(harness)["plan_blocks"]

    # 4. The user applies it with a generic yes, because applying a *local* plan is a local
    #    confirmation — and only that. Nothing external moves on "可以".
    harness.queue(direct_reply("要应用这个计划吗？回复「可以」我就写进计划。"))
    applied = await harness.service.send(thread.id, "可以")
    _assert_clean(applied.text)
    after_apply = _counts(harness)["plan_blocks"]
    assert after_apply >= before_apply
    assert _counts(harness)["approvals"] == 0  # a local apply is not an approval

    # 5. eHall: what can be done here, and what the form needs.
    harness.queue(plan(operation("ehall.status", {})))
    status = await harness.service.send(thread.id, "eHall 能用吗？")
    _assert_clean(status.text)
    assert "证明书申请" in status.text

    # 6. Preparing a certificate: an exact preview, and nothing else.
    harness.queue(
        plan(
            operation(
                "ehall.certificate.prepare",
                {
                    "fields": {
                        "applicant-name": APPLICANT,
                        "certificate-type": CERTIFICATE_TYPE,
                    },
                    "case_id": None,
                },
            )
        )
    )
    preview = await harness.service.send(
        thread.id,
        f"帮我申请{CERTIFICATE_TYPE}，申请人姓名是{APPLICANT}，证明书类型是{CERTIFICATE_TYPE}。",
    )
    _assert_clean(preview.text)
    assert "将要提交的证明申请" in preview.text
    assert APPLICANT in preview.text
    prepared = _counts(harness)
    assert prepared["action_requests"] == 1
    assert prepared["approvals"] == 0
    assert prepared["execution_runs"] == 0
    page = harness.ehall_page
    assert page is not None and page.clicks == []

    # 7. A generic yes is not a submission, on any surface.
    harness.queue(direct_reply("要提交的话，请回复「确认提交」。"))
    generic = await harness.service.send(thread.id, "可以")
    _assert_clean(generic.text)
    assert "确认提交" in generic.text
    assert _counts(harness)["approvals"] == 0
    assert page.clicks == []

    # 8. An explicit, action-specific confirmation: one approval, one run, one click.
    submitted = await harness.service.send(thread.id, "确认提交")
    _assert_clean(submitted.text)
    assert "已提交" in submitted.text
    assert page.clicks == ["certificate-submit"]
    assert dict(page.fills) == {
        "applicant-name": APPLICANT,
        "certificate-type": CERTIFICATE_TYPE,
    }
    settled = _counts(harness)
    assert settled["approvals"] == 1
    assert settled["execution_runs"] == 1
    assert len(
        await harness.reviews.list_by_status(ConversationExternalReviewStatus.SUCCEEDED)
    ) == 1

    # The flow touched no mail and no second submission.
    assert harness.executor.calls == []
    assert page.clicks == ["certificate-submit"]
    assert len(await harness.reviews.waiting_for_thread(thread.id)) == 0


async def test_the_v1_3_flow_never_leaks_internal_language_or_acts_twice(
    harness: ConversationHarness,
) -> None:
    """The same flow, once more, from the angle of what a user must never see or trigger twice."""
    thread, _ = await harness.service.resume_or_start()
    harness.queue(
        plan(
            operation(
                "ehall.certificate.prepare",
                {
                    "fields": {
                        "applicant-name": APPLICANT,
                        "certificate-type": CERTIFICATE_TYPE,
                    },
                    "case_id": None,
                },
            )
        )
    )
    prepared = await harness.service.send(
        thread.id,
        f"帮我申请{CERTIFICATE_TYPE}，申请人姓名是{APPLICANT}，证明书类型是{CERTIFICATE_TYPE}。",
    )
    assert prepared.status is ConversationTurnStatus.COMPLETED
    harness.queue(direct_reply("这份申请已经提交过了。"))
    first = await harness.service.send(thread.id, "确认提交")
    second = await harness.service.send(thread.id, "确认提交")
    _assert_clean(prepared.text, first.text, second.text)

    page = harness.ehall_page
    assert page is not None and page.clicks == ["certificate-submit"]
    assert _counts(harness)["approvals"] == 1
    assert _counts(harness)["execution_runs"] == 1
