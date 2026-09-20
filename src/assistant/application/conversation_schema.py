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
_NULLABLE_TEXT = {"type": ["string", "null"], "maxLength": _MAX_TEXT_CHARS}
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
