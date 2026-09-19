"""Remaining-effort rules: only recorded work reduces effort (ADR-0015)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from assistant.application.planning_effort import compute_remaining_effort
from assistant.domain.planning import PlanningIssueCode


@pytest.mark.parametrize(
    ("actual_seconds", "expected_minutes"),
    (
        (0, 0),
        (1, 1),  # any recorded second rounds up to a whole minute
        (59, 1),
        (60, 1),
        (61, 2),
        (3599, 60),
        (3600, 60),
        (3601, 61),
    ),
)
def test_actual_minutes_round_up(actual_seconds: int, expected_minutes: int) -> None:
    effort = compute_remaining_effort(
        task_id=uuid4(), estimated_minutes=600, actual_seconds=actual_seconds
    )

    assert effort.remaining_minutes == 600 - expected_minutes
    assert effort.issue is None


def test_missing_estimate_is_reported_instead_of_guessed() -> None:
    task_id = uuid4()

    effort = compute_remaining_effort(
        task_id=task_id, estimated_minutes=None, actual_seconds=3600
    )

    assert effort.remaining_minutes == 0
    assert effort.issue is not None
    assert effort.issue.code is PlanningIssueCode.MISSING_ESTIMATE
    assert effort.issue.task_id == task_id
    assert "pw task edit" in effort.issue.message


def test_exhausted_estimate_is_reported_and_never_extended() -> None:
    task_id = uuid4()

    effort = compute_remaining_effort(
        task_id=task_id, estimated_minutes=60, actual_seconds=3600
    )

    assert effort.remaining_minutes == 0
    assert effort.issue is not None
    assert effort.issue.code is PlanningIssueCode.ESTIMATE_EXHAUSTED
    assert effort.issue.required_minutes == 60
    assert effort.issue.scheduled_minutes == 0


def test_exact_estimate_boundary_counts_as_exhausted() -> None:
    effort = compute_remaining_effort(
        task_id=uuid4(), estimated_minutes=60, actual_seconds=3600
    )

    assert effort.remaining_minutes == 0
    assert effort.issue is not None
    assert effort.issue.code is PlanningIssueCode.ESTIMATE_EXHAUSTED


def test_one_second_short_of_the_estimate_still_has_work_left() -> None:
    effort = compute_remaining_effort(
        task_id=uuid4(), estimated_minutes=2, actual_seconds=61
    )

    assert effort.remaining_minutes == 0  # 61s rounds up to 2 minutes, which meets the estimate
    assert effort.issue is not None
    assert effort.issue.code is PlanningIssueCode.ESTIMATE_EXHAUSTED


def test_no_recorded_work_leaves_the_whole_estimate() -> None:
    effort = compute_remaining_effort(
        task_id=uuid4(), estimated_minutes=300, actual_seconds=0
    )

    assert effort.remaining_minutes == 300
    assert effort.issue is None
