"""The conversation vocabulary: closed, typed, and impossible to smuggle a tool through (ADR-0033).

The plan schema is the boundary, so these tests are about what *cannot* be expressed: an unknown
operation type, an argument the operation does not accept, a sixth operation, a raw command, an
unresolved naive time, an entity reference that was never offered.
"""

from __future__ import annotations

import pytest

from assistant.application.conversation_schema import CONVERSATION_SCHEMA_V1, OPERATION_SCHEMAS
from assistant.domain.conversation_plan import (
    MAX_OPERATIONS_PER_TURN,
    READ_OPERATIONS,
    CalendarCreateArguments,
    ConversationOperationType,
    ConversationPlan,
    ConversationPlanMode,
    PlannedOperation,
    TaskCreateArguments,
    TaskSetDeadlineArguments,
    build_arguments,
    operation_fingerprint,
    requires_planning_timezone,
)
from assistant.domain.errors import InvalidConversationPlan
from assistant.domain.task import TaskPriority
from tests.support.conversation import NOW

FORBIDDEN_OPERATION_PREFIXES = (
    "case.",
    "action.",
    "approval.",
    "execution.",
    "mail.",
    "ehall.",
    "fact.",
    "playbook.",
    "http.",
    "browser.",
    "shell.",
    "filesystem.",
    "tool.",
    "function.",
)


def test_the_vocabulary_is_exactly_the_reviewed_set() -> None:
    """10A's daily-local operations, 10B's mail, 10D's weekly rules and 10E's contacts."""
    expected = {
        "status.get",
        "task.list",
        "task.show",
        "task.create",
        "task.edit",
        "task.set_deadline",
        "task.clear_deadline",
        "task.complete",
        "calendar.list",
        "calendar.create",
        "calendar.recurring.list",
        "calendar.recurring.create_weekly",
        "calendar.recurring.edit",
        "calendar.recurring.retire",
        "work.record",
        "plan.current",
        "plan.propose_week",
        "plan.apply_proposal",
        "notification.list",
        "notification.read",
        "knowledge.ask",
        "mail.status",
        "mail.sync",
        "mail.list",
        "mail.show",
        "mail.thread",
        "mail.reply_draft",
        "mail.prepare_reply_send",
        "mail.reconcile_send",
        "mail.accounts",
        "mail.compose_new",
        "mail.prepare_new_send",
        "contact.list",
        "contact.create",
        "contact.edit",
        "contact.retire",
        "fact.list",
        "fact.show",
        "fact.propose",
        "brief.today",
        "system.capabilities",
    }

    assert {member.value for member in ConversationOperationType} == expected


def test_no_external_or_privileged_operation_exists() -> None:
    """The reviewed mail surface is the only external-adjacent vocabulary that exists.

    `mail.send` itself is still absent: a conversation prepares a send and a deterministic
    controller settles it, so the model never names the effect (ADR-0034 §2-4).
    """
    reviewed_mail = {
        "mail.status",
        "mail.sync",
        "mail.list",
        "mail.show",
        "mail.thread",
        "mail.reply_draft",
        "mail.prepare_reply_send",
        "mail.reconcile_send",
        "mail.accounts",
        "mail.compose_new",
        "mail.prepare_new_send",
    }
    for member in ConversationOperationType:
        if (
            member.value in reviewed_mail
            or member.value == "system.capabilities"
            or member.value.startswith("contact.")
            or member.value
            in {"fact.list", "fact.show", "fact.propose"}
        ):
            continue
        assert not member.value.startswith(FORBIDDEN_OPERATION_PREFIXES), member.value

    assert "mail.send" not in {member.value for member in ConversationOperationType}


def test_the_schema_offers_every_operation_and_nothing_generic() -> None:
    offered = {branch["properties"]["type"]["const"] for branch in OPERATION_SCHEMAS}

    assert offered == {member.value for member in ConversationOperationType}
    serialised = repr(CONVERSATION_SCHEMA_V1.schema)
    forbidden_fields = (
        "tool_name",
        "function_name",
        "method",
        "command",
        "shell",
        "url",
        "executor",
    )
    for forbidden in forbidden_fields:
        assert f"'{forbidden}'" not in serialised, forbidden


def test_an_unknown_operation_is_refused_not_ignored() -> None:
    with pytest.raises(InvalidConversationPlan):
        build_arguments("mail.send", {"to": "someone@example.edu"})
    with pytest.raises(InvalidConversationPlan):
        build_arguments("task.execute", {})


def test_an_argument_the_operation_does_not_accept_is_refused() -> None:
    with pytest.raises(InvalidConversationPlan):
        build_arguments("task.create", {"title": "写报告", "shell": "rm -rf /"})
    with pytest.raises(InvalidConversationPlan):
        build_arguments("task.list", {"include_terminal": False, "sql": "DROP TABLE tasks"})


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(InvalidConversationPlan):
        build_arguments("task.set_deadline", {"task_id": str(NOW.replace(tzinfo=None)) , "due_at": "2026-09-21T15:00:00"})  # noqa: E501


def test_arguments_are_validated_on_construction() -> None:
    with pytest.raises(InvalidConversationPlan):
        TaskCreateArguments(title="   ")
    with pytest.raises(InvalidConversationPlan):
        TaskSetDeadlineArguments(task_id=NOW, due_at=NOW.replace(tzinfo=None))  # type: ignore[arg-type]


def test_a_plan_is_capped_at_five_operations() -> None:
    created = PlannedOperation(
        operation_type=ConversationOperationType.TASK_CREATE,
        arguments=TaskCreateArguments(title="写报告"),
    )
    with pytest.raises(InvalidConversationPlan):
        ConversationPlan(
            mode=ConversationPlanMode.OPERATIONS,
            operations=(created,) * (MAX_OPERATIONS_PER_TURN + 1),
        )


def test_a_plan_mode_must_match_its_content() -> None:
    created = PlannedOperation(
        operation_type=ConversationOperationType.TASK_CREATE,
        arguments=TaskCreateArguments(title="写报告"),
    )
    with pytest.raises(InvalidConversationPlan):
        ConversationPlan(mode=ConversationPlanMode.OPERATIONS)
    with pytest.raises(InvalidConversationPlan):
        ConversationPlan(mode=ConversationPlanMode.DIRECT_REPLY, operations=(created,))
    with pytest.raises(InvalidConversationPlan):
        ConversationPlan(mode=ConversationPlanMode.CLARIFICATION)


def test_the_fingerprint_is_canonical_and_argument_sensitive() -> None:
    first = operation_fingerprint(
        ConversationOperationType.TASK_CREATE, TaskCreateArguments(title="写报告")
    )
    again = operation_fingerprint(
        ConversationOperationType.TASK_CREATE, TaskCreateArguments(title="写报告")
    )
    changed = operation_fingerprint(
        ConversationOperationType.TASK_CREATE, TaskCreateArguments(title="写报告！")
    )

    assert first == again
    assert first != changed
    assert len(first) == 64


def test_read_operations_are_the_ones_that_only_read() -> None:
    assert ConversationOperationType.TASK_LIST in READ_OPERATIONS
    assert ConversationOperationType.KNOWLEDGE_ASK in READ_OPERATIONS
    for write in (
        ConversationOperationType.TASK_CREATE,
        ConversationOperationType.TASK_COMPLETE,
        ConversationOperationType.CALENDAR_CREATE,
        ConversationOperationType.WORK_RECORD,
        ConversationOperationType.PLAN_APPLY_PROPOSAL,
    ):
        assert write not in READ_OPERATIONS


def test_only_time_bearing_operations_need_a_planning_timezone() -> None:
    assert requires_planning_timezone(
        ConversationOperationType.TASK_SET_DEADLINE,
        TaskSetDeadlineArguments(task_id=NOW, due_at=NOW),  # type: ignore[arg-type]
    )
    assert requires_planning_timezone(
        ConversationOperationType.TASK_CREATE,
        TaskCreateArguments(title="写报告", due_at=NOW),
    )
    assert not requires_planning_timezone(
        ConversationOperationType.TASK_CREATE, TaskCreateArguments(title="写报告")
    )
    assert not requires_planning_timezone(
        ConversationOperationType.TASK_LIST, build_arguments("task.list", {})
    )
    # A weekly commitment without a stated zone is only meaningful in the planning timezone.
    assert requires_planning_timezone(
        ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY,
        build_arguments(
            "calendar.recurring.create_weekly",
            {
                "title": "计算机系统基础课",
                "weekday": 1,
                "start_local_time": "10:00",
                "end_local_time": "12:00",
            },
        ),
    )
    # With its own IANA zone it is fully determined, and no planning timezone is needed.
    assert not requires_planning_timezone(
        ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY,
        build_arguments(
            "calendar.recurring.create_weekly",
            {
                "title": "计算机系统基础课",
                "weekday": 1,
                "start_local_time": "10:00",
                "end_local_time": "12:00",
                "timezone": "Asia/Shanghai",
            },
        ),
    )
    assert not requires_planning_timezone(
        ConversationOperationType.CALENDAR_RECURRING_LIST,
        build_arguments("calendar.recurring.list", {}),
    )


def test_an_event_must_end_after_it_starts() -> None:
    with pytest.raises(InvalidConversationPlan):
        CalendarCreateArguments(title="课", starts_at=NOW, ends_at=NOW)


def test_priority_round_trips_through_arguments() -> None:
    arguments = build_arguments("task.create", {"title": "写报告", "priority": "high"})

    assert isinstance(arguments, TaskCreateArguments)
    assert arguments.priority is TaskPriority.HIGH


def test_instants_are_rendered_in_the_planning_timezone() -> None:
    """The user reads their own clock, written the way a clock is written (`+08:00`)."""
    from assistant.application.conversation_capabilities.registry import OperationResult
    from assistant.application.conversation_render import render_result

    result = OperationResult(
        kind="deadline_set",
        ref="task",
        data={"title": "写报告", "due_at": "2026-09-22T07:00:00+00:00"},
    )

    rendered = render_result(result, timezone="Asia/Shanghai")
    other = render_result(result, timezone=None)

    assert "2026-09-22 15:00（+08:00）" in rendered
    assert "2026-09-22 07:00（UTC）" in other
