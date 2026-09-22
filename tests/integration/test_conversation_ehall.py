"""Conversational eHall certificate preparation and its exact confirmation boundary (Phase 11E).

Every test runs the real runtime — the real certificate service, the real `ActionRequest`, the real
review, the real approval and execution boundary, the real `ehall.submit-certificate` executor and
the real pipeline policy — over a *fake page*. No browser is opened and no university is contacted,
which is exactly what makes "nothing was typed during preparation" and "one click after one
confirmation" assertions instead of promises.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assistant.application.conversation_cards import (
    ConfirmationCardKind,
    ConversationCardService,
)
from assistant.domain.action import ActionRequestStatus
from assistant.domain.conversation import ConversationTurnStatus
from assistant.domain.conversation_review import (
    ALLOWED_EXTERNAL_ACTION_TYPES,
    CONFIRM_EHALL_CERTIFICATE_PHRASES,
    CONFIRM_SEND_PHRASES,
    ConversationExternalReviewStatus,
    matches_confirmation,
)
from assistant.domain.ehall import EHallCertificatePayload
from assistant.domain.errors import StaleConversationCard
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    operation,
    plan,
)
from tests.support.ehall import (
    FIELD_KEY,
    SECOND_FIELD_KEY,
    FakeEHallPage,
)

APPLICANT = "张三"
CERTIFICATE_TYPE = "在读证明"
SENTENCE = f"帮我申请{CERTIFICATE_TYPE}，申请人姓名是{APPLICANT}。"
MAIL_ADDRESS = "alice@example.com"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    """The real runtime with the certificate pipeline enabled over a fake page."""
    return await build_harness(tmp_path, ehall_page=FakeEHallPage())


async def _thread(harness: ConversationHarness):
    thread, resumed = await harness.service.resume_or_start()
    assert resumed is False
    return thread


def _counts(harness: ConversationHarness) -> dict[str, int]:
    """Row counts for the state an external effect must move exactly once."""
    connection = sqlite3.connect(str(harness.database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "cases",
                "action_requests",
                "approvals",
                "approval_challenges",
                "execution_runs",
                "conversation_external_reviews",
            )
        }
    finally:
        connection.close()


def prepare(
    *,
    applicant: str = APPLICANT,
    certificate: str = CERTIFICATE_TYPE,
    case_id: str | None = None,
) -> dict[str, object]:
    """One `ehall.certificate.prepare` operation, with every schema field present."""
    return operation(
        "ehall.certificate.prepare",
        {
            "fields": {FIELD_KEY: applicant, SECOND_FIELD_KEY: certificate},
            "case_id": case_id,
        },
    )


async def _prepare_certificate(
    harness: ConversationHarness,
    *,
    sentence: str = SENTENCE,
    applicant: str = APPLICANT,
    certificate: str = CERTIFICATE_TYPE,
    case_id: str | None = None,
):
    """One turn that prepares a certificate application and shows the exact preview."""
    thread = await _thread(harness)
    harness.queue(plan(prepare(applicant=applicant, certificate=certificate, case_id=case_id)))
    reply = await harness.service.send(thread.id, sentence)
    return thread, reply


def _page(harness: ConversationHarness) -> FakeEHallPage:
    assert harness.ehall_page is not None, "this harness has no certificate pipeline"
    return harness.ehall_page


# ------------------------------------------------------------------- status (§4, §6)


async def test_without_an_ehall_section_the_status_is_honestly_not_configured(
    tmp_path: Path,
) -> None:
    """A host that never enabled the pipeline says so, and names no internal machinery."""
    harness = await build_harness(tmp_path)
    thread = await _thread(harness)
    harness.queue(plan(operation("ehall.status", {})))

    reply = await harness.service.send(thread.id, "你能帮我在 eHall 交材料吗？")

    assert "还没有配置" in reply.text
    assert "pw ehall login" in reply.text
    assert "password" not in reply.text.lower()
    assert _counts(harness)["action_requests"] == 0


async def test_the_status_reports_the_live_form_it_can_actually_read(
    harness: ConversationHarness,
) -> None:
    """The form's own field keys, labels, options and materials — and nothing is typed."""
    thread = await _thread(harness)
    harness.queue(plan(operation("ehall.status", {})))

    reply = await harness.service.send(thread.id, "eHall 现在能用吗？")

    assert "证明书申请" in reply.text
    assert "申请人姓名" in reply.text
    assert "证明书类型" in reply.text
    assert CERTIFICATE_TYPE in reply.text
    assert "身份证件" in reply.text
    assert "确认提交" in reply.text
    page = _page(harness)
    assert page.fills == []
    assert page.clicks == []
    assert _counts(harness)["action_requests"] == 0


async def test_a_status_that_needs_a_login_says_so_without_asking_for_a_password(
    tmp_path: Path,
) -> None:
    """An expired session is its own state: log in by hand, and no password is ever requested."""
    from assistant.domain.errors import EHallLoginRequired
    from tests.support.ehall import FakeEHallPage

    page = FakeEHallPage()
    harness = await build_harness(tmp_path, ehall_page=page)
    thread = await _thread(harness)
    # The session expired between the two reads: the report is a login prompt, not a form dump.
    harness.queue(plan(operation("ehall.status", {})))
    page.service_error = EHallLoginRequired("no session")
    reply = await harness.service.send(thread.id, "eHall 能用吗？")

    assert "登录" in reply.text
    assert "学校密码" in reply.text
    assert "no session" not in reply.text


# --------------------------------------------------------------- preparation (§5-§12)


async def test_a_supported_certificate_is_prepared_and_never_submitted(
    harness: ConversationHarness,
) -> None:
    """The whole first half of the errand: prepare, show the exact payload, stop."""
    thread, reply = await _prepare_certificate(harness)

    assert "将要提交的证明申请" in reply.text
    assert APPLICANT in reply.text
    assert CERTIFICATE_TYPE in reply.text
    assert "身份证件" in reply.text
    assert "确认提交" in reply.text
    assert "回复「可以」不会提交" in reply.text
    counts = _counts(harness)
    assert counts["action_requests"] == 1
    assert counts["approvals"] == 0
    assert counts["approval_challenges"] == 0
    assert counts["execution_runs"] == 0
    assert counts["cases"] == 1
    waiting = await harness.reviews.waiting_for_thread(thread.id)
    assert len(waiting) == 1
    assert waiting[0].action_type == "ehall.submit-certificate"
    page = _page(harness)
    assert page.fills == []
    assert page.clicks == []
    # The service page was *read* (that is what an inspection is) and nothing else happened: no
    # keystroke, no click, no second page.
    assert page.opened == ["证明书申请"]


async def test_the_prepared_action_is_the_exact_immutable_certificate_errand(
    harness: ConversationHarness,
) -> None:
    """One immutable `ActionRequest`, fingerprinted, holding only the user's own values."""
    thread, _ = await _prepare_certificate(harness)

    actions = await harness.actions.list_actions(limit=5)
    assert len(actions) == 1
    action = actions[0]
    payload = EHallCertificatePayload.from_payload(action.payload)
    values = {value.key: value.value for value in payload.fields}
    assert values == {FIELD_KEY: APPLICANT, SECOND_FIELD_KEY: CERTIFICATE_TYPE}
    assert action.action_type.value == "ehall.submit-certificate"
    assert action.status is ActionRequestStatus.PREPARED
    assert action.fingerprint_matches()
    waiting = await harness.reviews.waiting_for_thread(thread.id)
    assert waiting[0].action_request_id == action.id
    assert waiting[0].action_fingerprint == action.fingerprint
    assert payload.service_identity == "nju-ehall/证明书申请"


async def test_a_missing_required_field_is_a_question_not_a_default(
    harness: ConversationHarness,
) -> None:
    """Half a form prepares nothing: the runtime asks for the page's own required field."""
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "ehall.certificate.prepare",
                {"fields": {FIELD_KEY: APPLICANT}, "case_id": None},
            )
        )
    )

    reply = await harness.service.send(thread.id, f"帮我申请，姓名{APPLICANT}。")

    assert "还缺少必要的信息" in reply.text
    assert "证明书类型" in reply.text
    assert "必填" in reply.text
    counts = _counts(harness)
    assert counts["action_requests"] == 0
    assert counts["conversation_external_reviews"] == 0
    assert counts["approvals"] == 0
    assert await harness.reviews.waiting_for_thread(thread.id) == []


async def test_a_certificate_the_page_does_not_offer_is_refused(
    harness: ConversationHarness,
) -> None:
    """Only the options the live page offers are acceptable, and the answer says which."""
    unsupported = "实习证明"
    thread = await _thread(harness)
    harness.queue(plan(prepare(certificate=unsupported)))

    reply = await harness.service.send(
        thread.id, f"帮我申请{unsupported}，申请人姓名是{APPLICANT}。"
    )

    assert "还缺少必要的信息" in reply.text
    assert "页面只接受" in reply.text
    assert CERTIFICATE_TYPE in reply.text
    assert _counts(harness)["action_requests"] == 0


async def test_a_field_the_page_does_not_have_is_refused(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "ehall.certificate.prepare",
                {"fields": {"applicant-nam": APPLICANT, SECOND_FIELD_KEY: CERTIFICATE_TYPE},
                 "case_id": None},
            )
        )
    )

    reply = await harness.service.send(thread.id, SENTENCE)

    assert "这个页面没有这个字段" in reply.text
    assert _counts(harness)["action_requests"] == 0


async def test_a_value_the_model_invented_is_refused_before_anything_happens(
    harness: ConversationHarness,
) -> None:
    """A name the human never wrote cannot reach a form: no case, no action, no review."""
    thread = await _thread(harness)
    harness.queue(plan(prepare(applicant="李四")))

    reply = await harness.service.send(thread.id, SENTENCE)

    assert "李四" in reply.text
    assert "没有出现在你刚才那句话里" in reply.text
    counts = _counts(harness)
    assert counts["cases"] == 0
    assert counts["action_requests"] == 0
    assert counts["conversation_external_reviews"] == 0
    assert _page(harness).opened == []


async def test_a_prepared_certificate_uses_the_open_case_it_was_given(
    harness: ConversationHarness,
) -> None:
    case = await harness.cases.create_case("Certificate errand")
    thread = await _thread(harness)
    harness.queue(plan(prepare(case_id=str(case.id))))

    reply = await harness.service.send(thread.id, SENTENCE)

    assert "将要提交的证明申请" in reply.text
    actions = await harness.actions.list_actions(limit=5)
    assert [action.case_id for action in actions] == [case.id]
    assert _counts(harness)["cases"] == 1


async def test_a_closed_case_refuses_the_preparation(harness: ConversationHarness) -> None:
    case = await harness.cases.create_case("Certificate errand")
    await harness.cases.cancel_case(case.id)
    thread = await _thread(harness)
    harness.queue(plan(prepare(case_id=str(case.id))))

    reply = await harness.service.send(thread.id, SENTENCE)

    assert "项目已经结束" in reply.text
    assert _counts(harness)["action_requests"] == 0
    assert _page(harness).opened == []


# ------------------------------------------------------------- confirmation (§13-§24)


async def test_a_generic_yes_does_not_submit(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)
    harness.queue(direct_reply("要提交的话，请回复「确认提交」。"))

    reply = await harness.service.send(thread.id, "可以")

    assert "确认提交" in reply.text
    counts = _counts(harness)
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    assert len(await harness.reviews.waiting_for_thread(thread.id)) == 1
    assert _page(harness).clicks == []


async def test_an_explicit_confirmation_submits_exactly_once(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)

    submitted = await harness.service.send(thread.id, "确认提交")

    assert "已提交" in submitted.text
    page = _page(harness)
    assert page.clicks == ["certificate-submit"]
    assert dict(page.fills) == {FIELD_KEY: APPLICANT, SECOND_FIELD_KEY: CERTIFICATE_TYPE}
    counts = _counts(harness)
    assert counts["approvals"] == 1
    assert counts["execution_runs"] == 1
    succeeded = await harness.reviews.list_by_status(
        ConversationExternalReviewStatus.SUCCEEDED
    )
    assert len(succeeded) == 1

    harness.queue(direct_reply("这份申请已经提交过了。"))
    again = await harness.service.send(thread.id, "确认提交")

    assert page.clicks == ["certificate-submit"]
    assert _counts(harness)["approvals"] == 1
    assert "提交" in again.text


async def test_cancelling_the_review_prevents_the_submission(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)

    cancelled = await harness.service.send(thread.id, "不要提交")

    assert "没有提交" in cancelled.text
    assert await harness.reviews.waiting_for_thread(thread.id) == []
    counts = _counts(harness)
    assert counts["approvals"] == 0
    assert counts["execution_runs"] == 0
    assert _page(harness).clicks == []


async def test_a_send_phrase_cannot_submit_a_certificate(
    harness: ConversationHarness,
) -> None:
    """The two confirmation vocabularies are disjoint, so one phrase settles one capability."""
    thread, _ = await _prepare_certificate(harness)
    harness.queue(direct_reply("这是一份证明申请，请回复「确认提交」。"))

    reply = await harness.service.send(thread.id, "确认发送")

    assert _page(harness).clicks == []
    assert len(await harness.reviews.waiting_for_thread(thread.id)) == 1
    assert _counts(harness)["approvals"] == 0
    assert reply.status is ConversationTurnStatus.COMPLETED
    assert not (CONFIRM_SEND_PHRASES & CONFIRM_EHALL_CERTIFICATE_PHRASES)
    assert matches_confirmation("ehall.submit-certificate", "确认提交")
    assert not matches_confirmation("ehall.submit-certificate", "确认发送")
    assert not matches_confirmation("mail.send", "确认提交")


async def test_a_submit_phrase_cannot_send_a_letter(
    harness: ConversationHarness,
) -> None:
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "测试",
                    "body": "你好",
                    "recipient_kind": "explicit_email",
                    "recipient_address": MAIL_ADDRESS,
                    "recipient_name": None,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        )
    )
    await harness.service.send(thread.id, f"给 {MAIL_ADDRESS} 发封邮件，主题“测试”，内容“你好”。")
    harness.queue(direct_reply("这是一封邮件，请回复「确认发送」。"))

    await harness.service.send(thread.id, "确认提交")

    assert harness.executor.calls == []
    assert len(await harness.reviews.waiting_for_thread(thread.id)) == 1
    assert _counts(harness)["approvals"] == 0


async def test_editing_the_values_stales_the_old_review_and_only_the_new_one_can_submit(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness, applicant=APPLICANT)
    first = (await harness.reviews.waiting_for_thread(thread.id))[0]
    first_actions = await harness.actions.list_actions(limit=5)
    first_action = first_actions[0]

    harness.queue(plan(prepare(applicant="李四")))
    second = await harness.service.send(
        thread.id, f"改成申请人姓名是李四，证明书类型是{CERTIFICATE_TYPE}。"
    )

    assert "李四" in second.text
    stale = await harness.reviews.get_review(first.id)
    assert stale is not None
    assert stale.status is ConversationExternalReviewStatus.STALE
    waiting = await harness.reviews.waiting_for_thread(thread.id)
    assert len(waiting) == 1
    assert waiting[0].id != first.id
    actions = await harness.actions.list_actions(limit=5)
    assert len(actions) == 2
    newest = next(action for action in actions if action.id == waiting[0].action_request_id)
    assert newest.id != first_action.id
    assert newest.fingerprint != first_action.fingerprint
    assert _counts(harness)["approvals"] == 0

    submitted = await harness.service.send(thread.id, "确认提交")

    assert "已提交" in submitted.text
    page = _page(harness)
    assert page.clicks == ["certificate-submit"]
    assert dict(page.fills)[FIELD_KEY] == "李四"
    old = await harness.actions.get_action(first_action.id)
    assert old is not None
    assert old.status is ActionRequestStatus.PREPARED
    assert await harness.actions.latest_execution(first_action.id) is None


async def test_an_unknown_submission_is_reported_and_never_retried(
    harness: ConversationHarness,
) -> None:
    """A click whose result cannot be read blocks the action instead of guessing."""
    thread, _ = await _prepare_certificate(harness)
    page = _page(harness)
    page.result = "unclear"

    reply = await harness.service.send(thread.id, "确认提交")

    assert "不确定" in reply.text
    assert "不会自动重试" in reply.text
    assert page.clicks == ["certificate-submit"]
    assert _counts(harness)["execution_runs"] == 1
    unknown = await harness.reviews.list_by_status(
        ConversationExternalReviewStatus.UNKNOWN
    )
    assert len(unknown) == 1
    assert await harness.reviews.waiting_for_thread(thread.id) == []

    harness.queue(direct_reply("这份申请的结果还不确定，我不会再提交一次。"))
    await harness.service.send(thread.id, "确认提交")

    assert page.clicks == ["certificate-submit"]
    assert _counts(harness)["execution_runs"] == 1


async def test_a_rejected_submission_reports_failure_without_a_retry(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)
    page = _page(harness)
    page.result = "rejected"

    reply = await harness.service.send(thread.id, "确认提交")

    assert "提交失败" in reply.text
    assert page.clicks == ["certificate-submit"]
    failed = await harness.reviews.list_by_status(ConversationExternalReviewStatus.FAILED)
    assert len(failed) == 1


# ------------------------------------------------------------------ cards (§18-§19)


def _cards(harness: ConversationHarness) -> ConversationCardService:
    return ConversationCardService(
        conversation=harness.service,
        reviews=harness.review_service,
        conversations=harness.conversations,
        planning=harness.planning,
        commitments=harness.commitments,
        facts=harness.facts,
        clock=harness.clock,
    )


async def test_the_browser_card_shows_the_exact_payload_and_settles_exactly_once(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)
    cards = _cards(harness)

    live = await cards.cards(thread.id)
    ehall_cards = [card for card in live if card.kind is ConfirmationCardKind.EHALL_CERTIFICATE]
    assert len(ehall_cards) == 1
    card = ehall_cards[0]
    assert card.confirm_label == "确认提交"
    assert card.cancel_label == "取消"
    rendered = " ".join(
        [field.value for field in card.fields]
        + [str(item.get("value") or item) for item in card.items]
    )
    assert APPLICANT in rendered
    assert CERTIFICATE_TYPE in rendered
    assert "身份证件" in rendered

    reply = await cards.settle(
        thread.id, card.id, expected_revision=card.expected_revision, confirm=True
    )

    assert "已提交" in reply.text
    page = _page(harness)
    assert page.clicks == ["certificate-submit"]
    assert _counts(harness)["approvals"] == 1
    # The recorded message is the phrase a terminal user would have typed.
    messages = await harness.conversations.list_messages(thread.id, limit=10)
    assert any(message.text == "确认提交" for message in messages)


async def test_a_stale_card_can_submit_nothing(harness: ConversationHarness) -> None:
    thread, _ = await _prepare_certificate(harness)
    cards = _cards(harness)
    card = next(
        item for item in await cards.cards(thread.id)
        if item.kind is ConfirmationCardKind.EHALL_CERTIFICATE
    )
    harness.queue(plan(prepare(applicant="李四")))
    await harness.service.send(
        thread.id, f"改成申请人姓名是李四，证明书类型是{CERTIFICATE_TYPE}。"
    )

    with pytest.raises(StaleConversationCard):
        await cards.settle(
            thread.id, card.id, expected_revision=card.expected_revision, confirm=True
        )

    assert _page(harness).clicks == []
    assert _counts(harness)["approvals"] == 0


async def test_a_card_from_another_thread_cannot_settle_this_one(
    harness: ConversationHarness,
) -> None:
    thread, _ = await _prepare_certificate(harness)
    cards = _cards(harness)
    card = next(
        item for item in await cards.cards(thread.id)
        if item.kind is ConfirmationCardKind.EHALL_CERTIFICATE
    )
    await harness.service.archive_thread(thread.id)
    other = await harness.service.start_thread(title="另一个对话")

    with pytest.raises(StaleConversationCard):
        await cards.settle(
            other.id, card.id, expected_revision=card.expected_revision, confirm=True
        )

    assert _page(harness).clicks == []
    assert _counts(harness)["approvals"] == 0


# --------------------------------------------------------- restart and closure (§25)


async def test_a_prepared_certificate_survives_a_restart(
    harness: ConversationHarness,
) -> None:
    from assistant import bootstrap
    from assistant.adapters.model.fake import FakeModelAdapter

    thread, _ = await _prepare_certificate(harness)

    restarted = bootstrap.conversation_service(
        harness.database,
        harness.clock,
        harness.config,
        model=FakeModelAdapter(),
        executors={},
        ehall=harness.ehall_service,
    )
    resumed, was_resumed = await restarted.resume_or_start()
    pending = await restarted.pending_external_preview(resumed.id)

    assert was_resumed is True
    assert resumed.id == thread.id
    assert pending is not None
    assert APPLICANT in pending
    assert CERTIFICATE_TYPE in pending
    assert "确认提交" in pending
    assert _page(harness).clicks == []


def test_the_reviewed_action_types_are_a_closed_pair() -> None:
    assert {"mail.send", "ehall.submit-certificate"} == ALLOWED_EXTERNAL_ACTION_TYPES


async def test_the_certificate_flow_logs_no_personal_value(
    harness: ConversationHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """§privacy: a certificate application is personal content, and a log is not a place for it."""
    import logging

    with caplog.at_level(logging.DEBUG):
        thread, _ = await _prepare_certificate(harness)
        await harness.service.send(thread.id, "确认提交")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert APPLICANT not in logged
    assert CERTIFICATE_TYPE not in logged
    # Nor any of the machinery behind the approval it produced.
    for forbidden in ("token_hash", "token", "cookie", "session", "selector"):
        assert forbidden not in logged.lower()
    assert _page(harness).clicks == ["certificate-submit"]


def test_a_review_of_an_unreviewed_capability_cannot_even_be_built() -> None:
    """The closed set is enforced in the domain, the table and the confirmation vocabulary."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from assistant.domain.conversation_review import (
        ConversationExternalReview,
        confirmation_phrases,
    )
    from assistant.domain.errors import InvalidConversationOperation

    now = datetime(2026, 9, 21, tzinfo=UTC)
    with pytest.raises(InvalidConversationOperation):
        ConversationExternalReview(
            conversation_operation_id=uuid4(),
            thread_id=uuid4(),
            action_request_id=uuid4(),
            action_type="ehall.drop-course",
            action_fingerprint="a" * 64,
            expires_at=now.replace(minute=30),
            created_at=now,
            updated_at=now,
        )
    with pytest.raises(InvalidConversationOperation):
        confirmation_phrases("ehall.drop-course")
    assert not matches_confirmation("ehall.drop-course", "确认提交")
