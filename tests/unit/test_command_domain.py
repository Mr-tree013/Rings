"""Typed command drafts and interpretation results (ADR-0018)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.domain.command import (
    CancelTaskDraft,
    ClearDeadlineDraft,
    CommandKind,
    CompleteTaskDraft,
    CreateCalendarEventDraft,
    CreateTaskDraft,
    RequestWeekPlanDraft,
    SetDeadlineDraft,
    is_time_bearing,
)
from assistant.domain.errors import (
    InvalidCommandDraft,
    InvalidInterpretationResult,
)
from assistant.domain.interpreter import InterpretationResult, InterpretationStatus
from assistant.domain.task import TaskPriority, validate_estimated_minutes, validate_task_title

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)


def test_the_supported_kinds_are_the_seven_of_this_phase() -> None:
    assert {kind.value for kind in CommandKind} == {
        "create_task",
        "complete_task",
        "cancel_task",
        "set_deadline",
        "clear_deadline",
        "create_calendar_event",
        "request_week_plan",
    }


def test_each_draft_reports_its_kind() -> None:
    assert CreateTaskDraft(title="x").kind is CommandKind.CREATE_TASK
    assert CompleteTaskDraft(task_id=uuid4()).kind is CommandKind.COMPLETE_TASK
    assert CancelTaskDraft(task_id=uuid4()).kind is CommandKind.CANCEL_TASK
    assert SetDeadlineDraft(task_id=uuid4(), due_at=LATER).kind is CommandKind.SET_DEADLINE
    assert ClearDeadlineDraft(task_id=uuid4()).kind is CommandKind.CLEAR_DEADLINE
    assert (
        CreateCalendarEventDraft(title="x", starts_at=NOW, ends_at=LATER).kind
        is CommandKind.CREATE_CALENDAR_EVENT
    )
    assert RequestWeekPlanDraft().kind is CommandKind.REQUEST_WEEK_PLAN


def test_a_create_task_draft_uses_the_task_domain_rules() -> None:
    """The draft cannot accept what `Task` would reject: same validators, one rule set."""
    draft = CreateTaskDraft(
        title="  Write SE lab report  ",
        description="  with references  ",
        priority=TaskPriority.HIGH,
        estimated_minutes=300,
        deadline=LATER,
    )

    assert draft.title == "Write SE lab report"  # trimmed, like the CLI does
    assert draft.description == "with references"
    assert draft.priority is TaskPriority.HIGH

    with pytest.raises(InvalidCommandDraft):
        CreateTaskDraft(title="   ")
    with pytest.raises(InvalidCommandDraft):
        CreateTaskDraft(title="x" * 501)
    with pytest.raises(InvalidCommandDraft):
        CreateTaskDraft(title="x", estimated_minutes=0)
    with pytest.raises(InvalidCommandDraft):
        CreateTaskDraft(title="x", deadline=datetime(2026, 9, 21, 0, 0))  # naive
    with pytest.raises(InvalidCommandDraft):
        CreateTaskDraft(title="x", priority="high")  # type: ignore[arg-type]


def test_blank_optional_text_becomes_none() -> None:
    assert CreateTaskDraft(title="x", description="   ").description is None


def test_task_reference_drafts_require_a_real_uuid() -> None:
    for draft_type in (CompleteTaskDraft, CancelTaskDraft, ClearDeadlineDraft):
        assert draft_type(task_id=uuid4()).task_id is not None
        with pytest.raises(InvalidCommandDraft):
            draft_type(task_id="not-a-uuid")  # type: ignore[arg-type]

    with pytest.raises(InvalidCommandDraft):
        SetDeadlineDraft(task_id="not-a-uuid", due_at=LATER)  # type: ignore[arg-type]


def test_a_deadline_draft_needs_an_aware_instant() -> None:
    with pytest.raises(InvalidCommandDraft):
        SetDeadlineDraft(task_id=uuid4(), due_at=datetime(2026, 9, 21, 0, 0))


def test_a_calendar_draft_uses_the_event_interval_rule() -> None:
    draft = CreateCalendarEventDraft(title=" Class ", starts_at=NOW, ends_at=LATER)

    assert draft.title == "Class"
    with pytest.raises(InvalidCommandDraft):
        CreateCalendarEventDraft(title="x", starts_at=LATER, ends_at=NOW)  # backwards
    with pytest.raises(InvalidCommandDraft):
        CreateCalendarEventDraft(title="x", starts_at=LATER, ends_at=LATER)  # empty
    with pytest.raises(InvalidCommandDraft):
        CreateCalendarEventDraft(
            title="x", starts_at=datetime(2026, 9, 21), ends_at=LATER
        )  # naive


def test_a_week_plan_draft_holds_one_boolean() -> None:
    assert RequestWeekPlanDraft().next_week is False
    assert RequestWeekPlanDraft(next_week=True).next_week is True
    with pytest.raises(InvalidCommandDraft):
        RequestWeekPlanDraft(next_week="yes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("draft", "expected"),
    (
        (CreateTaskDraft(title="x"), False),
        (CreateTaskDraft(title="x", deadline=LATER), True),
        (CompleteTaskDraft(task_id=uuid4()), False),
        (SetDeadlineDraft(task_id=uuid4(), due_at=LATER), True),
        (CreateCalendarEventDraft(title="x", starts_at=NOW, ends_at=LATER), True),
        (RequestWeekPlanDraft(), True),
    ),
)
def test_only_time_bearing_drafts_require_a_timezone(draft: object, expected: bool) -> None:
    assert is_time_bearing(draft) is expected  # type: ignore[arg-type]


def test_the_shared_validators_are_the_ones_the_entity_uses() -> None:
    assert validate_task_title("  x  ") == "x"
    validate_estimated_minutes(None)
    validate_estimated_minutes(1)
    with pytest.raises(Exception):  # noqa: B017 - the entity's own error type
        validate_estimated_minutes(0)


# ----------------------------------------------------------------- interpretation


def test_a_ready_result_carries_only_a_command() -> None:
    result = InterpretationResult.ready(CreateTaskDraft(title="x"))

    assert result.status is InterpretationStatus.READY
    assert result.command is not None
    assert result.question is None and result.reason is None


def test_a_clarification_carries_only_a_question() -> None:
    result = InterpretationResult.needs_clarification("  Which task?  ")

    assert result.status is InterpretationStatus.NEEDS_CLARIFICATION
    assert result.question == "Which task?"
    assert result.command is None and result.reason is None


def test_an_unsupported_result_carries_only_a_reason() -> None:
    result = InterpretationResult.unsupported("  Mail is not supported yet.  ")

    assert result.status is InterpretationStatus.UNSUPPORTED
    assert result.reason == "Mail is not supported yet."
    assert result.command is None and result.question is None


@pytest.mark.parametrize(
    "kwargs",
    (
        {"status": InterpretationStatus.READY},
        {"status": InterpretationStatus.READY, "command": None, "question": "hm"},
        {"status": InterpretationStatus.NEEDS_CLARIFICATION, "question": "  "},
        {"status": InterpretationStatus.NEEDS_CLARIFICATION, "reason": "because"},
        {"status": InterpretationStatus.UNSUPPORTED},
        {"status": InterpretationStatus.UNSUPPORTED, "reason": "  "},
        {"status": InterpretationStatus.UNSUPPORTED, "question": "hm"},
    ),
)
def test_mixed_statuses_are_impossible(kwargs: dict[str, object]) -> None:
    with pytest.raises(InvalidInterpretationResult):
        InterpretationResult(**kwargs)  # type: ignore[arg-type]


def test_a_result_has_no_confidence_or_rationale_field() -> None:
    """A model's self-reported confidence is not a business fact."""
    fields = set(InterpretationResult.__dataclass_fields__)

    assert fields == {"status", "command", "question", "reason"}
