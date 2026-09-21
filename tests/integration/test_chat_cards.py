"""Confirmation cards in the browser (Phase 11A, ADR-0041 §20-§29, §45-§46, §50).

Every card is built from durable state and settles exactly the target it names, through the same
deterministic controllers the terminal phrase path uses. The tests below are the phase's safety
claims in executable form: an exact mail preview, one approval and one execution per confirmed
send, a stale card that can send nothing, a fact that a generic "可以" cannot confirm, a plan that
applies exactly once, and a recurring group where only the current rules survive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from assistant.application.conversation_cards import ConfirmationCardKind, parse_card_id
from assistant.domain.conversation_review import ConversationExternalReviewStatus
from tests.support.chat import ChatStack, build_chat
from tests.support.conversation import operation, plan

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

ADDRESS = "alice@example.com"
SENTENCE = f"给 {ADDRESS} 发封邮件，主题“测试”，内容“你好”。"


@pytest.fixture
async def stack(tmp_path: Path) -> ChatStack:
    return await build_chat(tmp_path)


def _counts(stack: ChatStack) -> dict[str, int]:
    """Row counts for the state an external effect must move exactly once."""
    connection = sqlite3.connect(str(stack.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "action_requests",
                "approvals",
                "execution_runs",
                "conversation_external_reviews",
                "fact_candidates",
                "confirmed_facts",
                "plan_blocks",
            )
        }
    finally:
        connection.close()


def _compose(
    *,
    subject: str = "测试",
    body: str = "你好",
    address: str | None = ADDRESS,
) -> dict[str, object]:
    return operation(
        "mail.compose_new",
        {
            "subject": subject,
            "body": body,
            "recipient_kind": "explicit_email" if address else None,
            "recipient_address": address,
            "recipient_name": None,
            "sender_account": None,
            "draft_id": None,
        },
    )


async def _send(stack: ChatStack, client, tokens, thread: str, text: str) -> dict[str, object]:
    """Submit one message through the real endpoint and wait for the worker to settle it."""
    accepted = client.post(
        f"/api/chat/threads/{thread}/messages",
        json={"client_request_id": f"request-{abs(hash(text)) % 10**12:012d}", "text": text},
        headers=stack.headers(tokens["csrf"]),
    ).json()
    assert await stack.wait_until_async(
        lambda: _settled(stack, str(accepted["id"]))
    ), await stack.request_row(str(accepted["id"]))
    return accepted


async def _settled(stack: ChatStack, request_id: str) -> bool:
    row = await stack.request_row(request_id)
    return row is not None and row.status.value in TERMINAL


async def _thread(client, stack: ChatStack, tokens) -> str:
    return client.post("/api/chat/threads", headers=stack.headers(tokens["csrf"])).json()["id"]


async def _cards(client, thread: str) -> list[dict[str, object]]:
    return client.get(f"/api/chat/threads/{thread}/snapshot").json()["cards"]


# ------------------------------------------------------------------ the exact mail card


async def test_the_mail_card_shows_the_exact_message_and_sends_once(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(_compose(), operation("mail.prepare_new_send", {"draft_id": None}))
        )
        await _send(stack, client, tokens, thread, SENTENCE)

        cards = await _cards(client, thread)
        assert [card["kind"] for card in cards] == ["mail_send"]
        card = cards[0]
        fields = {item["label"]: item["value"] for item in card["fields"]}
        assert fields["收件人"] == ADDRESS
        assert fields["主题"] == "测试"
        assert card["items"] == [{"body": "你好"}]
        assert card["confirm_label"] == "确认发送"
        assert card["cancel_label"] == "取消"
        # Nothing external has happened yet, and no approval exists.
        before = _counts(stack)
        assert before["approvals"] == 0 and before["execution_runs"] == 0
        assert stack.harness.executor.calls == []

        confirmed = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert confirmed.status_code == 200, confirmed.text
        assert "已发送" in confirmed.json()["text"]
        after = _counts(stack)
        assert after["approvals"] == 1
        assert after["execution_runs"] == 1
        assert len(stack.harness.executor.calls) == 1
        # The click is not a second send: the card is gone, and a repeat is a stale confirmation.
        assert await _cards(client, thread) == []
        again = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )
        assert again.status_code == 409
        assert again.json()["code"] == "STALE_CONFIRMATION"
        assert len(stack.harness.executor.calls) == 1
        assert _counts(stack)["approvals"] == 1


async def test_the_mail_card_can_be_withdrawn_with_no_send(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(_compose(), operation("mail.prepare_new_send", {"draft_id": None}))
        )
        await _send(stack, client, tokens, thread, SENTENCE)
        card = (await _cards(client, thread))[0]

        cancelled = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/cancel",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert cancelled.status_code == 200, cancelled.text
        assert "没有发送" in cancelled.json()["text"]
        counts = _counts(stack)
        assert counts["approvals"] == 0 and counts["execution_runs"] == 0
        assert stack.harness.executor.calls == []
        reviews = await stack.harness.waiting_reviews()
        assert reviews == []
        # Confirming afterwards must not revive it.
        late = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )
        assert late.status_code == 409
        assert stack.harness.executor.calls == []


async def test_a_revised_draft_makes_the_old_card_stale(tmp_path: Path) -> None:
    """Revision through the conversation: the card that showed the old text may send nothing."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(_compose(), operation("mail.prepare_new_send", {"draft_id": None}))
        )
        await _send(stack, client, tokens, thread, SENTENCE)
        stale = (await _cards(client, thread))[0]

        # The user changes the letter, which supersedes the review the old card named.
        stack.harness.queue(
            plan(
                _compose(subject="改好的主题", body="改好的正文"),
                operation("mail.prepare_new_send", {"draft_id": None}),
            )
        )
        await _send(stack, client, tokens, thread, f"改一下：给 {ADDRESS} 发封邮件。")

        fresh = await _cards(client, thread)
        assert len(fresh) == 1
        fields = {item["label"]: item["value"] for item in fresh[0]["fields"]}
        assert fields["主题"] == "改好的主题"
        assert fresh[0]["id"] != stale["id"]

        refused = client.post(
            f"/api/chat/threads/{thread}/confirmations/{stale['id']}/confirm",
            json={"expected_revision": stale["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert refused.status_code == 409
        assert refused.json()["code"] == "STALE_CONFIRMATION"
        assert stack.harness.executor.calls == []
        assert [card["id"] for card in await _cards(client, thread)] == [fresh[0]["id"]]


async def test_a_tampered_revision_cannot_settle_a_card(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(_compose(), operation("mail.prepare_new_send", {"draft_id": None}))
        )
        await _send(stack, client, tokens, thread, SENTENCE)
        card = (await _cards(client, thread))[0]

        refused = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": "0" * 64},
            headers=stack.headers(tokens["csrf"]),
        )

        assert refused.status_code == 409
        assert stack.harness.executor.calls == []
        assert _counts(stack)["approvals"] == 0


async def test_an_unknown_card_kind_is_refused(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)

        response = client.post(
            f"/api/chat/threads/{thread}/confirmations/execute:everything/confirm",
            json={"expected_revision": "a" * 64},
            headers=stack.headers(tokens["csrf"]),
        )

        assert response.status_code == 404
        assert parse_card_id("mail_send:abc") == (ConfirmationCardKind.MAIL_SEND, "abc")


# ------------------------------------------------------------------------ the fact card


async def test_the_fact_card_confirms_exactly_one_fact_without_a_model(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(
                operation(
                    "fact.propose",
                    {
                        "key": "profile.office",
                        "value": "浏览器测试地点",
                        "correction_text": "记住我的办公室在浏览器测试地点",
                    },
                )
            )
        )
        await _send(stack, client, tokens, thread, "记住我的办公室在浏览器测试地点")

        cards = await _cards(client, thread)
        assert [card["kind"] for card in cards] == ["fact_confirmation"]
        card = cards[0]
        fields = {item["label"]: item["value"] for item in card["fields"]}
        assert fields["内容"] == "浏览器测试地点"
        assert card["confirm_label"] == "确认记住"

        # A generic acknowledgement is not a confirmation, and it is not a model call either.
        stack.harness.queue(plan(reply="要记住的话，请点确认卡片。", mode="direct_reply"))
        await _send(stack, client, tokens, thread, "可以")
        assert _counts(stack)["confirmed_facts"] == 0
        assert [item["kind"] for item in await _cards(client, thread)] == ["fact_confirmation"]

        calls_before = len(stack.model.requests)
        confirmed = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert confirmed.status_code == 200, confirmed.text
        assert len(stack.model.requests) == calls_before  # settling a fact is not a model call
        assert _counts(stack)["confirmed_facts"] == 1
        assert await _cards(client, thread) == []


async def test_the_fact_card_can_refuse_a_proposal(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(
                operation(
                    "fact.propose",
                    {
                        "key": "profile.office",
                        "value": "仙林",
                        "correction_text": "记住我的办公室在仙林",
                    },
                )
            )
        )
        await _send(stack, client, tokens, thread, "记住我的办公室在仙林")
        card = (await _cards(client, thread))[0]

        refused = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/cancel",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert refused.status_code == 200
        counts = _counts(stack)
        assert counts["confirmed_facts"] == 0
        assert counts["fact_candidates"] == 1  # the audit record survives the refusal
        assert await _cards(client, thread) == []


# ------------------------------------------------------------------------ the plan card


async def test_the_plan_card_applies_the_proposal_exactly_once(tmp_path: Path) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        await stack.harness.create_task("写实验报告", estimated_minutes=90)
        stack.harness.queue(plan(operation("plan.propose_week", {"next_week": False})))
        await _send(stack, client, tokens, thread, "帮我规划这周。")

        cards = await _cards(client, thread)
        assert [card["kind"] for card in cards] == ["plan_apply"]
        card = cards[0]
        assert card["confirm_label"] == "应用计划"
        assert card["items"], "the card must show what would be scheduled"
        assert card["items"][0]["title"] == "写实验报告"
        assert _counts(stack)["plan_blocks"] == 0

        calls_before = len(stack.model.requests)
        applied = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert applied.status_code == 200, applied.text
        assert len(stack.model.requests) == calls_before
        blocks = _counts(stack)["plan_blocks"]
        assert blocks == len(card["items"])

        again = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )
        assert again.status_code == 409
        assert _counts(stack)["plan_blocks"] == blocks


# ------------------------------------------------------------------- the recurring card


WEDNESDAY_CLASS = operation(
    "calendar.recurring.create_weekly",
    {
        "title": "操作系统",
        "weekday": 3,
        "start_local_time": "08:00",
        "end_local_time": "09:40",
        "timezone": None,
        "starts_on": None,
        "ends_on": None,
    },
)
MONDAY_CLASS = operation(
    "calendar.recurring.create_weekly",
    {
        "title": "编译原理",
        "weekday": 1,
        "start_local_time": "10:00",
        "end_local_time": "11:40",
        "timezone": None,
        "starts_on": None,
        "ends_on": None,
    },
)


async def test_the_recurring_card_lists_the_group_and_applies_only_the_current_one(
    tmp_path: Path,
) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = await _thread(client, stack, tokens)
        stack.harness.queue(plan(MONDAY_CLASS, WEDNESDAY_CLASS))
        await _send(stack, client, tokens, thread, "周一十点编译原理，周三八点操作系统。")

        cards = await _cards(client, thread)
        assert [card["kind"] for card in cards] == ["recurring_schedule"]
        card = cards[0]
        assert card["confirm_label"] == "保存固定安排"
        assert [item["title"] for item in card["items"]] == ["编译原理", "操作系统"]
        assert [item["weekday"] for item in card["items"]] == ["周一", "周三"]

        # The user revises the schedule through the conversation.
        revised = operation(
            "calendar.recurring.create_weekly",
            {
                "title": "操作系统",
                "weekday": 3,
                "start_local_time": "14:00",
                "end_local_time": "15:40",
                "timezone": None,
                "starts_on": None,
                "ends_on": None,
            },
        )
        stack.harness.queue(plan(revised))
        await _send(stack, client, tokens, thread, "课程时间改成周三下午两点。")

        fresh = await _cards(client, thread)
        assert len(fresh) == 1
        assert [item["start"] for item in fresh[0]["items"]] == ["14:00"]
        assert fresh[0]["id"] != card["id"]

        refused = client.post(
            f"/api/chat/threads/{thread}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )
        assert refused.status_code == 409

        applied = client.post(
            f"/api/chat/threads/{thread}/confirmations/{fresh[0]['id']}/confirm",
            json={"expected_revision": fresh[0]["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )
        assert applied.status_code == 200, applied.text
        rules = await stack.harness.recurring_rules.list_rules(status=None)
        stored_rules = [(rule.title, f"{rule.start_time:%H:%M}") for rule in rules]
        assert stored_rules == [("操作系统", "14:00")]
        assert await _cards(client, thread) == []


async def test_a_review_in_another_thread_is_not_settled_by_this_one(tmp_path: Path) -> None:
    """A card belongs to its conversation: the thread in the path has to own the target."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        first = await _thread(client, stack, tokens)
        second = await _thread(client, stack, tokens)
        stack.harness.queue(
            plan(_compose(), operation("mail.prepare_new_send", {"draft_id": None}))
        )
        await _send(stack, client, tokens, first, SENTENCE)
        card = (await _cards(client, first))[0]

        refused = client.post(
            f"/api/chat/threads/{second}/confirmations/{card['id']}/confirm",
            json={"expected_revision": card["expected_revision"]},
            headers=stack.headers(tokens["csrf"]),
        )

        assert refused.status_code == 409
        assert stack.harness.executor.calls == []
        assert _counts(stack)["approvals"] == 0
        reviews = await stack.harness.reviews.waiting_for_thread(UUID(first))
        assert len(reviews) == 1
        assert reviews[0].status is ConversationExternalReviewStatus.WAITING
