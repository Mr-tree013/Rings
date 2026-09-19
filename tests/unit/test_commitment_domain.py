"""Unit tests for the commitment domain: five distinct concepts (ADR-0014)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.calendar_event import CalendarEvent, CalendarEventNotActive
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    InvalidCalendarEvent,
    InvalidDeadline,
    InvalidPlanBlock,
    InvalidTask,
    InvalidTaskTransition,
    InvalidTimeInterval,
    InvalidWorkSession,
    PlanBlockNotActive,
)
from assistant.domain.plan_block import PlanBlock
from assistant.domain.task import TITLE_MAX_LENGTH, Task, TaskPriority, TaskStatus
from assistant.domain.time_interval import BusyInterval, BusyIntervalKind, overlaps
from assistant.domain.work_session import WorkSession

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "title": "Write SE lab report",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _event(**overrides: object) -> CalendarEvent:
    values: dict[str, object] = {
        "title": "SE lecture",
        "starts_at": NOW,
        "ends_at": LATER,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CalendarEvent(**values)  # type: ignore[arg-type]


def _block(**overrides: object) -> PlanBlock:
    values: dict[str, object] = {
        "task_id": uuid4(),
        "starts_at": NOW,
        "ends_at": LATER,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return PlanBlock(**values)  # type: ignore[arg-type]


def test_task_defaults_and_valid_construction() -> None:
    task = _task()

    assert task.status is TaskStatus.OPEN
    assert task.priority is TaskPriority.NORMAL
    assert task.estimated_minutes is None
    assert task.completed_at is None and task.cancelled_at is None
    assert task.is_open


@pytest.mark.parametrize("title", ["", "   ", "\n\t", "x" * (TITLE_MAX_LENGTH + 1)])
def test_task_title_is_validated(title: str) -> None:
    with pytest.raises(InvalidTask, match="title"):
        _task(title=title)


@pytest.mark.parametrize("estimate", [0, -1])
def test_task_estimate_must_be_positive(estimate: int) -> None:
    with pytest.raises(InvalidTask, match="estimated_minutes"):
        _task(estimated_minutes=estimate)


def test_task_timestamps_must_be_aware() -> None:
    with pytest.raises(InvalidTask, match="timezone-aware"):
        _task(created_at=datetime(2026, 9, 20, 9, 0))


def test_open_task_rejects_terminal_timestamps() -> None:
    with pytest.raises(InvalidTask, match="OPEN task"):
        _task(completed_at=NOW)


def test_completed_and_cancelled_consistency() -> None:
    with pytest.raises(InvalidTask, match="COMPLETED task must carry completed_at"):
        _task(status=TaskStatus.COMPLETED)
    with pytest.raises(InvalidTask, match="CANCELLED task must carry cancelled_at"):
        _task(status=TaskStatus.CANCELLED)
    with pytest.raises(InvalidTask, match="must not carry cancelled_at"):
        _task(status=TaskStatus.COMPLETED, completed_at=NOW, cancelled_at=NOW)


def test_complete_and_cancel_transitions() -> None:
    task = _task()

    completed = task.complete(LATER)
    cancelled = task.cancel(LATER)

    assert completed.status is TaskStatus.COMPLETED
    assert completed.completed_at == LATER
    assert completed.updated_at == LATER
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.cancelled_at == LATER
    assert task.status is TaskStatus.OPEN  # the original is untouched


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.CANCELLED])
def test_terminal_tasks_cannot_transition_again(status: TaskStatus) -> None:
    terminal = _task(**{status.value + "_at": NOW}, status=status)

    with pytest.raises(InvalidTaskTransition):
        terminal.complete(LATER)
    with pytest.raises(InvalidTaskTransition):
        terminal.cancel(LATER)
    with pytest.raises(InvalidTaskTransition):
        terminal.update_details(
            title="new",
            description=None,
            priority=TaskPriority.NORMAL,
            estimated_minutes=None,
            at=LATER,
        )


def test_updating_details_keeps_identity_and_moves_updated_at() -> None:
    task = _task()

    edited = task.update_details(
        title="Write OS lab report",
        description="chapter 3",
        priority=TaskPriority.HIGH,
        estimated_minutes=120,
        at=LATER,
    )

    assert edited.id == task.id
    assert edited.created_at == task.created_at
    assert edited.updated_at == LATER
    assert edited.priority is TaskPriority.HIGH


def test_deadline_requires_an_aware_due_time() -> None:
    with pytest.raises(InvalidDeadline, match="timezone-aware"):
        Deadline(
            task_id=uuid4(),
            due_at=datetime(2026, 10, 20, 23, 59),
            created_at=NOW,
            updated_at=NOW,
        )


def test_deadline_rescheduling_keeps_identity() -> None:
    deadline = Deadline(task_id=uuid4(), due_at=LATER, created_at=NOW, updated_at=NOW)

    moved = deadline.rescheduled(due_at=LATER + timedelta(days=1), at=LATER)

    assert moved.id == deadline.id
    assert moved.due_at == LATER + timedelta(days=1)
    assert moved.updated_at == LATER


def test_deadline_carries_no_duration_or_start() -> None:
    names = {field.name for field in fields(Deadline)}

    assert names == {"id", "task_id", "due_at", "created_at", "updated_at"}
    assert "duration" not in names and "start_at" not in names and "reminder_at" not in names


def test_calendar_event_requires_a_positive_duration() -> None:
    with pytest.raises(InvalidCalendarEvent, match="after starts_at"):
        _event(ends_at=NOW)
    with pytest.raises(InvalidCalendarEvent, match="after starts_at"):
        _event(ends_at=NOW - timedelta(minutes=1))


def test_calendar_event_cancellation_is_explicit() -> None:
    event = _event()

    cancelled = event.cancel(LATER)

    assert cancelled.cancelled_at == LATER
    assert not cancelled.is_active
    with pytest.raises(CalendarEventNotActive):
        cancelled.cancel(LATER)


def test_calendar_event_has_no_recurrence_or_attendees() -> None:
    names = {field.name for field in fields(CalendarEvent)}

    assert "rrule" not in names and "attendees" not in names and "location" not in names


def test_plan_block_has_no_actual_work_fields() -> None:
    names = {field.name for field in fields(PlanBlock)}

    assert {"task_id", "starts_at", "ends_at"} <= names
    assert "actual_minutes" not in names
    assert "completed_at" not in names
    assert "actual_seconds" not in names


def test_plan_block_duration_is_planned_not_actual() -> None:
    block = _block()

    assert block.planned_seconds == 7200
    assert not hasattr(block, "actual_seconds")


def test_plan_block_requires_a_task_and_positive_duration() -> None:
    with pytest.raises(InvalidPlanBlock, match="after starts_at"):
        _block(ends_at=NOW)


def test_plan_block_cancellation_is_explicit() -> None:
    block = _block()

    cancelled = block.cancel(LATER)

    assert cancelled.cancelled_at == LATER
    with pytest.raises(PlanBlockNotActive):
        cancelled.cancel(LATER)


def test_work_session_duration_is_exact_seconds() -> None:
    session = WorkSession(
        task_id=uuid4(),
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=90),
        created_at=NOW,
    )

    assert session.duration_seconds == 5400
    assert {field.name for field in fields(WorkSession)} == {
        "id",
        "task_id",
        "started_at",
        "ended_at",
        "created_at",
    }
    assert "duration_seconds" not in {field.name for field in fields(WorkSession)}


def test_work_session_rejects_empty_or_negative_intervals() -> None:
    with pytest.raises(InvalidWorkSession, match="after started_at"):
        WorkSession(task_id=uuid4(), started_at=NOW, ended_at=NOW, created_at=NOW)
    with pytest.raises(InvalidWorkSession, match="timezone-aware"):
        WorkSession(
            task_id=uuid4(),
            started_at=datetime(2026, 9, 20, 9, 0),
            ended_at=NOW,
            created_at=NOW,
        )


def test_overlaps_uses_half_open_intervals() -> None:
    ten, eleven, twelve = (
        datetime(2026, 9, 21, 10, tzinfo=UTC),
        datetime(2026, 9, 21, 11, tzinfo=UTC),
        datetime(2026, 9, 21, 12, tzinfo=UTC),
    )

    assert not overlaps(ten, eleven, eleven, twelve)  # back-to-back is not an overlap
    assert overlaps(ten, twelve, eleven - timedelta(minutes=1), twelve)
    assert overlaps(ten, eleven, eleven - timedelta(seconds=1), twelve)


def test_overlaps_rejects_invalid_input() -> None:
    with pytest.raises(InvalidTimeInterval):
        overlaps(NOW, NOW, LATER, LATER + timedelta(hours=1))
    with pytest.raises(InvalidTimeInterval, match="timezone-aware"):
        overlaps(datetime(2026, 9, 20, 9, 0), LATER, NOW, LATER)


def test_busy_interval_keeps_its_source_kind() -> None:
    interval = BusyInterval(
        source_kind=BusyIntervalKind.PLAN_BLOCK,
        source_id=uuid4(),
        starts_at=NOW,
        ends_at=LATER,
    )

    assert interval.source_kind is BusyIntervalKind.PLAN_BLOCK
    with pytest.raises(InvalidTimeInterval, match="after starts_at"):
        BusyInterval(
            source_kind=BusyIntervalKind.CALENDAR_EVENT,
            source_id=uuid4(),
            starts_at=NOW,
            ends_at=NOW,
        )

