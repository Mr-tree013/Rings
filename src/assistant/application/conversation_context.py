"""The bounded context one conversation turn is allowed to see (ADR-0033 §10-11).

Read-only by construction: this builder is given repositories and a clock, and nothing else. It
cannot mutate, cannot call a model and cannot reach an action, an approval or a mail body.

The bounded policy is explicit and tested:

* at most `MAX_CONTEXT_MESSAGES` recent messages, **and**
* at most `MAX_CONTEXT_HISTORY_CHARS` characters of them, taken newest first, so a long
  conversation degrades by dropping its oldest context instead of overflowing the request;
* at most `MAX_CONTEXT_ENTITIES_PER_KIND` tasks, proposals, calendar events and notifications,
  ordered deterministically, because the same state must produce the same context.

What is *not* here matters as much as what is: no mail bodies, no indexed document text, no
`ConfirmedFact`s, no Playbooks, no Action payloads, no Approval data and no credentials. Knowledge
reaches a turn only through `knowledge.ask`, which goes to the grounded-answer service and comes
back with citations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.domain.conversation import ConversationMessage, ConversationThreadId
from assistant.domain.conversation_context import (
    MAX_CONTEXT_ENTITIES_PER_KIND,
    MAX_CONTEXT_HISTORY_CHARS,
    MAX_CONTEXT_MESSAGES,
    ConversationContext,
    ConversationEntityKind,
    ConversationEntityRef,
    ConversationRecentMessage,
)
from assistant.domain.deadline import Deadline
from assistant.domain.notification import NotificationStatus
from assistant.domain.planning import PlanProposal, PlanProposalStatus
from assistant.domain.recurring_calendar import (
    RecurringCalendarRule,
    RecurringCalendarRuleStatus,
    format_clock,
)
from assistant.domain.task import Task, TaskId, TaskPriority, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.mail_draft_repository import MailDraftRepository
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository
from assistant.ports.planning_repository import PlanningRepository
from assistant.ports.recurring_calendar_repository import RecurringCalendarRepository
from assistant.ports.scheduler_repository import SchedulerRepository

CALENDAR_LOOKAHEAD_DAYS = 14
"""How far ahead calendar events are summarised for a follow-up reference."""

_MAIL_ENTITY_BOUND = 12
"""How many recent messages, threads and drafts a follow-up may refer to."""

_PRIORITY_RANK = {
    TaskPriority.HIGH: 0,
    TaskPriority.NORMAL: 1,
    TaskPriority.LOW: 2,
}


class ConversationContextBuilder:
    """Reads the bounded context from authoritative state. It can only read."""

    def __init__(
        self,
        repository: ConversationRepository,
        commitments: CommitmentRepository,
        planning: PlanningRepository,
        scheduler: SchedulerRepository,
        clock: Clock,
        *,
        planning_timezone: str | None,
        capability_snapshot: dict[str, object] | None = None,
        mail: MailRepository | None = None,
        mail_intelligence: MailIntelligenceRepository | None = None,
        mail_drafts: MailDraftRepository | None = None,
        recurring: RecurringCalendarRepository | None = None,
        max_messages: int = MAX_CONTEXT_MESSAGES,
        max_history_chars: int = MAX_CONTEXT_HISTORY_CHARS,
        max_entities: int = MAX_CONTEXT_ENTITIES_PER_KIND,
    ) -> None:
        if max_messages < 1 or max_history_chars < 1 or max_entities < 1:
            raise ValueError("every conversation context bound must be at least 1")
        self._repository = repository
        self._commitments = commitments
        self._planning = planning
        self._scheduler = scheduler
        self._clock = clock
        self._planning_timezone = planning_timezone
        self._capability_snapshot = capability_snapshot
        self._mail = mail
        self._mail_intelligence = mail_intelligence
        self._mail_drafts = mail_drafts
        self._recurring = recurring
        self._max_messages = max_messages
        self._max_history_chars = max_history_chars
        self._max_entities = max_entities

    async def build(
        self,
        thread_id: ConversationThreadId,
        *,
        confirmation_pending: bool = False,
    ) -> ConversationContext:
        """Return the same context for the same state, every time."""
        now = self._clock.now()
        messages = await self._repository.list_messages(thread_id)
        history, truncated = _recent_history(
            messages, max_messages=self._max_messages, max_chars=self._max_history_chars
        )
        entities = await self._entities(now)
        return ConversationContext(
            current_time=now,
            planning_timezone=self._planning_timezone,
            recent_messages=history,
            entities=entities,
            history_truncated=truncated,
            confirmation_pending=confirmation_pending,
            capabilities=self._capability_snapshot,
        )

    async def _entities(self, now: datetime) -> tuple[ConversationEntityRef, ...]:
        return (
            *await self._task_entities(),
            *await self._proposal_entities(),
            *await self._calendar_entities(now),
            *await self._recurring_entities(),
            *await self._notification_entities(),
            *await self._mail_entities(),
        )

    async def _task_entities(self) -> tuple[ConversationEntityRef, ...]:
        tasks = await self._commitments.list_tasks(statuses=(TaskStatus.OPEN,))
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in tasks]) if tasks else {}
        )
        ordered = _order_tasks(tasks, deadlines)[: self._max_entities]
        return tuple(
            ConversationEntityRef(
                kind=ConversationEntityKind.TASK,
                id=str(task.id),
                label=task.title,
                detail=_task_detail(task, deadlines.get(task.id)),
            )
            for task in ordered
        )

    async def _proposal_entities(self) -> tuple[ConversationEntityRef, ...]:
        summaries = await self._planning.list_proposal_summaries(limit=self._max_entities * 2)
        pending = [
            summary
            for summary in summaries
            if summary.proposal.status is PlanProposalStatus.PENDING
        ][: self._max_entities]
        return tuple(
            ConversationEntityRef(
                kind=ConversationEntityKind.PROPOSAL,
                id=str(summary.proposal.id),
                label=_proposal_label(summary.proposal),
                detail=f"blocks={summary.block_count}, issues={summary.issue_count}",
            )
            for summary in pending
        )

    async def _calendar_entities(self, now: datetime) -> tuple[ConversationEntityRef, ...]:
        events = await self._commitments.list_calendar_events(
            query_start=now,
            query_end=now + timedelta(days=CALENDAR_LOOKAHEAD_DAYS),
        )
        return tuple(
            ConversationEntityRef(
                kind=ConversationEntityKind.CALENDAR_EVENT,
                id=str(event.id),
                label=event.title,
                detail=f"starts={event.starts_at.isoformat()} ends={event.ends_at.isoformat()}",
            )
            for event in events[: self._max_entities]
        )

    async def _recurring_entities(self) -> tuple[ConversationEntityRef, ...]:
        """The user's standing weekly commitments, bounded and without any derived occurrence.

        A follow-up like "把刚才那门课改成九点到十一点" is possible because the short id is here;
        what the rule expands to is never sent, because occurrences are unbounded in principle and
        the model has no use for a list of dates (ADR-0036 §10).
        """
        if self._recurring is None:
            return ()
        rules = await self._recurring.list_rules(
            status=RecurringCalendarRuleStatus.ACTIVE
        )
        # The newest commitments are the ones a follow-up can mean; keep reading order.
        recent = rules[-self._max_entities :]
        return tuple(
            ConversationEntityRef(
                kind=ConversationEntityKind.RECURRING_CALENDAR_RULE,
                id=str(rule.id)[:8],
                label=rule.title,
                detail=_recurring_detail(rule),
            )
            for rule in recent
        )

    async def _notification_entities(self) -> tuple[ConversationEntityRef, ...]:
        notifications = await self._scheduler.list_notifications(
            unread_only=True, limit=self._max_entities
        )
        return tuple(
            ConversationEntityRef(
                kind=ConversationEntityKind.NOTIFICATION,
                id=str(notification.id),
                label=notification.title,
                detail=f"kind={notification.kind.value} status={NotificationStatus.UNREAD.value}",
            )
            for notification in notifications
        )

    async def _mail_entities(self) -> tuple[ConversationEntityRef, ...]:
        """Recent mail metadata — never the bodies, never the whole mailbox (ADR-0034 §8).

        A follow-up like "回复刚才那封" is possible because the message ids are here; what the
        message actually says is fetched through `mail.show` when the user asks for it.
        """
        if self._mail is None or self._mail_intelligence is None:
            return ()
        entities: list[ConversationEntityRef] = []
        for message in await self._mail.list_messages(limit=self._max_entities):
            analysis = await self._mail_intelligence.get_analysis(message.id)
            label = f"{message.from_address or '（未知发件人）'}：{message.subject or '（无主题）'}"
            detail = [f"received={_instant(message.sent_at or message.first_seen_at)}"]
            if analysis is not None:
                detail.append(f"category={analysis.category.value}")
                detail.append(f"requires_reply={str(analysis.requires_reply).lower()}")
            entities.append(
                ConversationEntityRef(
                    kind=ConversationEntityKind.MAIL_MESSAGE,
                    id=str(message.id),
                    label=label,
                    detail=" ".join(detail),
                )
            )
        summaries = await self._mail_intelligence.list_thread_summaries(
            limit=self._max_entities
        )
        for summary in summaries:
            entities.append(
                ConversationEntityRef(
                    kind=ConversationEntityKind.MAIL_THREAD,
                    id=str(summary.thread.id),
                    label=summary.subject_preview or "（无主题会话）",
                    detail=(
                        f"messages={summary.message_count} "
                        f"latest={_instant(summary.latest_at)}"
                    ),
                )
            )
        if self._mail_drafts is not None:
            for draft in await self._mail_drafts.list_drafts(limit=self._max_entities):
                entities.append(
                    ConversationEntityRef(
                        kind=ConversationEntityKind.MAIL_DRAFT,
                        id=str(draft.id),
                        label=f"{'、'.join(draft.to_addresses)}：{draft.subject}",
                        detail=f"version={draft.version} account={draft.account_id}",
                    )
                )
        return tuple(entities)


def _instant(value: datetime) -> str:
    """Render an instant as UTC ISO 8601 for the model to read."""
    return value.astimezone(UTC).isoformat()


def _recent_history(
    messages: list[ConversationMessage], *, max_messages: int, max_chars: int
) -> tuple[tuple[ConversationRecentMessage, ...], bool]:
    """The newest bounded slice of history, in reading order."""
    selected: list[ConversationRecentMessage] = []
    used = 0
    for message in reversed(messages):
        if len(selected) >= max_messages:
            break
        if used + len(message.text) > max_chars:
            break
        selected.append(
            ConversationRecentMessage(
                role=message.role,
                text=message.text,
                created_at=message.created_at,
            )
        )
        used += len(message.text)
    selected.reverse()
    return tuple(selected), len(selected) < len(messages)


def _order_tasks(tasks: list[Task], deadlines: dict[TaskId, Deadline]) -> list[Task]:
    """Deadline tasks first (earliest due), then the rest; deterministic tie-breaks."""

    def key(task: Task) -> tuple[object, ...]:
        deadline = deadlines.get(task.id)
        due_at = None if deadline is None else deadline.due_at
        return (
            due_at is None,
            due_at or task.created_at,
            _PRIORITY_RANK[task.priority],
            task.created_at,
            str(task.id),
        )

    return sorted(tasks, key=key)


def _task_detail(task: Task, deadline: Deadline | None) -> str:
    due_at = None if deadline is None else deadline.due_at
    estimate = task.estimated_minutes
    return (
        f"deadline={'-' if due_at is None else due_at.isoformat()} "
        f"priority={task.priority.value} "
        f"estimate={'-' if estimate is None else f'{estimate}m'}"
    )


def _proposal_label(proposal: PlanProposal) -> str:
    return (
        f"weekly plan proposal {proposal.window.starts_at.date().isoformat()} -> "
        f"{proposal.window.ends_at.date().isoformat()} ({proposal.window.timezone})"
    )


def _recurring_detail(rule: RecurringCalendarRule) -> str:
    """Everything a follow-up needs about one rule, and nothing derived from it."""
    return (
        f"id={str(rule.id)[:8]} "
        f"weekday={rule.weekday} "
        f"start={format_clock(rule.start_time)} "
        f"end={format_clock(rule.end_time)} "
        f"timezone={rule.timezone} "
        f"starts_on={rule.starts_on.isoformat()} "
        f"ends_on={'-' if rule.ends_on is None else rule.ends_on.isoformat()} "
        f"status={rule.status.value}"
    )


__all__ = ["CALENDAR_LOOKAHEAD_DAYS", "ConversationContextBuilder"]
