"""One deterministic summary of the user's local day (ADR-0039).

```text
Clock + planning.timezone ──► [00:00, 24:00) of the user's own day
        │
        ├── schedule   today's events, derived weekly occurrences, applied plan blocks
        ├── tasks      overdue, due today, due soon, high priority
        ├── attention  unread reminders, mail that asks for a reply
        ├── waiting    prepared-but-unsent mail, pending proposals, pending confirmations, facts
        └── checks     unresolved external outcomes (never called failures)
```

No model is involved and nothing is written: this module reads bounded collections from existing
services and returns them, so the same state always produces the same brief. It never touches SQL,
never opens a socket and never approves, sends or executes anything.

Two decisions are worth naming. "Today" is the user's civil day in `planning.timezone`; without one
there is no answer, and the runtime asks instead of using the machine's timezone. And an outcome
that is *unknown* is reported as unknown: a send whose delivery nobody can prove is not a failure,
and saying it failed would be a lie the user might act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistant.application.conversational_facts import ConversationalFactService
from assistant.application.mail_send_status import MailDeliveryState, MailSendStatusService
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.domain.conversation import ConversationOperationStatus
from assistant.domain.conversation_plan import ConversationOperationType
from assistant.domain.errors import PlanningNotConfigured
from assistant.domain.planning import PlanProposalStatus
from assistant.domain.task import Task, TaskId, TaskPriority, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository
from assistant.ports.planning_repository import PlanningRepository
from assistant.ports.scheduler_repository import SchedulerRepository

CALENDAR_LIMIT = 10
"""How many schedule entries one brief may carry."""

TASK_LIMIT = 10
"""How many tasks one brief may carry."""

MAIL_ATTENTION_LIMIT = 5
"""How many mail items one brief may carry."""

NOTIFICATION_LIMIT = 5
"""How many reminder entries one brief may carry."""

WAITING_LIMIT = 5
"""How many waiting items one brief may carry."""

CHECK_LIMIT = 5
"""How many unresolved external outcomes one brief may carry."""

READ_MAIL_SCAN_LIMIT = 25
"""How many stored messages are scanned to find the ones that ask for a reply."""

DUE_SOON_DAYS = 3
"""How far ahead "due soon" reaches."""


@dataclass(frozen=True, slots=True)
class BriefEntry:
    """One line of the brief, before it is rendered."""

    kind: str
    label: str
    detail: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TodayBrief:
    """Everything one brief contains, already bounded and already ordered."""

    local_date: date
    timezone: str
    schedule: tuple[BriefEntry, ...] = ()
    tasks: tuple[BriefEntry, ...] = ()
    attention: tuple[BriefEntry, ...] = ()
    waiting: tuple[BriefEntry, ...] = ()
    checks: tuple[BriefEntry, ...] = ()
    overflow: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether the day has nothing at all to report."""
        return not (
            self.schedule or self.tasks or self.attention or self.waiting or self.checks
        )


class TodayBriefService:
    """A read-only, deterministic brief for the user's own today."""

    def __init__(
        self,
        *,
        commitments: CommitmentRepository,
        planning: PlanningRepository,
        scheduler: SchedulerRepository,
        conversations: ConversationRepository,
        mail: MailRepository | None = None,
        mail_intelligence: MailIntelligenceRepository | None = None,
        sends: MailSendStatusService | None = None,
        recurring: RecurringCalendarService | None = None,
        facts: ConversationalFactService | None = None,
        clock: Clock,
        planning_timezone: str | None,
    ) -> None:
        self._commitments = commitments
        self._planning = planning
        self._scheduler = scheduler
        self._conversations = conversations
        self._mail = mail
        self._mail_intelligence = mail_intelligence
        self._sends = sends
        self._recurring = recurring
        self._facts = facts
        self._clock = clock
        self._timezone = planning_timezone

    def require_timezone(self) -> str:
        """The planning timezone that defines "today".

        Raises:
            PlanningNotConfigured: this host has not chosen one, so "today" is unanswerable.
        """
        if self._timezone is None:
            raise PlanningNotConfigured(
                "no [planning].timezone in the host config, so I cannot tell which day is today"
            )
        return self._timezone

    def today(self) -> date:
        """The user's local date right now, in the planning timezone."""
        zone = ZoneInfo(self.require_timezone())
        return self._clock.now().astimezone(zone).date()

    def window(self) -> tuple[datetime, datetime]:
        """`[midnight, midnight)` of the user's local day, as instants."""
        zone = ZoneInfo(self.require_timezone())
        local = datetime.combine(self.today(), time(0, 0), tzinfo=zone)
        return local, local + timedelta(days=1)

    async def build(self) -> TodayBrief:
        """Read every bounded source once and return the brief. It mutates nothing."""
        timezone = self.require_timezone()
        start, end = self.window()
        today = self.today()
        now = self._clock.now()
        overflow: dict[str, int] = {}
        schedule = await self._schedule(start, end, overflow)
        tasks = await self._tasks(today, now, overflow)
        attention, waiting, checks = await self._attention(overflow)
        return TodayBrief(
            local_date=today,
            timezone=timezone,
            schedule=schedule,
            tasks=tasks,
            attention=attention,
            waiting=waiting,
            checks=checks,
            overflow=overflow,
        )

    # ------------------------------------------------------------------ schedule

    async def _schedule(
        self, start: datetime, end: datetime, overflow: dict[str, int]
    ) -> tuple[BriefEntry, ...]:
        """Today's fixed time: events, derived weekly occurrences and applied plan blocks."""
        zone = ZoneInfo(self.require_timezone())
        entries: list[BriefEntry] = []
        for event in await self._commitments.list_calendar_events(
            query_start=start, query_end=end
        ):
            entries.append(
                BriefEntry(
                    kind="calendar_event",
                    label=event.title,
                    starts_at=event.starts_at.astimezone(zone),
                    ends_at=event.ends_at.astimezone(zone),
                )
            )
        if self._recurring is not None:
            for occurrence in await self._recurring.expand_range(
                window_start=start, window_end=end
            ):
                entries.append(
                    BriefEntry(
                        kind="recurring",
                        label=occurrence.title,
                        starts_at=occurrence.starts_at.astimezone(zone),
                        ends_at=occurrence.ends_at.astimezone(zone),
                    )
                )
        titles = await self._task_titles()
        for block in await self._commitments.list_plan_blocks_in_range(
            query_start=start, query_end=end
        ):
            entries.append(
                BriefEntry(
                    kind="plan_block",
                    label=titles.get(block.task_id, str(block.task_id)[:8]),
                    starts_at=block.starts_at.astimezone(zone),
                    ends_at=block.ends_at.astimezone(zone),
                )
            )
        entries.sort(key=lambda entry: (entry.starts_at or start, entry.label))
        return _bounded(tuple(entries), CALENDAR_LIMIT, overflow, "schedule")

    async def _task_titles(self) -> dict[TaskId, str]:
        """Every task title, so a plan block can name the work instead of an id."""
        return {
            task.id: task.title
            for task in await self._commitments.list_tasks(statuses=None)
        }

    # --------------------------------------------------------------------- tasks

    async def _tasks(
        self, today: date, now: datetime, overflow: dict[str, int]
    ) -> tuple[BriefEntry, ...]:
        """Overdue, due today, due soon, and high-priority work — in that order."""
        zone = ZoneInfo(self.require_timezone())
        open_tasks = await self._commitments.list_tasks(statuses=(TaskStatus.OPEN,))
        if not open_tasks:
            return ()
        deadlines = await self._commitments.list_deadlines(
            [task.id for task in open_tasks]
        )
        buckets: dict[str, list[BriefEntry]] = {
            "overdue": [],
            "today": [],
            "soon": [],
            "priority": [],
        }
        for task in open_tasks:
            deadline = deadlines.get(task.id)
            entry = BriefEntry(
                kind="task",
                label=task.title,
                detail=_task_detail(task, deadline, today, now, zone),
                at=None if deadline is None else deadline.due_at.astimezone(zone),
            )
            if deadline is None:
                if task.priority is TaskPriority.HIGH:
                    buckets["priority"].append(entry)
                continue
            local_due = deadline.due_at.astimezone(zone)
            if local_due < now:
                buckets["overdue"].append(entry)
            elif local_due.date() == today:
                buckets["today"].append(entry)
            elif local_due.date() <= today + timedelta(days=DUE_SOON_DAYS):
                buckets["soon"].append(entry)
            elif task.priority is TaskPriority.HIGH:
                buckets["priority"].append(entry)
        ordered = tuple(
            entry
            for name in ("overdue", "today", "soon", "priority")
            for entry in buckets[name]
        )
        return _bounded(ordered, TASK_LIMIT, overflow, "tasks")

    # ----------------------------------------------------------------- attention

    async def _attention(
        self, overflow: dict[str, int]
    ) -> tuple[tuple[BriefEntry, ...], tuple[BriefEntry, ...], tuple[BriefEntry, ...]]:
        """What needs the user, what is waiting for them, and what nobody can prove yet."""
        attention: list[BriefEntry] = []
        mail_items, mail_total = await self._mail_requiring_reply()
        attention.extend(mail_items)
        notifications, unread_total = await self._notifications()
        attention.extend(notifications)
        if mail_total > len(mail_items):
            overflow["mail"] = mail_total - len(mail_items)
        if unread_total > len(notifications):
            overflow["notifications"] = unread_total - len(notifications)
        waiting = await self._waiting(overflow)
        checks = await self._checks(overflow)
        bounded = _bounded(
            tuple(attention),
            MAIL_ATTENTION_LIMIT + NOTIFICATION_LIMIT,
            overflow,
            "attention",
        )
        return (bounded, waiting, checks)

    async def _mail_requiring_reply(self) -> tuple[list[BriefEntry], int]:
        """Stored messages whose analysis asks for a reply — metadata only, never a body."""
        if self._mail is None or self._mail_intelligence is None:
            return [], 0
        required: list[BriefEntry] = []
        total = 0
        for message in await self._mail.list_messages(limit=READ_MAIL_SCAN_LIMIT):
            analysis = await self._mail_intelligence.get_analysis(message.id)
            if analysis is None or not analysis.requires_reply:
                continue
            total += 1
            required.append(
                BriefEntry(
                    kind="mail_requires_reply",
                    label=message.subject or "（无主题）",
                    detail=message.from_address or "（未知发件人）",
                    at=(message.sent_at or message.first_seen_at),
                )
            )
        bounded = _bounded(tuple(required), MAIL_ATTENTION_LIMIT, {}, "mail")
        return list(bounded), total

    async def _notifications(self) -> tuple[list[BriefEntry], int]:
        """Unread reminders, as bounded metadata."""
        notifications = await self._scheduler.list_notifications(
            unread_only=True, limit=NOTIFICATION_LIMIT + 1
        )
        entries = [
            BriefEntry(
                kind="notification",
                label=notification.title,
                detail=notification.kind.value,
                at=notification.created_at,
            )
            for notification in notifications[:NOTIFICATION_LIMIT]
        ]
        return entries, len(notifications)

    async def _waiting(self, overflow: dict[str, int]) -> tuple[BriefEntry, ...]:
        """Things that are prepared and waiting for the user, never things that ran."""
        entries: list[BriefEntry] = []
        if self._sends is not None:
            prepared = 0
            for status in await self._sends.list_statuses(limit=WAITING_LIMIT + 1):
                if status.state in (
                    MailDeliveryState.DRAFT,
                    MailDeliveryState.APPROVED,
                ):
                    prepared += 1
            if prepared:
                entries.append(
                    BriefEntry(
                        kind="mail_awaiting_confirmation",
                        label=f"{prepared} 封邮件等待你的发送确认",
                    )
                )
        summaries = await self._planning.list_proposal_summaries(limit=WAITING_LIMIT + 1)
        pending_proposals = [
            summary
            for summary in summaries
            if summary.proposal.status is PlanProposalStatus.PENDING
        ]
        if pending_proposals:
            entries.append(
                BriefEntry(kind="plan_proposal", label="有一份周计划提案等待应用")
            )
        if self._facts is not None:
            pending_facts = await self._facts.pending(limit=WAITING_LIMIT + 1)
            if pending_facts:
                entries.append(
                    BriefEntry(
                        kind="fact_review",
                        label=f"{len(pending_facts)} 条长期信息等待确认",
                    )
                )
        for operation in await self._conversations.list_operations_by_status(
            ConversationOperationStatus.WAITING_CONFIRMATION
        ):
            label = _WAITING_OPERATION_LABELS.get(operation.operation_type)
            if label is not None:
                entries.append(BriefEntry(kind="local_confirmation", label=label))
        deduplicated = _deduplicate(entries)
        return _bounded(tuple(deduplicated), WAITING_LIMIT, overflow, "waiting")

    async def _checks(self, overflow: dict[str, int]) -> tuple[BriefEntry, ...]:
        """Unresolved external outcomes. Unknown is reported as unknown, never as failed."""
        if self._sends is None:
            return ()
        unknown = 0
        for status in await self._sends.list_statuses(limit=CHECK_LIMIT + 1):
            if status.state is MailDeliveryState.SENDING_UNKNOWN:
                unknown += 1
        if not unknown:
            return ()
        entry = BriefEntry(
            kind="send_unknown",
            label=(
                f"{unknown} 个邮件发送结果仍不确定；我不会自动重试。"
            ),
        )
        return _bounded((entry,), CHECK_LIMIT, overflow, "checks")


_WAITING_OPERATION_LABELS: dict[ConversationOperationType, str] = {
    ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY: "有固定安排等待你确认",
    ConversationOperationType.MAIL_COMPOSE_NEW: "有邮件草稿等待你确认发送",
}
"""Waiting local operations worth naming, and how to name them.

`plan.apply_proposal` is deliberately absent: the proposal itself is already reported above, and
counting both would tell the user two different things about one waiting decision.
"""


def _bounded(
    entries: tuple[BriefEntry, ...],
    limit: int,
    overflow: dict[str, int],
    section: str,
) -> tuple[BriefEntry, ...]:
    """Keep `limit` entries and record how many were dropped."""
    if len(entries) <= limit:
        return entries
    overflow[section] = len(entries) - limit
    return entries[:limit]


def _deduplicate(entries: list[BriefEntry]) -> list[BriefEntry]:
    """One line per distinct waiting item, in the order they were found."""
    seen: set[tuple[str, str]] = set()
    unique: list[BriefEntry] = []
    for entry in entries:
        key = (entry.kind, entry.label)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _task_detail(
    task: Task,
    deadline: object,
    today: date,
    now: datetime,
    zone: ZoneInfo,
) -> str:
    """One short, human phrase about why this task is in the brief."""
    due_at = getattr(deadline, "due_at", None)
    if due_at is None:
        return "高优先级"
    local_due = due_at.astimezone(zone)
    if local_due < now:
        return f"已超过截止时间 {local_due.strftime('%m-%d %H:%M')}"
    if local_due.date() == today:
        return f"今天 {local_due.strftime('%H:%M')} 截止"
    return f"{local_due.strftime('%m-%d')} 截止"


__all__ = [
    "CALENDAR_LIMIT",
    "DUE_SOON_DAYS",
    "MAIL_ATTENTION_LIMIT",
    "NOTIFICATION_LIMIT",
    "TASK_LIMIT",
    "WAITING_LIMIT",
    "BriefEntry",
    "TodayBrief",
    "TodayBriefService",
]
