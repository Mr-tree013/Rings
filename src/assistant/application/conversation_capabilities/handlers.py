"""Handlers: the only place where a conversation turn touches an application service (ADR-0033 §8).

Every handler calls the service that already owns the rule — `TaskService`, `CalendarService`,
`WorkService`, `PlannerService`, `GroundedAnswerService` and the scheduler repository. There is no
SQL here, no second task model and no second planner: the conversation layer is an orchestration
adapter over the core, so a task created from a sentence is created by exactly the code that
creates one from `pw task add`.

Handlers return structured `OperationResult` data. Turning that into Chinese prose is the
renderer's job (`conversation_render.py`), which keeps this module about calling services.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from assistant.application.calendar_service import CalendarService, CreateCalendarEvent
from assistant.application.conversation_capabilities.registry import (
    ConfirmationPolicy,
    ConversationCapability,
    ConversationCapabilityRegistry,
    OperationResult,
)
from assistant.application.grounded_answer import GroundedAnswerService
from assistant.application.planner_service import PlannerService
from assistant.application.task_service import CreateTask, EditTask, TaskService
from assistant.application.work_service import WorkService
from assistant.domain.conversation_plan import (
    CalendarCreateArguments,
    CalendarListArguments,
    ConversationOperationArguments,
    ConversationOperationType,
    KnowledgeAskArguments,
    NotificationListArguments,
    NotificationReadArguments,
    PlanApplyProposalArguments,
    PlanCurrentArguments,
    PlanProposeWeekArguments,
    StatusGetArguments,
    TaskClearDeadlineArguments,
    TaskCompleteArguments,
    TaskCreateArguments,
    TaskEditArguments,
    TaskListArguments,
    TaskSetDeadlineArguments,
    TaskShowArguments,
    WorkRecordArguments,
)
from assistant.domain.deadline import Deadline
from assistant.domain.grounded_answer import KnowledgeEvidence
from assistant.domain.knowledge import SourceSpanKind
from assistant.domain.planning import (
    PlanProposalDetail,
    PlanProposalStatus,
    PlanProposalSummary,
)
from assistant.domain.task import Task, TaskId, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.scheduler_repository import SchedulerRepository

CALENDAR_LIST_LIMIT = 60
"""How many events and plan blocks one `calendar.list` answer may carry."""

NOTIFICATION_LIST_LIMIT = 20
"""How many notifications one `notification.list` answer may carry."""


class ConversationHandlers:
    """Executes one allowed operation against the existing application services."""

    def __init__(
        self,
        *,
        tasks: TaskService,
        calendar: CalendarService,
        work: WorkService,
        planner: PlannerService,
        scheduler: SchedulerRepository,
        knowledge: GroundedAnswerService,
        commitments: CommitmentRepository,
        clock: Clock,
        knowledge_limit: int = 8,
    ) -> None:
        self._tasks = tasks
        self._calendar = calendar
        self._work = work
        self._planner = planner
        self._scheduler = scheduler
        self._knowledge = knowledge
        self._commitments = commitments
        self._clock = clock
        self._knowledge_limit = knowledge_limit

    # ------------------------------------------------------------------------- reads

    async def status_get(self, arguments: ConversationOperationArguments) -> OperationResult:
        _expect(StatusGetArguments, arguments)
        now = self._clock.now()
        open_tasks = await self._tasks.list_tasks()
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in open_tasks])
            if open_tasks
            else {}
        )
        next_entry = _next_deadline(open_tasks, deadlines)
        unread = await self._scheduler.list_notifications(unread_only=True, limit=50)
        pending = await self._latest_pending_proposal()
        return OperationResult(
            kind="status",
            data={
                "open_tasks": len(open_tasks),
                "next_deadline": next_entry,
                "unread_notifications": len(unread),
                "pending_proposal": None if pending is None else _proposal_payload(pending)[0],
                "now": now.isoformat(),
            },
        )

    async def task_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        include_terminal = _expect(TaskListArguments, arguments).include_terminal
        tasks = await self._tasks.list_tasks(include_terminal=include_terminal)
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in tasks]) if tasks else {}
        )
        actual = (
            await self._work.actual_seconds_for_tasks([task.id for task in tasks])
            if tasks
            else {}
        )
        return OperationResult(
            kind="tasks",
            data={
                "tasks": [
                    _task_payload(task, deadlines.get(task.id), actual.get(task.id, 0))
                    for task in tasks
                ]
            },
        )

    async def task_show(self, arguments: ConversationOperationArguments) -> OperationResult:
        task_id = _expect(TaskShowArguments, arguments).task_id
        task = await self._tasks.require_task(task_id)
        deadline = await self._tasks.get_deadline(task_id)
        actual = await self._work.get_task_actual_seconds(task_id)
        return OperationResult(
            kind="task",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, actual)},
        )

    async def calendar_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        days = _expect(CalendarListArguments, arguments).days
        now = self._clock.now()
        end = now + timedelta(days=days)
        events = await self._calendar.list_events(query_start=now, query_end=end)
        blocks = await self._commitments.list_plan_blocks_in_range(query_start=now, query_end=end)
        return OperationResult(
            kind="calendar",
            data={
                "window_days": days,
                "events": [
                    _interval_payload(event.title, event.starts_at, event.ends_at)
                    for event in events[:CALENDAR_LIST_LIMIT]
                ],
                "plan_blocks": [
                    _interval_payload(
                        str(block.task_id), block.starts_at, block.ends_at
                    )
                    for block in blocks[:CALENDAR_LIST_LIMIT]
                ],
            },
        )

    async def plan_current(self, arguments: ConversationOperationArguments) -> OperationResult:
        _expect(PlanCurrentArguments, arguments)
        summary = await self._latest_pending_proposal()
        if summary is None:
            return OperationResult(kind="proposal_absent", data={})
        detail = await self._planner.get_proposal_detail(str(summary.proposal.id))
        return OperationResult(kind="proposal", ref=str(summary.proposal.id), data=_detail(detail))

    async def notification_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        unread_only = _expect(NotificationListArguments, arguments).unread_only
        notifications = await self._scheduler.list_notifications(
            unread_only=unread_only, limit=NOTIFICATION_LIST_LIMIT
        )
        return OperationResult(
            kind="notifications",
            data={
                "notifications": [
                    {
                        "id": str(notification.id),
                        "title": notification.title,
                        "kind": notification.kind.value,
                        "created_at": notification.created_at.isoformat(),
                    }
                    for notification in notifications
                ]
            },
        )

    async def knowledge_ask(self, arguments: ConversationOperationArguments) -> OperationResult:
        asked = _expect(KnowledgeAskArguments, arguments)
        result = await self._knowledge.answer(
            asked.question, root_id=asked.root_id, limit=self._knowledge_limit
        )
        by_id = result.evidence_by_id()
        sources = [
            {
                "id": str(evidence.id),
                "label": str(evidence.logical_uri),
                "span": _span(evidence),
            }
            for evidence in result.evidence
        ]
        return OperationResult(
            kind="answer",
            data={
                "status": result.answer.status.value,
                "segments": [
                    {"text": segment.text, "sources": [str(s) for s in segment.source_ids]}
                    for segment in result.answer.segments
                ],
                "reason": result.answer.reason,
                "sources": sources,
                "offline_roots": list(result.offline_roots),
                "cited": sorted(
                    {
                        str(source_id)
                        for segment in result.answer.segments
                        for source_id in segment.source_ids
                        if source_id in by_id
                    }
                ),
            },
        )

    # ------------------------------------------------------------------------ writes

    async def task_create(self, arguments: ConversationOperationArguments) -> OperationResult:
        created = _expect(TaskCreateArguments, arguments)
        task = await self._tasks.create_task(
            CreateTask(
                title=created.title,
                description=created.description,
                priority=created.priority,
                estimated_minutes=created.estimated_minutes,
                due_at=created.due_at,
            )
        )
        deadline = await self._tasks.get_deadline(task.id)
        return OperationResult(
            kind="task_created",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, 0)},
        )

    async def task_edit(self, arguments: ConversationOperationArguments) -> OperationResult:
        edited = _expect(TaskEditArguments, arguments)
        current = await self._tasks.require_task(edited.task_id)
        task = await self._tasks.update_task(
            current.id,
            EditTask(
                title=current.title if edited.title is None else edited.title,
                description=current.description,
                priority=current.priority if edited.priority is None else edited.priority,
                estimated_minutes=(
                    current.estimated_minutes
                    if edited.estimated_minutes is None
                    else edited.estimated_minutes
                ),
            ),
        )
        deadline = await self._tasks.get_deadline(task.id)
        return OperationResult(
            kind="task_updated",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, 0)},
        )

    async def task_set_deadline(self, arguments: ConversationOperationArguments) -> OperationResult:
        set_deadline = _expect(TaskSetDeadlineArguments, arguments)
        task = await self._tasks.require_task(set_deadline.task_id)
        deadline = await self._tasks.set_deadline(task.id, set_deadline.due_at)
        return OperationResult(
            kind="deadline_set",
            ref=str(task.id),
            data={"title": task.title, "due_at": deadline.due_at.isoformat()},
        )

    async def task_clear_deadline(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        clear = _expect(TaskClearDeadlineArguments, arguments)
        task = await self._tasks.require_task(clear.task_id)
        await self._tasks.clear_deadline(task.id)
        return OperationResult(
            kind="deadline_cleared", ref=str(task.id), data={"title": task.title}
        )

    async def task_complete(self, arguments: ConversationOperationArguments) -> OperationResult:
        complete = _expect(TaskCompleteArguments, arguments)
        task = await self._tasks.require_task(complete.task_id)
        await self._tasks.complete_task(task.id)
        return OperationResult(
            kind="task_completed", ref=str(task.id), data={"title": task.title}
        )

    async def calendar_create(self, arguments: ConversationOperationArguments) -> OperationResult:
        created = _expect(CalendarCreateArguments, arguments)
        event = await self._calendar.create_event(
            CreateCalendarEvent(
                title=created.title,
                starts_at=created.starts_at,
                ends_at=created.ends_at,
                description=created.description,
            )
        )
        return OperationResult(
            kind="event_created",
            ref=str(event.id),
            data=_interval_payload(event.title, event.starts_at, event.ends_at),
        )

    async def work_record(self, arguments: ConversationOperationArguments) -> OperationResult:
        recorded = _expect(WorkRecordArguments, arguments)
        session = await self._work.record_session(
            task_id=recorded.task_id,
            started_at=recorded.started_at,
            ended_at=recorded.ended_at,
        )
        task = await self._tasks.require_task(session.task_id)
        seconds = int((session.ended_at - session.started_at).total_seconds())
        return OperationResult(
            kind="work_recorded",
            ref=str(session.id),
            data={
                "title": task.title,
                "task_id": str(task.id),
                "minutes": seconds // 60,
                "started_at": session.started_at.isoformat(),
                "ended_at": session.ended_at.isoformat(),
            },
        )

    async def plan_propose_week(self, arguments: ConversationOperationArguments) -> OperationResult:
        proposed = _expect(PlanProposeWeekArguments, arguments)
        detail = await self._planner.create_week_proposal(next_week=proposed.next_week)
        return OperationResult(
            kind="proposal", ref=str(detail.proposal.id), data=_detail(detail)
        )

    async def plan_apply_proposal(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        applied = _expect(PlanApplyProposalArguments, arguments)
        reference = applied.proposal_id
        if reference is None:
            summary = await self._latest_pending_proposal()
            if summary is None:
                return OperationResult(kind="proposal_absent", data={})
            reference = str(summary.proposal.id)
        result = await self._planner.apply_proposal(reference)
        detail = await self._planner.get_proposal_detail(str(result.proposal.id))
        payload = _detail(detail)
        payload.update(
            {
                "applied": True,
                "created_blocks": result.created_blocks,
                "replaced_blocks": result.replaced_blocks,
            }
        )
        return OperationResult(kind="proposal_applied", ref=str(result.proposal.id), data=payload)

    async def notification_read(self, arguments: ConversationOperationArguments) -> OperationResult:
        read = _expect(NotificationReadArguments, arguments)
        notification_id = await self._scheduler.resolve_notification_id(read.notification_id)
        notification = await self._scheduler.mark_notification_read(
            notification_id, at=self._clock.now()
        )
        return OperationResult(
            kind="notification_read",
            ref=str(notification.id),
            data={"title": notification.title},
        )

    # ----------------------------------------------------------------------- helpers

    async def _latest_pending_proposal(self) -> PlanProposalSummary | None:
        summaries = await self._planner.list_proposal_summaries(limit=20)
        for summary in summaries:
            if summary.proposal.status is PlanProposalStatus.PENDING:
                return summary
        return None


def build_phase_10a_registry(handlers: ConversationHandlers) -> ConversationCapabilityRegistry:
    """The frozen Phase 10A capability set, with its code-owned confirmation policies."""
    handler = Callable[..., Awaitable[OperationResult]]
    entries: tuple[tuple[ConversationOperationType, ConfirmationPolicy, handler], ...] = (
        (ConversationOperationType.STATUS_GET, ConfirmationPolicy.READ, handlers.status_get),
        (ConversationOperationType.TASK_LIST, ConfirmationPolicy.READ, handlers.task_list),
        (ConversationOperationType.TASK_SHOW, ConfirmationPolicy.READ, handlers.task_show),
        (ConversationOperationType.CALENDAR_LIST, ConfirmationPolicy.READ, handlers.calendar_list),
        (ConversationOperationType.PLAN_CURRENT, ConfirmationPolicy.READ, handlers.plan_current),
        (
            ConversationOperationType.NOTIFICATION_LIST,
            ConfirmationPolicy.READ,
            handlers.notification_list,
        ),
        (
            ConversationOperationType.KNOWLEDGE_ASK,
            ConfirmationPolicy.READ,
            handlers.knowledge_ask,
        ),
        (
            ConversationOperationType.TASK_CREATE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_create,
        ),
        (ConversationOperationType.TASK_EDIT, ConfirmationPolicy.LOCAL_WRITE, handlers.task_edit),
        (
            ConversationOperationType.TASK_SET_DEADLINE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_set_deadline,
        ),
        (
            ConversationOperationType.TASK_CLEAR_DEADLINE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_clear_deadline,
        ),
        (
            ConversationOperationType.TASK_COMPLETE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_complete,
        ),
        (
            ConversationOperationType.CALENDAR_CREATE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.calendar_create,
        ),
        (
            ConversationOperationType.WORK_RECORD,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.work_record,
        ),
        (
            ConversationOperationType.PLAN_PROPOSE_WEEK,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.plan_propose_week,
        ),
        (
            ConversationOperationType.PLAN_APPLY_PROPOSAL,
            ConfirmationPolicy.CONFIRM_LOCAL,
            handlers.plan_apply_proposal,
        ),
        (
            ConversationOperationType.NOTIFICATION_READ,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.notification_read,
        ),
    )
    return ConversationCapabilityRegistry(
        ConversationCapability(operation_type=kind, policy=policy, handler=handler)
        for kind, policy, handler in entries
    )


def _expect[ArgumentsT: ConversationOperationArguments](
    expected: type[ArgumentsT], arguments: ConversationOperationArguments
) -> ArgumentsT:
    """Typed dispatch: a mismatch is a programming error, not a model error."""
    if not isinstance(arguments, expected):
        raise AssertionError(  # pragma: no cover - the registry and the plan agree by construction
            f"handler expected {expected.__name__}, got {type(arguments).__name__}"
        )
    return arguments


def _task_payload(
    task: Task, deadline: Deadline | None, actual_seconds: int
) -> dict[str, object]:
    due_at = None if deadline is None else deadline.due_at
    return {
        "id": str(task.id),
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "estimated_minutes": task.estimated_minutes,
        "due_at": None if due_at is None else due_at.isoformat(),
        "actual_seconds": actual_seconds,
        "description": task.description,
    }


def _interval_payload(title: str, starts_at: datetime, ends_at: datetime) -> dict[str, object]:
    return {"title": title, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}


def _next_deadline(
    tasks: list[Task], deadlines: dict[TaskId, Deadline]
) -> dict[str, str] | None:
    open_tasks = [task for task in tasks if task.status is TaskStatus.OPEN]
    pairs = [
        (task, deadlines.get(task.id))
        for task in open_tasks
        if deadlines.get(task.id) is not None
    ]
    if not pairs:
        return None
    task, deadline = min(pairs, key=lambda entry: _due_at(entry[1]))
    return {"title": task.title, "due_at": _due_at(deadline).isoformat()}


def _due_at(deadline: Deadline | None) -> datetime:
    if deadline is None:  # pragma: no cover - callers filter `None` out first
        raise AssertionError("a deadline is required here")
    return deadline.due_at


def _detail(detail: PlanProposalDetail) -> dict[str, object]:
    proposal = detail.proposal
    blocks = detail.blocks
    issues = detail.issues
    return {
        "id": str(proposal.id),
        "window_starts_at": proposal.window.starts_at.isoformat(),
        "window_ends_at": proposal.window.ends_at.isoformat(),
        "timezone": proposal.window.timezone,
        "status": proposal.status.value,
        "block_count": len(blocks),
        "issue_count": len(issues),
        "blocks": [
            _interval_payload(str(block.task_id), block.starts_at, block.ends_at)
            for block in blocks[:10]
        ],
        "issues": [
            {"code": issue.code.value, "message": issue.message} for issue in issues[:5]
        ],
    }


def _proposal_payload(summary: PlanProposalSummary) -> tuple[str, dict[str, object]]:
    proposal = summary.proposal
    return (
        str(proposal.id),
        {
            "id": str(proposal.id),
            "window_starts_at": proposal.window.starts_at.isoformat(),
            "window_ends_at": proposal.window.ends_at.isoformat(),
            "block_count": summary.block_count,
            "issue_count": summary.issue_count,
        },
    )


def _span(evidence: KnowledgeEvidence) -> str:
    span = evidence.source_span
    if span.kind is SourceSpanKind.LINE and span.line_start is not None:
        return f"lines {span.line_start}-{span.line_end}"
    return f"page {span.page_number}"


__all__ = ["ConversationHandlers", "build_phase_10a_registry"]
