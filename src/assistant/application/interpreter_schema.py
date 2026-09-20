"""The one JSON Schema the interpreter asks a model to satisfy (ADR-0018).

The schema is deliberately narrow:

- the top level is a closed object (`additionalProperties: false`), so a provider inventing
  fields fails validation instead of being ignored;
- `status` selects one of three mutually exclusive branches via `oneOf`, so "ready with a
  question" is not a representable answer;
- every command is its own closed object with `kind` as a `const`, so there is no free-form
  `arguments` bag for a model to improvise in, and an unknown command (`send_email`) fails
  schema validation rather than quietly degrading into "unsupported".

Datetime fields carry `format: date-time` for the provider's benefit; the interpreter parses
and checks them again, because schema validation never replaces semantic validation.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.model import JsonSchemaOutput

INTERPRETER_SCHEMA_NAME = "command_interpretation_v1"
INTERPRETER_SCHEMA_VERSION = 1

_MAX_MESSAGE_LENGTH = 500
_MAX_TITLE_LENGTH = 500
_MAX_DESCRIPTION_LENGTH = 2000

_DATE_TIME: dict[str, Any] = {"type": "string", "format": "date-time"}
_NULLABLE_DATE_TIME: dict[str, Any] = {"type": ["string", "null"], "format": "date-time"}
_NULLABLE_TEXT = {"type": ["string", "null"], "maxLength": _MAX_DESCRIPTION_LENGTH}
_TASK_ID = {"type": "string", "format": "uuid"}


def _command(kind: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """One closed command object."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"kind": {"const": kind}, **properties},
        "required": ["kind", *required],
    }


CREATE_TASK_COMMAND = _command(
    "create_task",
    {
        "title": {"type": "string", "minLength": 1, "maxLength": _MAX_TITLE_LENGTH},
        "description": _NULLABLE_TEXT,
        # JSON null (not the string "null") means "the user did not say".
        "priority": {"enum": ["low", "normal", "high", None]},
        "estimated_minutes": {"type": ["integer", "null"], "minimum": 1},
        "deadline": _NULLABLE_DATE_TIME,
    },
    ["title", "description", "priority", "estimated_minutes", "deadline"],
)
COMPLETE_TASK_COMMAND = _command("complete_task", {"task_id": _TASK_ID}, ["task_id"])
CANCEL_TASK_COMMAND = _command("cancel_task", {"task_id": _TASK_ID}, ["task_id"])
SET_DEADLINE_COMMAND = _command(
    "set_deadline", {"task_id": _TASK_ID, "due_at": _DATE_TIME}, ["task_id", "due_at"]
)
CLEAR_DEADLINE_COMMAND = _command("clear_deadline", {"task_id": _TASK_ID}, ["task_id"])
CREATE_CALENDAR_EVENT_COMMAND = _command(
    "create_calendar_event",
    {
        "title": {"type": "string", "minLength": 1, "maxLength": _MAX_TITLE_LENGTH},
        "description": _NULLABLE_TEXT,
        "starts_at": _DATE_TIME,
        "ends_at": _DATE_TIME,
    },
    ["title", "description", "starts_at", "ends_at"],
)
REQUEST_WEEK_PLAN_COMMAND = _command(
    "request_week_plan", {"next_week": {"type": "boolean"}}, ["next_week"]
)

COMMAND_SCHEMAS: tuple[dict[str, Any], ...] = (
    CREATE_TASK_COMMAND,
    COMPLETE_TASK_COMMAND,
    CANCEL_TASK_COMMAND,
    SET_DEADLINE_COMMAND,
    CLEAR_DEADLINE_COMMAND,
    CREATE_CALENDAR_EVENT_COMMAND,
    REQUEST_WEEK_PLAN_COMMAND,
)

_READY_BRANCH: dict[str, Any] = {
    "properties": {
        "status": {"const": "ready"},
        "command": {"oneOf": list(COMMAND_SCHEMAS)},
        "question": {"type": "null"},
        "reason": {"type": "null"},
    },
    "required": ["status", "command", "question", "reason"],
}

_CLARIFICATION_BRANCH: dict[str, Any] = {
    "properties": {
        "status": {"const": "needs_clarification"},
        "command": {"type": "null"},
        "question": {"type": "string", "minLength": 1, "maxLength": _MAX_MESSAGE_LENGTH},
        "reason": {"type": "null"},
    },
    "required": ["status", "command", "question", "reason"],
}

_UNSUPPORTED_BRANCH: dict[str, Any] = {
    "properties": {
        "status": {"const": "unsupported"},
        "command": {"type": "null"},
        "question": {"type": "null"},
        "reason": {"type": "string", "minLength": 1, "maxLength": _MAX_MESSAGE_LENGTH},
    },
    "required": ["status", "command", "question", "reason"],
}

INTERPRETER_SCHEMA_V1 = JsonSchemaOutput(
    name=INTERPRETER_SCHEMA_NAME,
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "command_interpretation_v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"enum": ["ready", "needs_clarification", "unsupported"]},
            "command": {"oneOf": [*COMMAND_SCHEMAS, {"type": "null"}]},
            "question": {"type": ["string", "null"], "maxLength": _MAX_MESSAGE_LENGTH},
            "reason": {"type": ["string", "null"], "maxLength": _MAX_MESSAGE_LENGTH},
        },
        "required": ["status", "command", "question", "reason"],
        "oneOf": [_READY_BRANCH, _CLARIFICATION_BRANCH, _UNSUPPORTED_BRANCH],
    },
)
"""The exact schema sent as `json_schema` for every interpretation request."""


__all__ = [
    "COMMAND_SCHEMAS",
    "INTERPRETER_SCHEMA_NAME",
    "INTERPRETER_SCHEMA_V1",
    "INTERPRETER_SCHEMA_VERSION",
]
