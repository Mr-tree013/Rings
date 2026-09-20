"""What a model turn is allowed to say: one closed plan of typed local operations (ADR-0033).

This file is the whole vocabulary. A turn is either a `DIRECT_REPLY`, a `CLARIFICATION`, or a list
of `OPERATIONS`, and every operation is one of the enum members below with a closed argument
object. There is no `tool_name`, `function_name`, `method`, `command`, `url` or free-form payload
anywhere in it, so "call something I was not offered" is not a thing a model can express — it is a
schema violation, which is a rejected turn rather than an adventurous one.

The vocabulary is deliberately the *daily local* surface: tasks, calendar, work sessions,
planning, the notification inbox and grounded knowledge. No `case.*`, `action.*`, `approval.*`,
`execution.*`, `mail.send`, `ehall.*`, `fact.*` or `playbook.*` operation exists in Phase 10A, and
neither does any `http.*`, `browser.*`, `shell.*` or `filesystem.*` one.

Arguments are validated here, deterministically, before anything is executed: a dataclass cannot
be constructed in an invalid state, so the runtime never has to re-check a value it just built.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from assistant.domain.errors import InvalidConversationPlan
from assistant.domain.task import TaskId, TaskPriority

MAX_OPERATIONS_PER_TURN = 5
"""One turn is a request, not a batch job. More than this becomes a question back to the user."""

MAX_OPERATION_TEXT_CHARS = 2000
"""Upper bound for any single free-text argument."""


class ConversationPlanMode(StrEnum):
    """The three shapes a model answer may take."""

    DIRECT_REPLY = "direct_reply"
    CLARIFICATION = "clarification"
    OPERATIONS = "operations"


class ConversationOperationType(StrEnum):
    """The frozen Phase 10A operation vocabulary (ADR-0033 §30)."""

    STATUS_GET = "status.get"

    TASK_LIST = "task.list"
    TASK_SHOW = "task.show"
    TASK_CREATE = "task.create"
    TASK_EDIT = "task.edit"
    TASK_SET_DEADLINE = "task.set_deadline"
    TASK_CLEAR_DEADLINE = "task.clear_deadline"
    TASK_COMPLETE = "task.complete"

    CALENDAR_LIST = "calendar.list"
    CALENDAR_CREATE = "calendar.create"

    WORK_RECORD = "work.record"

    PLAN_CURRENT = "plan.current"
    PLAN_PROPOSE_WEEK = "plan.propose_week"
    PLAN_APPLY_PROPOSAL = "plan.apply_proposal"

    NOTIFICATION_LIST = "notification.list"
    NOTIFICATION_READ = "notification.read"

    KNOWLEDGE_ASK = "knowledge.ask"

    MAIL_STATUS = "mail.status"
    MAIL_SYNC = "mail.sync"
    MAIL_LIST = "mail.list"
    MAIL_SHOW = "mail.show"
    MAIL_THREAD = "mail.thread"
    MAIL_REPLY_DRAFT = "mail.reply_draft"
    MAIL_PREPARE_REPLY_SEND = "mail.prepare_reply_send"
    MAIL_RECONCILE_SEND = "mail.reconcile_send"


READ_OPERATIONS = frozenset(
    {
        ConversationOperationType.STATUS_GET,
        ConversationOperationType.TASK_LIST,
        ConversationOperationType.TASK_SHOW,
        ConversationOperationType.CALENDAR_LIST,
        ConversationOperationType.PLAN_CURRENT,
        ConversationOperationType.NOTIFICATION_LIST,
        ConversationOperationType.KNOWLEDGE_ASK,
        ConversationOperationType.MAIL_STATUS,
        ConversationOperationType.MAIL_LIST,
        ConversationOperationType.MAIL_SHOW,
        ConversationOperationType.MAIL_THREAD,
    }
)
"""Operations that only read. They execute immediately (ADR-0033 §10)."""


def _text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InvalidConversationPlan(f"{field_name} must not be blank")
    if len(cleaned) > MAX_OPERATION_TEXT_CHARS:
        raise InvalidConversationPlan(
            f"{field_name} is at most {MAX_OPERATION_TEXT_CHARS} characters"
        )
    return cleaned


def _optional_text(value: str | None, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidConversationPlan(f"{field_name} must be timezone-aware")
    return value


def _task_id(value: UUID) -> TaskId:
    return value


# ---------------------------------------------------------------------------- read arguments


@dataclass(frozen=True, slots=True)
class StatusGetArguments:
    """`status.get` takes no arguments."""

    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.STATUS_GET, init=False
    )


@dataclass(frozen=True, slots=True)
class TaskListArguments:
    """Which tasks to list."""

    include_terminal: bool = False
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_LIST, init=False
    )


@dataclass(frozen=True, slots=True)
class TaskShowArguments:
    """One task, by an identity that was present in the supplied context."""

    task_id: TaskId
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_SHOW, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class CalendarListArguments:
    """Events and plan blocks over the next `days` days."""

    days: int = 7
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.CALENDAR_LIST, init=False
    )

    def __post_init__(self) -> None:
        if not 1 <= self.days <= 60:
            raise InvalidConversationPlan("calendar.list supports 1 to 60 days")


@dataclass(frozen=True, slots=True)
class PlanCurrentArguments:
    """The current pending weekly proposal, if there is one."""

    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.PLAN_CURRENT, init=False
    )


@dataclass(frozen=True, slots=True)
class NotificationListArguments:
    """The durable notification inbox."""

    unread_only: bool = True
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.NOTIFICATION_LIST, init=False
    )


@dataclass(frozen=True, slots=True)
class NotificationReadArguments:
    """Mark one notification read."""

    notification_id: str
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.NOTIFICATION_READ, init=False
    )

    def __post_init__(self) -> None:
        _text(self.notification_id, "notification_id")


@dataclass(frozen=True, slots=True)
class KnowledgeAskArguments:
    """A question answerable from indexed personal sources, with citations."""

    question: str
    root_id: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.KNOWLEDGE_ASK, init=False
    )

    def __post_init__(self) -> None:
        _text(self.question, "question")
        if self.root_id is not None:
            _text(self.root_id, "root_id")


# -------------------------------------------------------------------------- mail arguments


@dataclass(frozen=True, slots=True)
class MailStatusArguments:
    """The mailbox at a glance: stored messages, unread work, prepared sends."""

    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_STATUS, init=False
    )


@dataclass(frozen=True, slots=True)
class MailSyncArguments:
    """One incremental IMAP receive (read-only on the server, durable locally)."""

    account_id: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_SYNC, init=False
    )

    def __post_init__(self) -> None:
        if self.account_id is not None:
            _text(self.account_id, "account_id")


@dataclass(frozen=True, slots=True)
class MailListArguments:
    """List stored messages, newest first."""

    limit: int = 10
    requires_reply: bool = False
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_LIST, init=False
    )

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 25:
            raise InvalidConversationPlan("mail.list supports 1 to 25 messages")


@dataclass(frozen=True, slots=True)
class MailShowArguments:
    """One stored message, by an identity that was in the supplied context."""

    message_id: str
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_SHOW, init=False
    )

    def __post_init__(self) -> None:
        _text(self.message_id, "message_id")


@dataclass(frozen=True, slots=True)
class MailThreadArguments:
    """One stored thread and its messages."""

    thread_id: str
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_THREAD, init=False
    )

    def __post_init__(self) -> None:
        _text(self.thread_id, "thread_id")


@dataclass(frozen=True, slots=True)
class MailReplyDraftArguments:
    """Draft a reply with the existing reply-draft service.

    `body_text` is what the user asked Tree to say ("说我周五之前交"), already written out by the
    interpretation turn. When it is absent the existing service composes the body itself.
    """

    message_id: str
    body_text: str | None = None
    context_query: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_REPLY_DRAFT, init=False
    )

    def __post_init__(self) -> None:
        _text(self.message_id, "message_id")
        if self.body_text is not None:
            _text(self.body_text, "body_text")
        if self.context_query is not None:
            _text(self.context_query, "context_query")


@dataclass(frozen=True, slots=True)
class MailPrepareReplySendArguments:
    """Freeze one draft version into an immutable, reviewable `mail.send` action.

    This prepares; it never approves and never executes (ADR-0034 §10).

    Either `draft_id` or `message_id` identifies the draft: a turn that drafts and prepares in one
    go cannot know the draft's id yet, so it names the message and the runtime uses the newest
    draft for it.
    """

    draft_id: str | None = None
    message_id: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_PREPARE_REPLY_SEND, init=False
    )

    def __post_init__(self) -> None:
        if self.draft_id is None and self.message_id is None:
            raise InvalidConversationPlan(
                "mail.prepare_reply_send needs a draft_id or a message_id"
            )
        if self.draft_id is not None:
            _text(self.draft_id, "draft_id")
        if self.message_id is not None:
            _text(self.message_id, "message_id")


@dataclass(frozen=True, slots=True)
class MailReconcileSendArguments:
    """Ask the Sent mailbox whether one prepared message actually arrived."""

    action_id: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.MAIL_RECONCILE_SEND, init=False
    )

    def __post_init__(self) -> None:
        if self.action_id is not None:
            _text(self.action_id, "action_id")


# ---------------------------------------------------------------------------- write arguments


@dataclass(frozen=True, slots=True)
class TaskCreateArguments:
    """Create one task."""

    title: str
    description: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    estimated_minutes: int | None = None
    due_at: datetime | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_CREATE, init=False
    )

    def __post_init__(self) -> None:
        _text(self.title, "title")
        _optional_text(self.description, "description")
        if self.estimated_minutes is not None and not 1 <= self.estimated_minutes <= 60_000:
            raise InvalidConversationPlan("estimated_minutes must be between 1 and 60000")
        if self.due_at is not None:
            _aware(self.due_at, "due_at")


@dataclass(frozen=True, slots=True)
class TaskEditArguments:
    """Change fields of one existing task; unset fields keep their current value.

    `description` is deliberately absent: "the user said nothing about it" and "the user wants it
    empty" cannot be told apart in one nullable field, so the interpreter refuses rather than
    guesses (the same reasoning as ADR-0018 for `EditTask`).
    """

    task_id: TaskId
    title: str | None = None
    priority: TaskPriority | None = None
    estimated_minutes: int | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_EDIT, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)
        changes = (self.title, self.priority, self.estimated_minutes)
        if all(value is None for value in changes):
            raise InvalidConversationPlan("task.edit needs at least one field to change")
        if self.title is not None:
            _text(self.title, "title")
        if self.estimated_minutes is not None and not 1 <= self.estimated_minutes <= 60_000:
            raise InvalidConversationPlan("estimated_minutes must be between 1 and 60000")


@dataclass(frozen=True, slots=True)
class TaskSetDeadlineArguments:
    """Set or move one task's deadline."""

    task_id: TaskId
    due_at: datetime
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_SET_DEADLINE, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)
        _aware(self.due_at, "due_at")


@dataclass(frozen=True, slots=True)
class TaskClearDeadlineArguments:
    """Remove one task's deadline (and its reminder jobs)."""

    task_id: TaskId
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_CLEAR_DEADLINE, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class TaskCompleteArguments:
    """Complete one task, cancelling its unfinished plan blocks."""

    task_id: TaskId
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.TASK_COMPLETE, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)


@dataclass(frozen=True, slots=True)
class CalendarCreateArguments:
    """Record time that is already taken."""

    title: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.CALENDAR_CREATE, init=False
    )

    def __post_init__(self) -> None:
        _text(self.title, "title")
        _optional_text(self.description, "description")
        _aware(self.starts_at, "starts_at")
        _aware(self.ends_at, "ends_at")
        if self.ends_at <= self.starts_at:
            raise InvalidConversationPlan("an event must end after it starts")


@dataclass(frozen=True, slots=True)
class WorkRecordArguments:
    """Record time actually spent on an existing task."""

    task_id: TaskId
    started_at: datetime
    ended_at: datetime
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.WORK_RECORD, init=False
    )

    def __post_init__(self) -> None:
        _task_id(self.task_id)
        _aware(self.started_at, "started_at")
        _aware(self.ended_at, "ended_at")
        if self.ended_at <= self.started_at:
            raise InvalidConversationPlan("a work session must end after it starts")


@dataclass(frozen=True, slots=True)
class PlanProposeWeekArguments:
    """Ask the deterministic planner for a weekly proposal."""

    next_week: bool = False
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.PLAN_PROPOSE_WEEK, init=False
    )


@dataclass(frozen=True, slots=True)
class PlanApplyProposalArguments:
    """Apply a pending proposal. `proposal_id=None` means the latest pending one."""

    proposal_id: str | None = None
    operation_type: ConversationOperationType = field(
        default=ConversationOperationType.PLAN_APPLY_PROPOSAL, init=False
    )

    def __post_init__(self) -> None:
        if self.proposal_id is not None:
            _text(self.proposal_id, "proposal_id")


ConversationOperationArguments = (
    StatusGetArguments
    | TaskListArguments
    | TaskShowArguments
    | TaskCreateArguments
    | TaskEditArguments
    | TaskSetDeadlineArguments
    | TaskClearDeadlineArguments
    | TaskCompleteArguments
    | CalendarListArguments
    | CalendarCreateArguments
    | WorkRecordArguments
    | PlanCurrentArguments
    | PlanProposeWeekArguments
    | PlanApplyProposalArguments
    | NotificationListArguments
    | NotificationReadArguments
    | KnowledgeAskArguments
    | MailStatusArguments
    | MailSyncArguments
    | MailListArguments
    | MailShowArguments
    | MailThreadArguments
    | MailReplyDraftArguments
    | MailPrepareReplySendArguments
    | MailReconcileSendArguments
)
"""The closed union of argument objects. Adding a member is a vocabulary change."""


def arguments_payload(arguments: ConversationOperationArguments) -> dict[str, Any]:
    """The canonical JSON object stored for one operation's arguments."""
    raw: dict[str, Any] = {}
    for entry in fields(arguments):
        if entry.name == "operation_type":
            continue
        value = getattr(arguments, entry.name)
        if isinstance(value, datetime):
            raw[entry.name] = value.isoformat()
        elif isinstance(value, StrEnum):
            raw[entry.name] = value.value
        elif isinstance(value, UUID):
            raw[entry.name] = str(value)
        else:
            raw[entry.name] = value
    return raw


_ALLOWED_KEYS: dict[ConversationOperationType, frozenset[str]] = {
    ConversationOperationType.STATUS_GET: frozenset(),
    ConversationOperationType.TASK_LIST: frozenset({"include_terminal"}),
    ConversationOperationType.TASK_SHOW: frozenset({"task_id"}),
    ConversationOperationType.TASK_CREATE: frozenset(
        {"title", "description", "priority", "estimated_minutes", "due_at"}
    ),
    ConversationOperationType.TASK_EDIT: frozenset(
        {"task_id", "title", "priority", "estimated_minutes"}
    ),
    ConversationOperationType.TASK_SET_DEADLINE: frozenset({"task_id", "due_at"}),
    ConversationOperationType.TASK_CLEAR_DEADLINE: frozenset({"task_id"}),
    ConversationOperationType.TASK_COMPLETE: frozenset({"task_id"}),
    ConversationOperationType.CALENDAR_LIST: frozenset({"days"}),
    ConversationOperationType.CALENDAR_CREATE: frozenset(
        {"title", "starts_at", "ends_at", "description"}
    ),
    ConversationOperationType.WORK_RECORD: frozenset({"task_id", "started_at", "ended_at"}),
    ConversationOperationType.PLAN_CURRENT: frozenset(),
    ConversationOperationType.PLAN_PROPOSE_WEEK: frozenset({"next_week"}),
    ConversationOperationType.PLAN_APPLY_PROPOSAL: frozenset({"proposal_id"}),
    ConversationOperationType.NOTIFICATION_LIST: frozenset({"unread_only"}),
    ConversationOperationType.NOTIFICATION_READ: frozenset({"notification_id"}),
    ConversationOperationType.KNOWLEDGE_ASK: frozenset({"question", "root_id"}),
    ConversationOperationType.MAIL_STATUS: frozenset(),
    ConversationOperationType.MAIL_SYNC: frozenset({"account_id"}),
    ConversationOperationType.MAIL_LIST: frozenset({"limit", "requires_reply"}),
    ConversationOperationType.MAIL_SHOW: frozenset({"message_id"}),
    ConversationOperationType.MAIL_THREAD: frozenset({"thread_id"}),
    ConversationOperationType.MAIL_REPLY_DRAFT: frozenset(
        {"message_id", "body_text", "context_query"}
    ),
    ConversationOperationType.MAIL_PREPARE_REPLY_SEND: frozenset({"draft_id", "message_id"}),
    ConversationOperationType.MAIL_RECONCILE_SEND: frozenset({"action_id"}),
}
"""The exact argument keys each operation accepts. Anything else is rejected, not ignored."""


def _boolean(value: object, field_name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InvalidConversationPlan(f"{field_name} must be true or false")
    return value


def _integer(value: object, field_name: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidConversationPlan(f"{field_name} must be an integer")
    return value


def _strings(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidConversationPlan(f"{field_name} must be a string")
    return value


def _optional_strings(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _strings(value, field_name)


def _identifier(value: object, field_name: str) -> UUID:
    if not isinstance(value, str):
        raise InvalidConversationPlan(f"{field_name} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidConversationPlan(f"{field_name} is not a UUID: {value!r}") from exc


def _instant(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidConversationPlan(f"{field_name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidConversationPlan(f"{field_name}: not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidConversationPlan(f"{field_name} carries no timezone offset")
    return parsed


def _optional_instant(value: object, field_name: str) -> datetime | None:
    return None if value is None else _instant(value, field_name)


def _priority(value: object) -> TaskPriority:
    if value is None:
        return TaskPriority.NORMAL
    if not isinstance(value, str):
        raise InvalidConversationPlan("priority must be a string")
    try:
        return TaskPriority(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in TaskPriority)
        raise InvalidConversationPlan(f"priority must be one of: {allowed}") from exc


def _optional_priority(value: object) -> TaskPriority | None:
    return None if value is None else _priority(value)


def build_arguments(
    operation_type: str, payload: Mapping[str, Any] | None = None
) -> ConversationOperationArguments:
    """Build typed arguments from untrusted data: a model answer, or a stored row.

    Raises:
        InvalidConversationPlan: the operation type is not in the frozen vocabulary, the payload
            is not an object, it carries a key the operation does not accept, or a value has the
            wrong type.
    """
    try:
        kind = ConversationOperationType(operation_type)
    except ValueError as exc:
        raise InvalidConversationPlan(f"unsupported operation type: {operation_type!r}") from exc
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise InvalidConversationPlan("operation arguments must be an object")
    unknown = sorted(set(payload) - _ALLOWED_KEYS[kind])
    if unknown:
        raise InvalidConversationPlan(
            f"{kind.value} does not accept argument(s): {', '.join(unknown)}"
        )
    if kind is ConversationOperationType.STATUS_GET:
        return StatusGetArguments()
    if kind is ConversationOperationType.TASK_LIST:
        return TaskListArguments(
            include_terminal=_boolean(
                payload.get("include_terminal"), "include_terminal", default=False
            )
        )
    if kind is ConversationOperationType.TASK_SHOW:
        return TaskShowArguments(task_id=_identifier(payload.get("task_id"), "task_id"))
    if kind is ConversationOperationType.TASK_CREATE:
        return TaskCreateArguments(
            title=_strings(payload.get("title"), "title"),
            description=_optional_strings(payload.get("description"), "description"),
            priority=_priority(payload.get("priority")),
            estimated_minutes=_optional_estimated(payload.get("estimated_minutes")),
            due_at=_optional_instant(payload.get("due_at"), "due_at"),
        )
    if kind is ConversationOperationType.TASK_EDIT:
        return TaskEditArguments(
            task_id=_identifier(payload.get("task_id"), "task_id"),
            title=_optional_strings(payload.get("title"), "title"),
            priority=_optional_priority(payload.get("priority")),
            estimated_minutes=_optional_estimated(payload.get("estimated_minutes")),
        )
    if kind is ConversationOperationType.TASK_SET_DEADLINE:
        return TaskSetDeadlineArguments(
            task_id=_identifier(payload.get("task_id"), "task_id"),
            due_at=_instant(payload.get("due_at"), "due_at"),
        )
    if kind is ConversationOperationType.TASK_CLEAR_DEADLINE:
        return TaskClearDeadlineArguments(task_id=_identifier(payload.get("task_id"), "task_id"))
    if kind is ConversationOperationType.TASK_COMPLETE:
        return TaskCompleteArguments(task_id=_identifier(payload.get("task_id"), "task_id"))
    if kind is ConversationOperationType.CALENDAR_LIST:
        return CalendarListArguments(
            days=_integer(payload.get("days"), "days", default=7)
        )
    if kind is ConversationOperationType.CALENDAR_CREATE:
        return CalendarCreateArguments(
            title=_strings(payload.get("title"), "title"),
            starts_at=_instant(payload.get("starts_at"), "starts_at"),
            ends_at=_instant(payload.get("ends_at"), "ends_at"),
            description=_optional_strings(payload.get("description"), "description"),
        )
    if kind is ConversationOperationType.WORK_RECORD:
        return WorkRecordArguments(
            task_id=_identifier(payload.get("task_id"), "task_id"),
            started_at=_instant(payload.get("started_at"), "started_at"),
            ended_at=_instant(payload.get("ended_at"), "ended_at"),
        )
    if kind is ConversationOperationType.PLAN_CURRENT:
        return PlanCurrentArguments()
    if kind is ConversationOperationType.PLAN_PROPOSE_WEEK:
        return PlanProposeWeekArguments(
            next_week=_boolean(payload.get("next_week"), "next_week", default=False)
        )
    if kind is ConversationOperationType.PLAN_APPLY_PROPOSAL:
        return PlanApplyProposalArguments(
            proposal_id=_optional_strings(payload.get("proposal_id"), "proposal_id")
        )
    if kind is ConversationOperationType.NOTIFICATION_LIST:
        return NotificationListArguments(
            unread_only=_boolean(payload.get("unread_only"), "unread_only", default=True)
        )
    if kind is ConversationOperationType.NOTIFICATION_READ:
        return NotificationReadArguments(
            notification_id=_strings(payload.get("notification_id"), "notification_id")
        )
    if kind is ConversationOperationType.KNOWLEDGE_ASK:
        return KnowledgeAskArguments(
            question=_strings(payload.get("question"), "question"),
            root_id=_optional_strings(payload.get("root_id"), "root_id"),
        )
    if kind is ConversationOperationType.MAIL_STATUS:
        return MailStatusArguments()
    if kind is ConversationOperationType.MAIL_SYNC:
        return MailSyncArguments(
            account_id=_optional_strings(payload.get("account_id"), "account_id")
        )
    if kind is ConversationOperationType.MAIL_LIST:
        return MailListArguments(
            limit=_integer(payload.get("limit"), "limit", default=10),
            requires_reply=_boolean(
                payload.get("requires_reply"), "requires_reply", default=False
            ),
        )
    if kind is ConversationOperationType.MAIL_SHOW:
        return MailShowArguments(message_id=_strings(payload.get("message_id"), "message_id"))
    if kind is ConversationOperationType.MAIL_THREAD:
        return MailThreadArguments(thread_id=_strings(payload.get("thread_id"), "thread_id"))
    if kind is ConversationOperationType.MAIL_REPLY_DRAFT:
        return MailReplyDraftArguments(
            message_id=_strings(payload.get("message_id"), "message_id"),
            body_text=_optional_strings(payload.get("body_text"), "body_text"),
            context_query=_optional_strings(payload.get("context_query"), "context_query"),
        )
    if kind is ConversationOperationType.MAIL_PREPARE_REPLY_SEND:
        return MailPrepareReplySendArguments(
            draft_id=_optional_strings(payload.get("draft_id"), "draft_id"),
            message_id=_optional_strings(payload.get("message_id"), "message_id"),
        )
    if kind is ConversationOperationType.MAIL_RECONCILE_SEND:
        return MailReconcileSendArguments(
            action_id=_optional_strings(payload.get("action_id"), "action_id")
        )
    raise AssertionError(f"unhandled operation type: {kind}")  # pragma: no cover


def _optional_estimated(value: object) -> int | None:
    """An estimate is optional, and `null`/absent both mean "the user did not say"."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidConversationPlan("estimated_minutes must be an integer or null")
    return value


def operation_fingerprint(
    operation_type: ConversationOperationType, arguments: ConversationOperationArguments
) -> str:
    """A canonical SHA-256 over the operation type and its arguments.

    A confirmation is bound to this value, so a plan that changes by one character is a different
    operation that the user has not confirmed.
    """
    document = {
        "type": operation_type.value,
        "arguments": arguments_payload(arguments),
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def requires_planning_timezone(
    operation_type: ConversationOperationType, arguments: ConversationOperationArguments
) -> bool:
    """Whether this operation's meaning depends on the user's planning timezone.

    A relative expression ("明天下午三点") is only defensible against a configured planning
    timezone; if there is none, the runtime asks instead of guessing the host's (ADR-0033 §20-21).
    An operation that carries no time at all — listing tasks, reading a notification, asking a
    knowledge question — is unaffected.
    """
    if operation_type in (
        ConversationOperationType.TASK_SET_DEADLINE,
        ConversationOperationType.CALENDAR_CREATE,
        ConversationOperationType.WORK_RECORD,
        ConversationOperationType.CALENDAR_LIST,
        ConversationOperationType.PLAN_CURRENT,
        ConversationOperationType.PLAN_PROPOSE_WEEK,
        ConversationOperationType.PLAN_APPLY_PROPOSAL,
    ):
        return True
    if operation_type is ConversationOperationType.TASK_CREATE:
        return isinstance(arguments, TaskCreateArguments) and arguments.due_at is not None
    return False


@dataclass(frozen=True, slots=True)
class PlannedOperation:
    """One operation inside a model-produced plan, before it is persisted."""

    operation_type: ConversationOperationType
    arguments: ConversationOperationArguments
    note: str | None = None

    def __post_init__(self) -> None:
        if self.arguments.operation_type is not self.operation_type:
            raise InvalidConversationPlan(
                f"arguments for {self.arguments.operation_type.value} do not match "
                f"{self.operation_type.value}"
            )
        if self.note is not None:
            _optional_text(self.note, "note")

    @property
    def fingerprint(self) -> str:
        """The canonical fingerprint a confirmation is bound to."""
        return operation_fingerprint(self.operation_type, self.arguments)


@dataclass(frozen=True, slots=True)
class ConversationPlan:
    """One validated model answer."""

    mode: ConversationPlanMode
    reply: str | None = None
    clarification: str | None = None
    operations: tuple[PlannedOperation, ...] = ()

    def __post_init__(self) -> None:
        if len(self.operations) > MAX_OPERATIONS_PER_TURN:
            raise InvalidConversationPlan(
                f"a turn proposes at most {MAX_OPERATIONS_PER_TURN} operations"
            )
        if self.mode is ConversationPlanMode.OPERATIONS:
            if not self.operations:
                raise InvalidConversationPlan("an operations plan carries at least one operation")
            if self.clarification is not None:
                raise InvalidConversationPlan("an operations plan is not a clarification")
        elif self.operations:
            raise InvalidConversationPlan("only an operations plan carries operations")
        if self.mode is ConversationPlanMode.CLARIFICATION and not (
            self.clarification or self.reply
        ):
            raise InvalidConversationPlan("a clarification plan asks something")
        if self.mode is ConversationPlanMode.DIRECT_REPLY and not self.reply:
            raise InvalidConversationPlan("a direct reply says something")


__all__ = [
    "MAX_OPERATIONS_PER_TURN",
    "MAX_OPERATION_TEXT_CHARS",
    "READ_OPERATIONS",
    "CalendarCreateArguments",
    "CalendarListArguments",
    "ConversationOperationArguments",
    "ConversationOperationType",
    "ConversationPlan",
    "ConversationPlanMode",
    "KnowledgeAskArguments",
    "MailListArguments",
    "MailPrepareReplySendArguments",
    "MailReconcileSendArguments",
    "MailReplyDraftArguments",
    "MailShowArguments",
    "MailStatusArguments",
    "MailSyncArguments",
    "MailThreadArguments",
    "NotificationListArguments",
    "NotificationReadArguments",
    "PlanApplyProposalArguments",
    "PlanCurrentArguments",
    "PlanProposeWeekArguments",
    "PlannedOperation",
    "StatusGetArguments",
    "TaskClearDeadlineArguments",
    "TaskCompleteArguments",
    "TaskCreateArguments",
    "TaskEditArguments",
    "TaskListArguments",
    "TaskSetDeadlineArguments",
    "TaskShowArguments",
    "WorkRecordArguments",
    "arguments_payload",
    "build_arguments",
    "operation_fingerprint",
    "requires_planning_timezone",
]
