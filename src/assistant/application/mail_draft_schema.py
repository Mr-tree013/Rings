"""The JSON Schema a reply draft must satisfy (ADR-0022).

Closed and total: three fields, all required, no additional property allowed. There is nowhere to
put a recipient, a subject, a send instruction, an approval, a task, a tool, a shell command, a
confidence score or an explanation of the answer — because none of those is the model's to
decide. The recipients and the subject are derived locally (see `assistant.domain.mail_draft`),
and sending does not exist in this phase at all.

`used_source_ids` is only *shaped* here (`^S[1-9][0-9]*$`). Whether an id was actually supplied in
this request is decided after the call, against the exact evidence list.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.mail_draft import (
    MAX_DRAFT_BODY_CHARS,
    MAX_NEEDS_USER_INPUT,
    MAX_NEEDS_USER_INPUT_CHARS,
    MAX_USED_SOURCES,
)
from assistant.domain.model import JsonSchemaOutput

MAIL_REPLY_DRAFT_SCHEMA_NAME = "mail_reply_draft_v1"
MAIL_REPLY_DRAFT_SCHEMA_VERSION = 1

MAIL_REPLY_DRAFT_SCHEMA_V1 = JsonSchemaOutput(
    name=MAIL_REPLY_DRAFT_SCHEMA_NAME,
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "mail_reply_draft_v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "body": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_DRAFT_BODY_CHARS,
                "description": "The reply body. Plain text, no citations, no headers.",
            },
            "used_source_ids": {
                "type": "array",
                "maxItems": MAX_USED_SOURCES,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": "^S[1-9][0-9]*$",
                    "description": "An opaque knowledge id supplied in this exact request.",
                },
            },
            "needs_user_input": {
                "type": "array",
                "maxItems": MAX_NEEDS_USER_INPUT,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_NEEDS_USER_INPUT_CHARS,
                },
                "description": (
                    "Personal facts the reply would need but that the supplied material does "
                    "not support. The user supplies them; the draft never invents them."
                ),
            },
        },
        "required": ["body", "used_source_ids", "needs_user_input"],
    },
)
"""The exact schema sent as `json_schema` for every draft request."""


def draft_schema_payload() -> dict[str, Any]:
    """The schema document, for tests that pin its property set."""
    return MAIL_REPLY_DRAFT_SCHEMA_V1.schema


__all__ = [
    "MAIL_REPLY_DRAFT_SCHEMA_NAME",
    "MAIL_REPLY_DRAFT_SCHEMA_V1",
    "MAIL_REPLY_DRAFT_SCHEMA_VERSION",
    "draft_schema_payload",
]
