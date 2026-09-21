"""Reliability of the conversation boundary (ADR-0035 §4-§12, §22, §36).

These are the properties that made `rings` unusable in practice: benign provider variation turned
into a schema error, one bad answer ended the turn for good, a malformed plan could apply half of
itself, and internals reached the user. Each of those is pinned here at the layer that owns it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from assistant.application import conversation_render as render
from assistant.application.conversation_capabilities.introspection import (
    CapabilityState,
    build_capability_snapshot,
)
from assistant.application.conversation_input import ConsoleInput, InputOutcome, decode_strictly
from assistant.application.conversation_plan_normalizer import normalize_wire_plan
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.conversation_plan import ConversationPlan, ConversationPlanMode
from assistant.domain.errors import InvalidConversationPlan
from tests.support.conversation import (
    ConversationHarness,
    build_harness,
    direct_reply,
    draft_answer,
    operation,
    plan,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"


@pytest.fixture
async def harness(tmp_path: Path) -> ConversationHarness:
    return await build_harness(tmp_path)


async def _thread(harness: ConversationHarness):
    thread, _ = await harness.service.resume_or_start()
    return thread


# ---------------------------------------------------------------- normalization (§4, §10)


def test_benign_variation_is_filled_in_not_invented() -> None:
    normalized, issue = normalize_wire_plan({"mode": "direct_reply", "reply": "你好"})

    assert issue is None
    assert normalized == {
        "mode": "direct_reply",
        "reply": "你好",
        "clarification": None,
        "operations": [],
    }


def test_a_null_operations_field_becomes_an_empty_list() -> None:
    normalized, issue = normalize_wire_plan(
        {"mode": "operations", "reply": None, "clarification": None, "operations": None}
    )

    assert issue is None
    assert normalized is not None and normalized["operations"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "mail.send", "reply": "x"},
        {"operations": []},
        ["not", "an", "object"],
        {"mode": "operations", "operations": "soon"},
    ],
)
def test_unsafe_or_unusable_shapes_are_left_alone(payload: object) -> None:
    normalized, issue = normalize_wire_plan(payload)

    assert normalized is None
    assert issue


def test_an_empty_direct_reply_is_still_refused_by_the_plan() -> None:
    """Filling in an absence is not the same as accepting an empty answer."""
    normalized, issue = normalize_wire_plan({"mode": "direct_reply"})

    assert issue is None and normalized is not None
    with pytest.raises(InvalidConversationPlan):
        ConversationPlan(mode=ConversationPlanMode.DIRECT_REPLY, reply=None, operations=())


def test_normalization_never_invents_an_operation() -> None:
    normalized, _ = normalize_wire_plan({"mode": "operations", "operations": None})

    assert normalized is not None
    assert normalized["operations"] == []


# -------------------------------------------------------------------- repair (§6, §11)


async def test_one_repair_attempt_can_rescue_a_turn(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue(
        "not json at all",
        plan(
            operation(
                "task.create",
                {
                    "title": "写报告",
                    "description": None,
                    "priority": "normal",
                    "estimated_minutes": None,
                    "due_at": None,
                },
            )
        ),
    )

    reply = await harness.service.send(thread.id, "加个任务，写报告")

    assert len(harness.model.requests) == 2  # the answer, then exactly one repair
    assert reply.status.value == "completed"
    assert await harness.task_titles() == ["写报告"]


async def test_a_second_bad_answer_executes_nothing(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue("still not json", "nope again")

    reply = await harness.service.send(thread.id, "加个任务，写报告")

    assert len(harness.model.requests) == 2
    assert reply.error_code is ConversationErrorCode.MODEL_REPAIR_FAILED
    assert await harness.task_titles() == []
    assert "没有执行任何操作" in reply.text
    assert "jsonschema" not in reply.text and "required property" not in reply.text


async def test_a_provider_failure_is_friendly_and_leaves_the_session_usable(
    harness: ConversationHarness,
) -> None:
    from assistant.domain.errors import ModelTransientError

    thread = await _thread(harness)
    harness.model.queue_error(ModelTransientError("provider timed out"))

    failed = await harness.service.send(thread.id, "写个任务")

    assert failed.error_code is ConversationErrorCode.MODEL_TIMEOUT
    assert "Traceback" not in failed.text
    harness.queue(direct_reply("我在。"))
    recovered = await harness.service.send(thread.id, "还在吗")
    assert recovered.text == "我在。"


async def test_an_empty_answer_is_a_friendly_failure(harness: ConversationHarness) -> None:
    thread = await _thread(harness)
    harness.queue("", "")

    reply = await harness.service.send(thread.id, "你好")

    assert reply.error_code is ConversationErrorCode.MODEL_REPAIR_FAILED
    assert await harness.task_titles() == []


# ------------------------------------------------------------------ preflight (§15, §22)


async def test_a_plan_that_cannot_run_in_full_applies_nothing(
    harness: ConversationHarness,
) -> None:
    """A task create followed by an unsatisfiable send: the task must not be created either."""
    message = await harness.seed_mail()
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "task.create",
                {
                    "title": "写报告",
                    "description": None,
                    "priority": "normal",
                    "estimated_minutes": None,
                    "due_at": None,
                },
            ),
            operation(
                "mail.prepare_reply_send",
                {"draft_id": None, "message_id": str(message.id)},
            ),
        )
    )

    reply = await harness.service.send(thread.id, "加个任务，然后把刚才那封回复发出去")

    assert reply.error_code is ConversationErrorCode.UNSUPPORTED_SEMANTICS
    assert "没有执行任何操作" in reply.text
    assert await harness.task_titles() == []  # the first operation did not run
    assert harness.executor.calls == []


async def test_a_legal_draft_then_prepare_plan_still_runs(
    harness: ConversationHarness,
) -> None:
    """The dependency inside one turn is understood, not refused (ADR-0035 §24)."""
    message = await harness.seed_mail()
    thread = await _thread(harness)
    harness.queue(
        plan(
            operation(
                "mail.reply_draft",
                {"message_id": str(message.id), "body_text": "好的。", "context_query": None},
            ),
            operation(
                "mail.prepare_reply_send",
                {"draft_id": None, "message_id": str(message.id)},
            ),
        ),
        draft_answer("好的。"),
    )

    reply = await harness.service.send(thread.id, "回复张老师，说好的")

    assert "将要发送的邮件" in reply.text
    assert len(await harness.reviews.waiting_for_thread(thread.id)) == 1


# --------------------------------------------------------- terminal boundary (§4-§6, §31)


def test_the_input_reader_decodes_strictly() -> None:
    assert decode_strictly("你好".encode()) == ("你好", None)
    assert decode_strictly("🎉".encode())[0] == "🎉"
    assert decode_strictly(b"\xff\xfe")[0] is None
    assert decode_strictly(b"a\x00b")[0] is None


def test_a_decode_failure_is_a_value_not_an_exception() -> None:
    import io

    reader = ConsoleInput(buffer=io.BytesIO(b"\xff\xfe\n"), output=io.StringIO())

    outcome = reader.read_line("You > ")

    assert outcome.outcome is InputOutcome.DECODE_FAILED


def test_the_reader_reports_a_closed_stream() -> None:
    import io

    reader = ConsoleInput(buffer=io.BytesIO(b""), output=io.StringIO())

    assert reader.read_line("You > ").outcome is InputOutcome.CLOSED


# ------------------------------------------------------------ error rendering (§8, §32)


def test_internal_detail_never_reaches_a_normal_reply() -> None:
    for code in ConversationErrorCode:
        rendered = render.render_error(
            code, "model output violates schema 'type' at operations: validation error"
        )
        assert "schema" not in rendered
        assert "Traceback" not in rendered


def test_debug_mode_is_the_only_place_detail_appears() -> None:
    rendered = render.render_error(
        ConversationErrorCode.OPERATION_FAILED, "the mailbox rejected the message", debug=True
    )

    assert "[debug] the mailbox rejected the message" in rendered


# -------------------------------------------------------------------- capabilities (§13)


def test_the_snapshot_reports_configuration_not_prose() -> None:
    snapshot = build_capability_snapshot(None)

    assert snapshot.area("mail_read") is not None
    assert snapshot.area("mail_read").state is CapabilityState.NOT_CONFIGURED
    # New outbound compose is a real capability now; without configuration it is not usable.
    assert snapshot.area("mail_new_outbound_compose").state is CapabilityState.NOT_CONFIGURED
    assert snapshot.area("contacts").state is CapabilityState.AVAILABLE
    assert snapshot.area("ehall").state is CapabilityState.UNAVAILABLE
    assert all(area.name != "credentials" for area in snapshot.areas)


async def test_the_snapshot_matches_the_registry(harness: ConversationHarness) -> None:
    snapshot = build_capability_snapshot(
        harness.config, operation_types=tuple(harness.service._capabilities.operation_types)
    )

    assert snapshot.area("mail_send_after_confirmation").state is CapabilityState.AVAILABLE
    assert set(snapshot.operation_types) == {
        operation.value for operation in harness.service._capabilities.operation_types
    }


# ------------------------------------------------------------------ architecture (§36)


def _imports(relative: str) -> set[str]:
    tree = ast.parse((SOURCE_ROOT / relative).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_input_reading_cannot_touch_a_service() -> None:
    modules = _imports("application/conversation_input.py")

    for forbidden in ("assistant.store", "assistant.adapters", "assistant.application"):
        assert not [module for module in modules if module.startswith(forbidden)], forbidden


def test_normalization_cannot_reach_a_capability_handler() -> None:
    modules = _imports("application/conversation_plan_normalizer.py")

    assert not [module for module in modules if "capabilities" in module]
    assert not [module for module in modules if module.startswith("assistant.application")]


def test_capability_introspection_reads_metadata_only() -> None:
    modules = _imports("application/conversation_capabilities/introspection.py")

    for forbidden in ("assistant.store", "assistant.adapters"):
        assert not [module for module in modules if module.startswith(forbidden)]
    text = (SOURCE_ROOT / "application/conversation_capabilities/introspection.py").read_text(
        encoding="utf-8"
    )
    for secret in ("password", "credential", "token", "api_key", "secret"):
        assert secret not in text.lower()


async def test_mail_accounts_handler_exposes_no_credential_field(
    harness: ConversationHarness,
) -> None:
    """The result is configuration metadata: no password, token, key or secret of any kind."""
    from assistant.domain.conversation_plan import (
        ConversationOperationType,
        MailAccountsArguments,
    )

    capability = harness.service._capabilities.require(
        ConversationOperationType.MAIL_ACCOUNTS
    )
    result = await capability.handler(MailAccountsArguments())
    accounts = result.data["accounts"]

    assert accounts, "the harness configures one account"
    for account in accounts:
        for key in account:
            for secret in ("password", "credential", "token", "api_key", "secret"):
                assert secret not in key.lower(), key
        assert "from_address" in account
    assert "stored_messages" in accounts[0]


def test_no_generic_tool_or_shell_capability_was_added() -> None:
    from assistant.domain.conversation_plan import ConversationOperationType

    for member in ConversationOperationType:
        for forbidden in ("tool", "shell", "http.", "filesystem", "browser", "ehall", "memory."):
            assert forbidden not in member.value, member.value
    # Phase 10F adds three narrow fact reads/proposals and, deliberately, no way to confirm one:
    # the final confirmation is a deterministic response to the human's own turn (ADR-0038).
    fact_operations = {
        member.value for member in ConversationOperationType if member.value.startswith("fact.")
    }
    assert fact_operations == {"fact.list", "fact.show", "fact.propose"}
