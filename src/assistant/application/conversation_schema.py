"""The one JSON Schema a conversation turn must satisfy (ADR-0033 §4-6).

The schema is the boundary, and it is deliberately narrow in the same way ADR-0018's is:

- the top level is closed (`additionalProperties: false`), and it requires all four fields, so
  "an operations turn that also asks a question" is not a representable answer;
- `operations` is capped at five items;
- every operation is its own closed object with `type` as a `const` and its own closed `arguments`
  object, so there is no free-form argument bag and no field named `tool`, `tool_name`,
  `function`, `method`, `command`, `url`, `shell` or `filesystem` anywhere;
- an operation outside the Phase 10A vocabulary is a schema violation, not a request the runtime
  has to remember to refuse.

`format: date-time` is carried for the provider's benefit; every timestamp is parsed and checked
again locally, because schema validation never replaces semantic validation.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.conversation_plan import MAX_OPERATIONS_PER_TURN
from assistant.domain.model import JsonSchemaOutput

CONVERSATION_SCHEMA_NAME = "tree_conversation_v1"

_MAX_REPLY_CHARS = 4000
_MAX_CLARIFICATION_CHARS = 1000
_MAX_NOTE_CHARS = 300
_MAX_TITLE_CHARS = 500
_MAX_TEXT_CHARS = 2000

_DATE_TIME: dict[str, Any] = {"type": "string", "format": "date-time"}
_NULLABLE_DATE_TIME: dict[str, Any] = {"type": ["string", "null"], "format": "date-time"}
_CIVIL_DATE = {"type": "string", "format": "date"}
_NULLABLE_CIVIL_DATE = {"type": ["string", "null"], "format": "date"}
_CLOCK_TIME = {"type": "string", "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$"}
_NULLABLE_CLOCK_TIME = {"type": ["string", "null"], "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$"}
_WEEKDAY = {"type": "integer", "minimum": 1, "maximum": 7}
_NULLABLE_WEEKDAY = {"type": ["integer", "null"], "minimum": 1, "maximum": 7}
_TIMEZONE_NAME = {"type": ["string", "null"], "maxLength": 64}
_NULLABLE_TEXT = {"type": ["string", "null"], "maxLength": _MAX_TEXT_CHARS}
_RECIPIENT_KIND = {"enum": ["explicit_email", "contact", "self", None]}
_NULLABLE_SHORT_TEXT = {"type": ["string", "null"], "maxLength": 200}
_TASK_ID = {"type": "string", "format": "uuid"}
_PRIORITY = {"enum": ["low", "normal", "high", None]}
_ESTIMATE = {"type": ["integer", "null"], "minimum": 1, "maximum": 60000}


def _operation(
    operation_type: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One closed operation object: `type`, closed `arguments`, optional `note`."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {"const": operation_type, "description": description},
            "arguments": {
                "type": "object",
                "additionalProperties": False,
                "properties": properties or {},
                "required": list(required),
            },
            "note": {"type": ["string", "null"], "maxLength": _MAX_NOTE_CHARS},
        },
        "required": ["type", "arguments", "note"],
    }


OPERATION_SCHEMAS: tuple[dict[str, Any], ...] = (
    _operation(
        "status.get",
        "Summarise the local day: open tasks, next deadline, unread reminders.",
    ),
    _operation(
        "task.list",
        "List the user's tasks.",
        {"include_terminal": {"type": "boolean"}},
        ("include_terminal",),
    ),
    _operation("task.show", "Show one task.", {"task_id": _TASK_ID}, ("task_id",)),
    _operation(
        "task.create",
        "Create one task. Omit or null any field the user did not state.",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": _MAX_TITLE_CHARS},
            "description": _NULLABLE_TEXT,
            "priority": _PRIORITY,
            "estimated_minutes": _ESTIMATE,
            "due_at": _NULLABLE_DATE_TIME,
        },
        ("title", "description", "priority", "estimated_minutes", "due_at"),
    ),
    _operation(
        "task.edit",
        "Change a task's title, priority or estimate. Null means 'leave unchanged'.",
        {
            "task_id": _TASK_ID,
            "title": {"type": ["string", "null"], "maxLength": _MAX_TITLE_CHARS},
            "priority": _PRIORITY,
            "estimated_minutes": _ESTIMATE,
        },
        ("task_id", "title", "priority", "estimated_minutes"),
    ),
    _operation(
        "task.set_deadline",
        "Set or move one task's deadline.",
        {"task_id": _TASK_ID, "due_at": _DATE_TIME},
        ("task_id", "due_at"),
    ),
    _operation(
        "task.clear_deadline",
        "Remove one task's deadline and its reminder jobs.",
        {"task_id": _TASK_ID},
        ("task_id",),
    ),
    _operation(
        "task.complete",
        "Mark one task complete.",
        {"task_id": _TASK_ID},
        ("task_id",),
    ),
    _operation(
        "calendar.list",
        "List calendar events and plan blocks over the next days.",
        {"days": {"type": "integer", "minimum": 1, "maximum": 60}},
        ("days",),
    ),
    _operation(
        "calendar.create",
        "Record time that is already taken.",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": _MAX_TITLE_CHARS},
            "starts_at": _DATE_TIME,
            "ends_at": _DATE_TIME,
            "description": _NULLABLE_TEXT,
        },
        ("title", "starts_at", "ends_at", "description"),
    ),
    _operation(
        "calendar.recurring.list",
        "List the user's standing weekly commitments (classes and other fixed weekly time). "
        "Use this for any question about fixed arrangements, recurring schedules or 固定安排.",
        {"include_retired": {"type": "boolean"}},
        ("include_retired",),
    ),
    _operation(
        "calendar.recurring.create_weekly",
        "Record ONE standing weekly commitment: the same weekday, the same local start and end "
        "time, every week until it ends. One rule is one weekday: for '周一和周三' propose two "
        "operations. Set timezone only when the user named one; null means the runtime's planning "
        "timezone, which the runtime applies itself. starts_on defaults to today in that "
        "timezone; set ends_on only when the user named a last day. There is no recurrence string "
        "argument, and every-N-weeks, odd/even weeks, monthly, yearly and holiday exceptions are "
        "not expressible here.",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": _MAX_TITLE_CHARS},
            "weekday": _WEEKDAY,
            "start_local_time": _CLOCK_TIME,
            "end_local_time": _CLOCK_TIME,
            "timezone": _TIMEZONE_NAME,
            "starts_on": _NULLABLE_CIVIL_DATE,
            "ends_on": _NULLABLE_CIVIL_DATE,
        },
        (
            "title",
            "weekday",
            "start_local_time",
            "end_local_time",
            "timezone",
            "starts_on",
            "ends_on",
        ),
    ),
    _operation(
        "calendar.recurring.edit",
        "Change one existing weekly commitment, by a rule_id from recent_entities. Null means "
        "'leave unchanged'. Use this for '把刚才那门课改成九点到十一点'.",
        {
            "rule_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "title": {"type": ["string", "null"], "maxLength": _MAX_TITLE_CHARS},
            "weekday": _NULLABLE_WEEKDAY,
            "start_local_time": _NULLABLE_CLOCK_TIME,
            "end_local_time": _NULLABLE_CLOCK_TIME,
        },
        ("rule_id", "title", "weekday", "start_local_time", "end_local_time"),
    ),
    _operation(
        "calendar.recurring.retire",
        "End one standing weekly commitment for the future, by a rule_id from recent_entities. "
        "Use this for '以后周一没有这门课了'. It never deletes anything that already happened. "
        "This is the only way to stop a weekly commitment; there is no delete.",
        {"rule_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        ("rule_id",),
    ),
    _operation(
        "work.record",
        "Record time actually spent on an existing task.",
        {"task_id": _TASK_ID, "started_at": _DATE_TIME, "ended_at": _DATE_TIME},
        ("task_id", "started_at", "ended_at"),
    ),
    _operation("plan.current", "Show the current pending weekly plan proposal."),
    _operation(
        "plan.propose_week",
        "Ask the deterministic planner for a weekly proposal. This never applies it.",
        {"next_week": {"type": "boolean"}},
        ("next_week",),
    ),
    _operation(
        "plan.apply_proposal",
        "Apply a pending weekly proposal. The user must confirm this before it happens.",
        {"proposal_id": {"type": ["string", "null"]}},
        ("proposal_id",),
    ),
    _operation(
        "notification.list",
        "List the durable reminder inbox.",
        {"unread_only": {"type": "boolean"}},
        ("unread_only",),
    ),
    _operation(
        "notification.read",
        "Mark one notification read.",
        {"notification_id": {"type": "string", "minLength": 1}},
        ("notification_id",),
    ),
    _operation(
        "knowledge.ask",
        "Answer a question from the user's indexed personal sources, with citations.",
        {
            "question": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_CHARS},
            "root_id": {"type": ["string", "null"], "maxLength": 200},
        },
        ("question", "root_id"),
    ),
    _operation(
        "mail.status",
        "Summarise the mailbox: stored messages, what is waiting for a reply, prepared sends.",
    ),
    _operation(
        "mail.sync",
        "Receive new mail once (IMAP read-only, stored locally). Omit account_id "
        "for every account.",
        {"account_id": {"type": ["string", "null"], "maxLength": 200}},
        ("account_id",),
    ),
    _operation(
        "mail.list",
        "List recent stored messages. Use this before referring to a message.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            "requires_reply": {"type": "boolean"},
        },
        ("limit", "requires_reply"),
    ),
    _operation(
        "mail.show",
        "Show one stored message, by an id from recent_entities.",
        {"message_id": {"type": "string", "minLength": 1, "maxLength": 200}},
        ("message_id",),
    ),
    _operation(
        "mail.thread",
        "Show one stored thread and its messages, by an id from recent_entities.",
        {"thread_id": {"type": "string", "minLength": 1, "maxLength": 200}},
        ("thread_id",),
    ),
    _operation(
        "mail.reply_draft",
        "Draft a reply to one stored message with the existing reply-draft service. Put what the "
        "user asked to say in `body_text`; leave it null to let the drafting service compose it.",
        {
            "message_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "body_text": {"type": ["string", "null"], "maxLength": _MAX_TEXT_CHARS},
            "context_query": {"type": ["string", "null"], "maxLength": _MAX_TEXT_CHARS},
        },
        ("message_id", "body_text", "context_query"),
    ),
    _operation(
        "mail.prepare_reply_send",
        "Freeze one reply draft into an immutable mail.send action and show the user the exact "
        "preview. This prepares only: it never approves and never sends, and the user must confirm "
        "afterwards with an explicit send phrase. Use draft_id when a draft id is in "
        "recent_entities, otherwise the message_id you drafted the reply to.",
        {
            "draft_id": {"type": ["string", "null"], "maxLength": 200},
            "message_id": {"type": ["string", "null"], "maxLength": 200},
        },
        ("draft_id", "message_id"),
    ),
    _operation(
        "mail.reconcile_send",
        "Check the Sent mailbox for one prepared message whose delivery is unknown. Omit "
        "action_id for the most recent prepared send.",
        {"action_id": {"type": ["string", "null"], "maxLength": 200}},
        ("action_id",),
    ),
    _operation(
        "mail.accounts",
        "List the mail accounts this host is configured to use. Configured accounts are not the "
        "same thing as stored messages: use this whenever the user asks which mailbox or address "
        "Tree can read.",
    ),
    _operation(
        "mail.compose_new",
        "Write a NEW letter (not a reply) and prepare it for the user's review. Write the subject "
        "and the body yourself. Choose exactly one recipient source: `explicit_email` with an "
        "address the user actually typed in this message, `contact` with a name the user wrote, or "
        "`self` for 我自己. Never invent an address: an explicit address that is not in the user's "
        "own message is refused by the runtime. Leave `sender_account` null unless the user named "
        "one. Leave `draft_id` null for a new letter; pass the id of the draft you are revising "
        "(and it is then fine to leave `recipient_kind` null to keep its recipient). This only "
        "prepares: the user must confirm the exact preview afterwards.",
        {
            "subject": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_CHARS},
            "body": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_CHARS},
            "recipient_kind": _RECIPIENT_KIND,
            "recipient_address": _NULLABLE_SHORT_TEXT,
            "recipient_name": _NULLABLE_SHORT_TEXT,
            "sender_account": _NULLABLE_SHORT_TEXT,
            "draft_id": _NULLABLE_SHORT_TEXT,
        },
        (
            "subject",
            "body",
            "recipient_kind",
            "recipient_address",
            "recipient_name",
            "sender_account",
            "draft_id",
        ),
    ),
    _operation(
        "mail.prepare_new_send",
        "Freeze the new-mail draft into an immutable mail.send action and show the user the exact "
        "preview. Use it in the same turn as mail.compose_new with draft_id null, or later with "
        "the draft id from recent_entities. This prepares only: it never approves and never sends.",
        {"draft_id": _NULLABLE_SHORT_TEXT},
        ("draft_id",),
    ),
    _operation(
        "contact.list",
        "List the user's contacts. Use this for 我有哪些联系人.",
        {"include_retired": {"type": "boolean"}},
        ("include_retired",),
    ),
    _operation(
        "contact.create",
        "Record one contact: a name and the address the user wrote for it. Use this when the user "
        "tells you someone's address and asks you to remember it as a contact. The address must "
        "appear in the user's own message; the runtime refuses one that does not.",
        {
            "display_name": {"type": "string", "minLength": 1, "maxLength": 120},
            "email_address": {"type": "string", "minLength": 1, "maxLength": 320},
        },
        ("display_name", "email_address"),
    ),
    _operation(
        "contact.edit",
        "Change one contact's name and/or address, by a contact_id from recent_entities. Null "
        "means 'leave unchanged'.",
        {
            "contact_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "display_name": {"type": ["string", "null"], "maxLength": 120},
            "email_address": {"type": ["string", "null"], "maxLength": 320},
        },
        ("contact_id", "display_name", "email_address"),
    ),
    _operation(
        "contact.retire",
        "Stop using one contact, by a contact_id from recent_entities. Use this for 以后不要用这个"
        "联系人了. It deletes nothing; there is no contact delete.",
        {"contact_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        ("contact_id",),
    ),
    _operation(
        "fact.list",
        "List the long-term personal facts the user has confirmed. Use this for 我有哪些长期信息 / "
        "你记得什么 about me.",
    ),
    _operation(
        "fact.show",
        "Read ONE confirmed long-term fact by its key. Use a key from recent_entities when one "
        "matches the question (for example profile.office for 我的办公室). Never answer a personal "
        "fact from memory, from the conversation, or from world knowledge: if this returns "
        "nothing, the honest answer is that nothing has been confirmed. This never writes.",
        {"key": {"type": "string", "minLength": 1, "maxLength": 128}},
        ("key",),
    ),
    _operation(
        "fact.propose",
        "Propose one long-term fact to remember. Use ONLY when the user explicitly asks you to "
        "remember something long-term (记住 / 以后记得 / 保存为长期信息) or corrects a remembered "
        "value. Quote the user's own sentence in correction_text. This creates a reviewable "
        "proposal and NEVER confirms it: the runtime shows the exact preview and the user must say "
        "an explicit phrase such as 确认记住 afterwards. Never treat a passing statement as a "
        "memory request.",
        {
            "key": {"type": "string", "minLength": 1, "maxLength": 128},
            "value": {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT_CHARS},
            "correction_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_TEXT_CHARS,
            },
        },
        ("key", "value", "correction_text"),
    ),
    _operation(
        "brief.today",
        "Summarise the user's own today: today's fixed time, the tasks that matter, what needs "
        "attention, what is waiting for the user and what is unresolved. Use this for 我今天有什 "
        "么事 / 今天要做什么 / 最近有什么需要我处理的. It reads real local state and writes "
        "nothing; never answer these from memory.",
    ),
    _operation(
        "system.capabilities",
        "Describe what this build can currently do, from the runtime. Use this for any question "
        "about Tree's own abilities instead of answering from memory.",
    ),
)

CONVERSATION_SCHEMA_V1: JsonSchemaOutput = JsonSchemaOutput(
    name=CONVERSATION_SCHEMA_NAME,
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"enum": ["direct_reply", "clarification", "operations"]},
            "reply": {"type": ["string", "null"], "maxLength": _MAX_REPLY_CHARS},
            "clarification": {"type": ["string", "null"], "maxLength": _MAX_CLARIFICATION_CHARS},
            "operations": {
                "type": "array",
                "maxItems": MAX_OPERATIONS_PER_TURN,
                "items": {"oneOf": list(OPERATION_SCHEMAS)},
            },
        },
        "required": ["mode", "reply", "clarification", "operations"],
    },
)
"""The schema every conversation turn is asked to satisfy."""


__all__ = [
    "CONVERSATION_SCHEMA_NAME",
    "CONVERSATION_SCHEMA_V1",
    "OPERATION_SCHEMAS",
]
