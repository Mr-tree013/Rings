"""Unit tests for planning preferences, planning value objects and interval algebra (ADR-0015)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.config import (
    AssistantConfig,
    Weekday,
    WeeklyAvailabilityRule,
)
from assistant.domain.errors import InvalidAssistantConfig, InvalidPlanBlock, InvalidTimeInterval
from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin
from assistant.domain.planning import (
    PlanningIssue,
    PlanningIssueCode,
    PlanningTask,
    PlanningWindow,
    PlanProposal,
    PlanProposalStatus,
    ProposedPlanBlock,
)
from assistant.domain.planning_intervals import (
    clip_interval,
    merge_intervals,
    subtract_intervals,
)
from assistant.domain.task import TaskPriority

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)


def _planning_mapping(**overrides: object) -> dict[str, object]:
    planning: dict[str, object] = {
        "timezone": "Asia/Shanghai",
        "min_block_minutes": 30,
        "max_block_minutes": 120,
        "deadline_buffer_minutes": 120,
        "availability": [
            {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "22:00"},
            {"days": ["sat", "sun"], "start": "10:00", "end": "22:00"},
        ],
    }
    planning.update(overrides)
    return {"format_version": 1, "planning": planning}


def test_planning_config_parses_with_availability_and_defaults() -> None:
    config = AssistantConfig.from_mapping(_planning_mapping())

    planning = config.planning
    assert planning is not None
    assert planning.timezone == "Asia/Shanghai"
    assert planning.min_block_minutes == 30
    assert planning.max_block_minutes == 120
    assert planning.deadline_buffer_minutes == 120
    assert len(planning.availability) == 2
    assert planning.availability[0].days == (
        Weekday.MON,
        Weekday.TUE,
        Weekday.WED,
        Weekday.THU,
        Weekday.FRI,
    )
    assert (planning.availability[0].start_minute, planning.availability[0].end_minute) == (
        9 * 60,
        22 * 60,
    )


def test_planning_defaults_are_applied_when_only_a_timezone_is_given() -> None:
    config = AssistantConfig.from_mapping({"format_version": 1, "planning": {"timezone": "UTC"}})

    assert config.planning is not None
    assert (config.planning.min_block_minutes, config.planning.max_block_minutes) == (30, 120)
    assert config.planning.deadline_buffer_minutes == 120
    assert config.planning.availability == ()


def test_planning_section_is_optional() -> None:
    assert AssistantConfig.from_mapping({"format_version": 1}).planning is None


@pytest.mark.parametrize(
    "override",
    [
        {"timezone": "UTC+8"},
        {"timezone": "CST"},
        {"timezone": "local"},
        {"timezone": "Asia/Not_A_City"},
        {"timezone": ""},
        {"min_block_minutes": 0},
        {"min_block_minutes": 120, "max_block_minutes": 30},
        {"max_block_minutes": 8 * 60 + 1},
        {"deadline_buffer_minutes": -1},
        {"deadline_buffer_minutes": 7 * 24 * 60 + 1},
        {"min_block_minutes": "30"},
        {"unknown_key": 1},
    ],
)
def test_invalid_planning_config_is_rejected(override: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(_planning_mapping(**override))


@pytest.mark.parametrize(
    "entry",
    [
        {"days": [], "start": "09:00", "end": "10:00"},
        {"days": ["monday"], "start": "09:00", "end": "10:00"},
        {"days": ["mon", "mon"], "start": "09:00", "end": "10:00"},
        {"days": ["mon"], "start": "9:00", "end": "10:00"},
        {"days": ["mon"], "start": "09:00", "end": "09:00"},
        {"days": ["mon"], "start": "22:00", "end": "02:00"},
        {"days": ["mon"], "start": "09:00", "end": "25:00"},
        {"days": ["mon"], "start": "09:00", "end": "10:00", "extra": True},
        {"days": "mon", "start": "09:00", "end": "10:00"},
    ],
)
def test_invalid_availability_rules_are_rejected(entry: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(_planning_mapping(availability=[entry]))


def test_availability_days_are_normalised_to_week_order() -> None:
    config = AssistantConfig.from_mapping(
        _planning_mapping(
            availability=[{"days": ["fri", "mon", "wed"], "start": "09:00", "end": "17:00"}]
        )
    )

    assert config.planning is not None
    assert config.planning.availability[0].days == (
        Weekday.MON,
        Weekday.WED,
        Weekday.FRI,
    )


def test_availability_rule_rejects_overnight_and_accepts_multiple_windows() -> None:
    with pytest.raises(InvalidAssistantConfig):
        WeeklyAvailabilityRule(days=(Weekday.MON,), start_minute=22 * 60, end_minute=2 * 60)
    morning = WeeklyAvailabilityRule(days=(Weekday.MON,), start_minute=9 * 60, end_minute=12 * 60)
    afternoon = WeeklyAvailabilityRule(
        days=(Weekday.MON,), start_minute=14 * 60, end_minute=22 * 60
    )

    assert (morning.end_minute, afternoon.start_minute) == (12 * 60, 14 * 60)


def test_plan_block_provenance_must_be_consistent() -> None:
    with pytest.raises(InvalidPlanBlock, match="MANUAL"):
        PlanBlock(
            task_id=uuid4(),
            starts_at=NOW,
            ends_at=LATER,
            created_at=NOW,
            updated_at=NOW,
            origin=PlanBlockOrigin.MANUAL,
            proposal_id=uuid4(),
        )
    with pytest.raises(InvalidPlanBlock, match="PLANNER"):
        PlanBlock(
            task_id=uuid4(),
            starts_at=NOW,
            ends_at=LATER,
            created_at=NOW,
            updated_at=NOW,
            origin=PlanBlockOrigin.PLANNER,
        )
    planner_block = PlanBlock(
        task_id=uuid4(),
        starts_at=NOW,
        ends_at=LATER,
        created_at=NOW,
        updated_at=NOW,
        origin=PlanBlockOrigin.PLANNER,
        proposal_id=uuid4(),
    )
    assert planner_block.is_planner_generated
    assert plan_block_default_is_manual()


def plan_block_default_is_manual() -> bool:
    manual = PlanBlock(
        task_id=uuid4(), starts_at=NOW, ends_at=LATER, created_at=NOW, updated_at=NOW
    )
    return manual.origin is PlanBlockOrigin.MANUAL and manual.proposal_id is None


def test_planning_window_requires_an_ordered_aware_range() -> None:
    window = PlanningWindow(starts_at=NOW, ends_at=LATER, timezone="Asia/Shanghai")

    assert window.timezone == "Asia/Shanghai"
    with pytest.raises(InvalidTimeInterval):
        PlanningWindow(starts_at=LATER, ends_at=NOW, timezone="Asia/Shanghai")
    with pytest.raises(InvalidTimeInterval, match="timezone-aware"):
        PlanningWindow(
            starts_at=datetime(2026, 9, 21, 9, 0), ends_at=LATER, timezone="UTC"
        )
    with pytest.raises(InvalidTimeInterval, match="IANA"):
        PlanningWindow(starts_at=NOW, ends_at=LATER, timezone="  ")


def test_planning_task_is_a_read_model_not_a_task_entity() -> None:
    names = {field.name for field in fields(PlanningTask)}

    assert names == {
        "task_id",
        "title",
        "priority",
        "created_at",
        "estimated_minutes",
        "actual_seconds",
        "remaining_minutes",
        "deadline",
    }
    task = PlanningTask(
        task_id=uuid4(),
        title="Write report",
        priority=TaskPriority.HIGH,
        created_at=NOW,
        estimated_minutes=300,
        actual_seconds=7200,
        remaining_minutes=180,
        deadline=None,
    )
    assert task.remaining_minutes == 180


def test_proposed_block_validation_and_duration() -> None:
    block = ProposedPlanBlock(task_id=uuid4(), starts_at=NOW, ends_at=LATER, ordinal=0)

    assert block.duration_minutes == 120
    # The pure planner emits scheduling values only; row ids are assigned on persistence.
    assert {field.name for field in fields(ProposedPlanBlock)} == {
        "task_id",
        "starts_at",
        "ends_at",
        "ordinal",
    }
    with pytest.raises(InvalidTimeInterval):
        ProposedPlanBlock(task_id=uuid4(), starts_at=NOW, ends_at=NOW, ordinal=0)
    with pytest.raises(InvalidTimeInterval):
        ProposedPlanBlock(task_id=uuid4(), starts_at=NOW, ends_at=LATER, ordinal=-1)


def test_proposal_validation_and_status_vocabulary() -> None:
    window = PlanningWindow(starts_at=NOW, ends_at=LATER, timezone="UTC")
    proposal = PlanProposal(
        window=window, input_fingerprint="f" * 64, input_revision=3, created_at=NOW
    )

    assert proposal.status is PlanProposalStatus.PENDING
    assert {status.value for status in PlanProposalStatus} == {
        "pending",
        "applied",
        "superseded",
        "stale",
    }
    with pytest.raises(InvalidTimeInterval, match="fingerprint"):
        PlanProposal(window=window, input_fingerprint="  ", input_revision=0, created_at=NOW)
    with pytest.raises(InvalidTimeInterval, match="revision"):
        PlanProposal(window=window, input_fingerprint="f" * 64, input_revision=-1, created_at=NOW)


def test_planning_issues_have_a_fixed_code_vocabulary() -> None:
    assert {code.value for code in PlanningIssueCode} == {
        "MISSING_ESTIMATE",
        "ESTIMATE_EXHAUSTED",
        "DEADLINE_ALREADY_PASSED",
        "INSUFFICIENT_CAPACITY",
        "BUFFER_VIOLATED",
        "NO_AVAILABILITY",
        "WINDOW_CAPACITY_EXHAUSTED",
        "DAILY_CAPACITY_REACHED",
    }
    issue = PlanningIssue(code=PlanningIssueCode.BUFFER_VIOLATED, message="used buffer time")
    assert issue.task_id is None and issue.required_minutes is None


def test_merge_intervals_merges_overlapping_and_adjacent_ranges() -> None:
    ten, eleven, twelve, thirteen = (
        NOW.replace(hour=10),
        NOW.replace(hour=11),
        NOW.replace(hour=12),
        NOW.replace(hour=13),
    )

    merged = merge_intervals([(ten, eleven), (eleven, twelve), (twelve, thirteen)])

    assert merged == [(ten, thirteen)]
    assert merge_intervals([(eleven, twelve), (ten, eleven)]) == [(ten, twelve)]


def test_subtract_intervals_respects_half_open_boundaries() -> None:
    ten, eleven, twelve, thirteen = (
        NOW.replace(hour=10),
        NOW.replace(hour=11),
        NOW.replace(hour=12),
        NOW.replace(hour=13),
    )

    remaining = subtract_intervals([(ten, thirteen)], [(ten, eleven), (twelve, thirteen)])

    assert remaining == [(eleven, twelve)]
    assert subtract_intervals([(ten, eleven)], [(eleven, twelve)]) == [(ten, eleven)]


def test_clip_interval_returns_none_outside_the_bounds() -> None:
    assert clip_interval((NOW, LATER), (NOW, LATER)) == (NOW, LATER)
    assert clip_interval((NOW, LATER), (LATER, LATER + timedelta(hours=1))) is None
    assert clip_interval(
        (NOW, LATER), (NOW + timedelta(hours=1), LATER + timedelta(hours=1))
    ) == (NOW + timedelta(hours=1), LATER)
