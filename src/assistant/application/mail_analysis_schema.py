"""The JSON Schema a mail analysis must satisfy (ADR-0021).

Closed and total: every field is required and no additional property is allowed. The vocabulary
is exactly the four categories and the four temporal kinds the domain already models, and there
is deliberately nowhere to put an instruction to act, a destination, or an account of how the
answer was reached. The schema asks about a message; it authorises nothing.

`temporal_kind` is the reason this schema is not one field shorter. A registration *deadline*
and an event *start time* are different facts about the world, and a message can contain both;
merging them into a single date would throw the distinction away before the user ever sees it.

`interpreted_at` carries `format: date-time` as documentation only. `format` is an annotation in
JSON Schema unless a format checker is supplied, and this project deliberately does not rely on
one: the value is parsed and checked for an explicit UTC offset *locally*, after the schema has
accepted the shape.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.mail_analysis import (
    MAX_ACTION_CANDIDATES,
    MAX_CANDIDATE_TEXT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TIME_TEXT_CHARS,
)
from assistant.domain.model import JsonSchemaOutput

MAIL_ANALYSIS_SCHEMA_NAME = "mail_analysis_v1"
MAIL_ANALYSIS_SCHEMA_VERSION = 1

_CANDIDATE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_CANDIDATE_TEXT_CHARS},
        "temporal_kind": {"enum": ["none", "deadline", "event_start", "other"]},
        "time_text": {
            "type": ["string", "null"],
            "maxLength": MAX_TIME_TEXT_CHARS,
            "description": "The time expression exactly as it appears in the message, if any.",
        },
        "interpreted_at": {
            "type": ["string", "null"],
            "format": "date-time",
            "description": (
                "The instant the time expression names, as ISO 8601 with an explicit UTC "
                "offset, or null when it cannot be resolved."
            ),
        },
    },
    "required": ["text", "temporal_kind", "time_text", "interpreted_at"],
}

MAIL_ANALYSIS_SCHEMA_V1 = JsonSchemaOutput(
    name=MAIL_ANALYSIS_SCHEMA_NAME,
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "mail_analysis_v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {
                "enum": [
                    "ordinary_correspondence",
                    "receipt_result",
                    "actionable_notice",
                    "unknown",
                ]
            },
            "requires_reply": {"type": "boolean"},
            "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS},
            "action_candidates": {
                "type": "array",
                "maxItems": MAX_ACTION_CANDIDATES,
                "items": _CANDIDATE,
            },
        },
        "required": ["category", "requires_reply", "summary", "action_candidates"],
    },
)
"""The exact schema sent as `json_schema` for every mail analysis request."""


__all__ = [
    "MAIL_ANALYSIS_SCHEMA_NAME",
    "MAIL_ANALYSIS_SCHEMA_V1",
    "MAIL_ANALYSIS_SCHEMA_VERSION",
]
