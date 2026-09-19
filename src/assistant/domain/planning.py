"""Planning domain: deterministic proposals, not silent plan changes (ADR-0015).

The planner is a pure function over a read model; its output is a *durable proposal* that the
user reviews before anything is written to `plan_blocks`. Nothing here reads the clock, the
database or a calendar service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidTimeInterval
from assistant.domain.task import TaskId, TaskPriority

PlanProposalId = UUID
"""Stable identity of one plan proposal."""

ProposedPlanBlockId = UUID
"""Stable identity of one proposed block."""

PlanningIssueId = UUID
"""Stable identity of one recorded planning issue."""


def new_proposal_id() -> PlanProposalId:
    return uuid4()


def new_proposed_block_id() -> ProposedPlanBlockId:
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
    """A planned interval inside a proposal; not yet a real plan block."""

    task_id: TaskId
    starts_at: datetime
    ends_at: datetime
    ordinal: int
    id: ProposedPlanBlockId = field(default_factory=new_proposed_block_id)

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
class PlanResult:
    """What the pure planner produced for one request."""

    blocks: tuple[ProposedPlanBlock, ...]
    issues: tuple[PlanningIssue, ...]


__all__ = [
    "PlanProposal",
    "PlanProposalDetail",
    "PlanProposalId",
    "PlanProposalStatus",
    "PlanResult",
    "PlanningIssue",
    "PlanningIssueCode",
    "PlanningTask",
    "PlanningWindow",
    "ProposedPlanBlock",
    "ProposedPlanBlockId",
    "new_planning_issue_id",
    "new_proposal_id",
    "new_proposed_block_id",
]

