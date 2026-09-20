"""The bounded read/write view a local MCP client is allowed to see (ADR-0030).

This module is the whole application surface of the MCP integration, and it deliberately contains
**no MCP SDK** — no decorators, no protocol types, no server. It turns existing services into small
immutable DTOs with fixed fields, and that is the boundary: whatever a client can ask for, it can
only ask through one of these methods, and every method returns a value whose shape was chosen here
rather than by the caller.

What the surface can see:

- status counts and the capability mode;
- open tasks and their fields, one task at a time;
- open cases and their lifecycle fields — never their actions;
- the current planning week's blocks — a read model, not a re-plan;
- optionally, bounded excerpts from the local knowledge index;
- optionally, `TaskService` writes behind an explicitly configured write scope.

What it cannot see is as deliberate: no mail, no drafts, no facts, no playbooks, no action payloads,
no approvals, no executions, no credentials, no physical paths, and no way to reach a browser, a
socket or a shell. The facade imports none of those services, so the absence is structural rather
than a filter applied at the edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from assistant.application.case_service import CaseService
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.planner_service import PlannerService
from assistant.application.task_service import CreateTask, TaskService
from assistant.domain.case import Case, CaseStatus
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    McpInvalidArgument,
    McpToolUnavailable,
    PlanningNotConfigured,
)
from assistant.domain.knowledge import KnowledgeContextHit
from assistant.domain.plan_block import PlanBlock
from assistant.domain.task import Task, TaskId
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.scheduler_repository import SchedulerRepository

LOGGER = logging.getLogger("assistant.mcp")

OPEN_TASK_LIMIT = 50
OPEN_CASE_LIMIT = 50
PLAN_BLOCK_LIMIT = 100
KNOWLEDGE_EXCERPT_CHARS = 1200
KNOWLEDGE_TOTAL_EXCERPT_CHARS = 6000


@dataclass(frozen=True, slots=True)
class McpStatusView:
    """What the local surface is, and how much of it exists on this host."""

    version: str
    write_scope: str
    knowledge_exposure: bool
    open_task_count: int
    open_case_count: int
    unread_notification_count: int
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class McpTaskSummary:
    """One open task, with the fields a task list actually has."""

    id: str
    title: str
    priority: str
    status: str
    estimated_minutes: int | None
    deadline: datetime | None


@dataclass(frozen=True, slots=True)
class McpTaskDetail:
    """One task, with everything the task entity itself carries."""

    id: str
    title: str
    description: str | None
    priority: str
    status: str
    estimated_minutes: int | None
    deadline: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class McpCaseSummary:
    """One case, without its actions: a case is a container, not a payload."""

    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class McpCaseDetail:
    """One case's lifecycle fields. `ActionRequest`s are not part of this view."""

    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None


@dataclass(frozen=True, slots=True)
class McpPlanBlock:
    """One planned block in the current week."""

    task_id: str
    starts_at: datetime
    ends_at: datetime
    origin: str


@dataclass(frozen=True, slots=True)
class McpPlanView:
    """The current planning week, or an honest "not configured"."""

    configured: bool
    timezone: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    blocks: tuple[McpPlanBlock, ...]


@dataclass(frozen=True, slots=True)
class McpKnowledgeHit:
    """One bounded excerpt from the local index."""

    logical_uri: str
    source_span: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class McpKnowledgeResult:
    """The result of one local search, with the roots that could not be searched."""

    query: str
    hits: tuple[McpKnowledgeHit, ...]
    offline_roots: tuple[str, ...]


class McpFacade:
    """The bounded application surface a local MCP client may call."""

    def __init__(
        self,
        tasks: TaskService,
        cases: CaseService,
        commitments: CommitmentRepository,
        planning: PlannerService,
        notifications: SchedulerRepository,
        clock: Clock,
        *,
        version: str,
        write_scope: str = "none",
        expose_knowledge: bool = False,
        knowledge: KnowledgeSearchService | None = None,
    ) -> None:
        self._tasks = tasks
        self._cases = cases
        self._commitments = commitments
        self._planning = planning
        self._notifications = notifications
        self._clock = clock
        self._version = version
        self._write_scope = write_scope
        self._expose_knowledge = expose_knowledge
        self._knowledge = knowledge

    @property
    def write_scope(self) -> str:
        """The configured write scope, as it will be reported to a client."""
        return self._write_scope

    @property
    def allows_task_writes(self) -> bool:
        """Whether the task write methods may be called at all."""
        return self._write_scope == "tasks"

    @property
    def exposes_knowledge(self) -> bool:
        """Whether the knowledge search method may be called at all."""
        return self._expose_knowledge and self._knowledge is not None

    # ------------------------------------------------------------------- status

    async def status(self) -> McpStatusView:
        """Counts and capability mode. No credentials, no content, no paths."""
        tasks = await self._tasks.list_tasks(include_terminal=False)
        cases = await self._cases.list_cases(statuses=(CaseStatus.OPEN,), limit=None)
        notifications = await self._notifications.list_notifications(
            unread_only=True, limit=None
        )
        return McpStatusView(
            version=self._version,
            write_scope=self._write_scope,
            knowledge_exposure=self.exposes_knowledge,
            open_task_count=len(tasks),
            open_case_count=len(cases),
            unread_notification_count=len(notifications),
            capabilities=self.capabilities(),
        )

    def capabilities(self) -> tuple[str, ...]:
        """The exact capabilities this host exposes, as a client would see them."""
        capabilities = ["read:tasks", "read:cases", "read:plan"]
        if self.exposes_knowledge:
            capabilities.append("read:knowledge")
        if self.allows_task_writes:
            capabilities.extend(("write:tasks:create", "write:tasks:complete"))
        return tuple(capabilities)

    # -------------------------------------------------------------------- tasks

    async def open_tasks(self, *, limit: int = OPEN_TASK_LIMIT) -> tuple[McpTaskSummary, ...]:
        """Open tasks in the same deterministic order the CLI uses, bounded."""
        bounded = _bounded_limit(limit, maximum=OPEN_TASK_LIMIT)
        tasks = await self._tasks.list_tasks(include_terminal=False)
        deadlines = await self._deadlines(tasks)
        return tuple(
            McpTaskSummary(
                id=str(task.id),
                title=task.title,
                priority=task.priority.value,
                status=task.status.value,
                estimated_minutes=task.estimated_minutes,
                deadline=None if task.id not in deadlines else deadlines[task.id].due_at,
            )
            for task in tasks[:bounded]
        )

    async def get_task(self, reference: str) -> McpTaskDetail:
        """One task by full UUID or unique prefix.

        Raises:
            TaskNotFound: nothing matches.
            AmbiguousId: the prefix matched several tasks.
            McpInvalidArgument: the reference is blank.
        """
        task = await self._require_task(reference)
        deadline = await self._tasks.get_deadline(task.id)
        return McpTaskDetail(
            id=str(task.id),
            title=task.title,
            description=task.description,
            priority=task.priority.value,
            status=task.status.value,
            estimated_minutes=task.estimated_minutes,
            deadline=None if deadline is None else deadline.due_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    # -------------------------------------------------------------------- cases

    async def open_cases(self, *, limit: int = OPEN_CASE_LIMIT) -> tuple[McpCaseSummary, ...]:
        """Open cases, bounded. The actions inside them are never expanded here."""
        bounded = _bounded_limit(limit, maximum=OPEN_CASE_LIMIT)
        cases = await self._cases.list_cases(statuses=(CaseStatus.OPEN,), limit=bounded)
        return tuple(_case_summary(case) for case in cases)

    async def get_case(self, reference: str) -> McpCaseDetail:
        """One case's lifecycle fields by full UUID or unique prefix.

        Raises:
            CaseNotFound: nothing matches.
            AmbiguousId: the prefix matched several cases.
            McpInvalidArgument: the reference is blank.
        """
        detail = await self._cases.get_case(_require_reference(reference))
        return _case_detail(detail.case)

    # --------------------------------------------------------------------- plan

    async def current_plan(self, *, limit: int = PLAN_BLOCK_LIMIT) -> McpPlanView:
        """The current planning week's blocks, or `configured=False` when planning is off.

        Nothing here plans, applies or replans: the window comes from the existing week convention
        and the blocks come from the existing read model.
        """
        bounded = _bounded_limit(limit, maximum=PLAN_BLOCK_LIMIT)
        try:
            window = self._planning.week_window()
        except PlanningNotConfigured:
            return McpPlanView(
                configured=False, timezone=None, starts_at=None, ends_at=None, blocks=()
            )
        blocks = await self._commitments.list_plan_blocks_in_range(
            query_start=window.starts_at, query_end=window.ends_at
        )
        return McpPlanView(
            configured=True,
            timezone=window.timezone,
            starts_at=window.starts_at,
            ends_at=window.ends_at,
            blocks=tuple(_plan_block(block) for block in blocks[:bounded]),
        )

    # ---------------------------------------------------------------- knowledge

    async def search_knowledge(
        self, query: str, *, root_id: str | None = None, limit: int = 5
    ) -> McpKnowledgeResult:
        """Bounded local full-text search. No model is involved at any point.

        Raises:
            McpToolUnavailable: knowledge exposure is not enabled for this host.
            McpInvalidArgument: the query or the limit is unusable.
        """
        if not self.exposes_knowledge:
            raise McpToolUnavailable("local knowledge search")
        assert self._knowledge is not None  # narrowed by `exposes_knowledge`
        cleaned = query.strip()
        if not cleaned:
            raise McpInvalidArgument("query", "must not be blank")
        if len(cleaned) > 1000:
            raise McpInvalidArgument("query", "must be at most 1000 characters")
        if not 1 <= limit <= 8:
            raise McpInvalidArgument("limit", "must be between 1 and 8")
        result = await self._knowledge.search_context(cleaned, root_id=root_id, limit=limit)
        return McpKnowledgeResult(
            query=cleaned,
            hits=_bounded_hits(result.content_hits),
            offline_roots=tuple(result.offline_roots),
        )

    # ------------------------------------------------------------- task writes

    async def create_task(
        self,
        *,
        title: str,
        priority: str | None = None,
        estimated_minutes: int | None = None,
        deadline: datetime | None = None,
    ) -> McpTaskDetail:
        """Create one task through `TaskService`, exactly as the CLI does.

        Raises:
            McpToolUnavailable: the write scope does not allow task writes.
            InvalidTask: the task breaks its invariants.
        """
        if not self.allows_task_writes:
            raise McpToolUnavailable("task creation")
        from assistant.domain.task import TaskPriority

        chosen = TaskPriority.NORMAL if priority is None else TaskPriority(priority)
        task = await self._tasks.create_task(
            CreateTask(
                title=title,
                priority=chosen,
                estimated_minutes=estimated_minutes,
                due_at=deadline,
            )
        )
        return await self.get_task(str(task.id))

    async def complete_task(self, reference: str) -> McpTaskDetail:
        """Complete one task through `TaskService`, preserving its CAS semantics.

        Raises:
            McpToolUnavailable: the write scope does not allow task writes.
            TaskNotFound, AmbiguousId: the reference resolves to nothing or several tasks.
            TaskNotOpen, StaleTaskUpdate: the task is not completable.
        """
        if not self.allows_task_writes:
            raise McpToolUnavailable("task completion")
        task = await self._require_task(reference)
        result = await self._tasks.complete_task(task.id)
        return await self.get_task(str(result.task.id))

    # ---------------------------------------------------------------- internals

    async def _require_task(self, reference: str) -> Task:
        task_id = await self._tasks.resolve_task_id(_require_reference(reference))
        return await self._tasks.require_task(task_id)

    async def _deadlines(self, tasks: list[Task]) -> dict[TaskId, Deadline]:
        identifiers = [task.id for task in tasks]
        if not identifiers:
            return {}
        return await self._commitments.list_deadlines(identifiers)


def _require_reference(reference: str) -> str:
    if not isinstance(reference, str) or not reference.strip():
        raise McpInvalidArgument("id", "must not be blank")
    return reference.strip()


def _bounded_limit(limit: int, *, maximum: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise McpInvalidArgument("limit", "must be a positive integer")
    return min(limit, maximum)


def _case_summary(case: Case) -> McpCaseSummary:
    return McpCaseSummary(
        id=str(case.id),
        title=case.title,
        status=case.status.value,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _case_detail(case: Case) -> McpCaseDetail:
    return McpCaseDetail(
        id=str(case.id),
        title=case.title,
        status=case.status.value,
        created_at=case.created_at,
        updated_at=case.updated_at,
        completed_at=case.completed_at,
        cancelled_at=case.cancelled_at,
    )


def _plan_block(block: PlanBlock) -> McpPlanBlock:
    return McpPlanBlock(
        task_id=str(block.task_id),
        starts_at=block.starts_at,
        ends_at=block.ends_at,
        origin=block.origin.value,
    )


def _bounded_hits(
    hits: tuple[KnowledgeContextHit, ...],
) -> tuple[McpKnowledgeHit, ...]:
    """Cap each excerpt and the total, so one search cannot return a document."""
    bounded: list[McpKnowledgeHit] = []
    budget = KNOWLEDGE_TOTAL_EXCERPT_CHARS
    for hit in hits:
        if budget <= 0:
            break
        excerpt = hit.content.strip()
        allowed = min(KNOWLEDGE_EXCERPT_CHARS, budget)
        if len(excerpt) > allowed:
            excerpt = excerpt[:allowed] + "…"
        budget -= len(excerpt)
        bounded.append(
            McpKnowledgeHit(
                logical_uri=str(hit.logical_uri),
                source_span=hit.source_span.describe(),
                excerpt=excerpt,
            )
        )
    return tuple(bounded)


__all__ = [
    "KNOWLEDGE_EXCERPT_CHARS",
    "KNOWLEDGE_TOTAL_EXCERPT_CHARS",
    "OPEN_CASE_LIMIT",
    "OPEN_TASK_LIMIT",
    "PLAN_BLOCK_LIMIT",
    "McpCaseDetail",
    "McpCaseSummary",
    "McpFacade",
    "McpKnowledgeHit",
    "McpKnowledgeResult",
    "McpPlanBlock",
    "McpPlanView",
    "McpStatusView",
    "McpTaskDetail",
    "McpTaskSummary",
]
