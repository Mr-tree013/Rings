"""Planning domain: deterministic proposals, not silent plan changes (ADR-0015).

The planner is a pure function over a read model; its output is a *durable proposal* that the
user reviews before anything is written to `plan_blocks`. Nothing here reads the clock, the
database or a calendar service.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.errors import InvalidTimeInterval
from assistant.domain.plan_block import PlanBlock
from assistant.domain.planning_preferences import DailyWindow, ProposalMode
from assistant.domain.task import Task, TaskId, TaskPriority

PlanProposalId = UUID
"""Stable identity of one plan proposal."""

PlanningIssueId = UUID
"""Stable identity of one recorded planning issue."""


def new_proposal_id() -> PlanProposalId:
    return uuid4()


def new_planning_issue_id() -> PlanningIssueId:
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTimeInterval(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PlanningWindow:
    """The half-open instant range being planned, plus the timezone it is reasoned in."""

    starts_at: datetime
    ends_at: datetime
    timezone: str

    def __post_init__(self) -> None:
        _require_aware(self.starts_at, "starts_at")
        _require_aware(self.ends_at, "ends_at")
        if self.ends_at <= self.starts_at:
            raise InvalidTimeInterval("planning window ends_at must be after starts_at")
        if not self.timezone.strip():
            raise InvalidTimeInterval("planning window needs an IANA timezone")


@dataclass(frozen=True, slots=True)
class PlanningTask:
    """Read model for one OPEN task as the planner sees it."""

    task_id: TaskId
    title: str
    priority: TaskPriority
    created_at: datetime
    estimated_minutes: int | None
    actual_seconds: int
    remaining_minutes: int
    deadline: datetime | None = None


class PlanningIssueCode(StrEnum):
    """Why a task could not be planned exactly as hoped."""

    MISSING_ESTIMATE = "MISSING_ESTIMATE"
    ESTIMATE_EXHAUSTED = "ESTIMATE_EXHAUSTED"
    DEADLINE_ALREADY_PASSED = "DEADLINE_ALREADY_PASSED"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"
    BUFFER_VIOLATED = "BUFFER_VIOLATED"
    NO_AVAILABILITY = "NO_AVAILABILITY"
    WINDOW_CAPACITY_EXHAUSTED = "WINDOW_CAPACITY_EXHAUSTED"
    DAILY_CAPACITY_REACHED = "DAILY_CAPACITY_REACHED"
    """The user's own daily limit stopped the plan, and the remainder is honestly unscheduled."""


@dataclass(frozen=True, slots=True)
class PlanningIssue:
    """A deterministic, user-readable explanation attached to a proposal."""

    code: PlanningIssueCode
    message: str
    task_id: TaskId | None = None
    required_minutes: int | None = None
    scheduled_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class ProposedPlanBlock:
    """A planned interval inside a proposal; not yet a real plan block.

    Deliberately carries no identity: the pure planner emits only scheduling values, and the
    persistence layer assigns row ids when the proposal is stored.
    """

    task_id: TaskId
    starts_at: datetime
    ends_at: datetime
    ordinal: int

    def __post_init__(self) -> None:
        _require_aware(self.starts_at, "starts_at")
        _require_aware(self.ends_at, "ends_at")
        if self.ends_at <= self.starts_at:
            raise InvalidTimeInterval("proposed block ends_at must be after starts_at")
        if self.ordinal < 0:
            raise InvalidTimeInterval("proposed block ordinal must not be negative")

    @property
    def duration_minutes(self) -> int:
        """Elapsed minutes of the proposal block."""
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


class PlanProposalStatus(StrEnum):
    """Lifecycle of a proposal."""

    PENDING = "pending"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A durable, reviewable plan."""

    window: PlanningWindow
    input_fingerprint: str
    input_revision: int
    created_at: datetime
    id: PlanProposalId = field(default_factory=new_proposal_id)
    status: PlanProposalStatus = PlanProposalStatus.PENDING
    mode: ProposalMode = ProposalMode.NORMAL
    """What this proposal intends to do with the plan that already exists (ADR-0044 §39).
    """
    applied_at: datetime | None = None
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if not self.input_fingerprint.strip():
            raise InvalidTimeInterval("a proposal needs an input fingerprint")
        if self.input_revision < 0:
            raise InvalidTimeInterval("a proposal needs a non-negative input revision")


@dataclass(frozen=True, slots=True)
class PlanProposalDetail:
    """A proposal together with everything it proposed and reported."""

    proposal: PlanProposal
    blocks: tuple[ProposedPlanBlock, ...]
    issues: tuple[PlanningIssue, ...]


@dataclass(frozen=True, slots=True)
class PlanProposalSummary:
    """A proposal plus how much it proposed, for list views."""

    proposal: PlanProposal
    block_count: int
    issue_count: int


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What the pure planner produced for one request."""

    blocks: tuple[ProposedPlanBlock, ...]
    issues: tuple[PlanningIssue, ...]


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    """Everything the pure planner needs, and nothing it could use to reach the database."""

    window: PlanningWindow
    tasks: tuple[PlanningTask, ...]
    availability: tuple[tuple[datetime, datetime], ...]
    busy_intervals: tuple[tuple[datetime, datetime], ...]
    min_block_minutes: int
    max_block_minutes: int
    deadline_buffer_minutes: int
    preferred_block_minutes: int = 0
    """The normal length of one sitting. `0` means "use `max_block_minutes`".

    It defaults to zero rather than to a number so that a caller which has not been told what the
    user prefers plans exactly as it did before this preference existed, instead of quietly
    shortening every block.
    """
    daily_capacity: tuple[DailyWindow, ...] = ()
    """Local days the plan may use, with their own minute budget. Empty means "availability only".

    Each window is the planner's whole picture of a day: it is why a personal rule such as "at most
    six hours" is a hard constraint rather than a suggestion, and why nothing can be placed after
    the user's own day has ended.
    """


@dataclass(frozen=True, slots=True)
class PlanningSnapshot:
    """Planner-relevant authoritative state, read from one consistent transaction."""

    window: PlanningWindow
    revision: int
    open_tasks: tuple[Task, ...]
    deadlines: Mapping[TaskId, Deadline]
    actual_work_seconds: Mapping[TaskId, int]
    active_calendar_events: tuple[CalendarEvent, ...]
    active_manual_plan_blocks: tuple[PlanBlock, ...]
    active_planner_plan_blocks: tuple[PlanBlock, ...]


__all__ = [
    "DailyWindow",
    "PlanProposal",
    "PlanProposalDetail",
    "PlanProposalId",
    "PlanProposalStatus",
    "PlanProposalSummary",
    "PlanResult",
    "PlanningIssue",
    "PlanningIssueCode",
    "PlanningRequest",
    "PlanningSnapshot",
    "PlanningTask",
    "PlanningWindow",
    "ProposalMode",
    "ProposedPlanBlock",
    "new_planning_issue_id",
    "new_proposal_id",
]
