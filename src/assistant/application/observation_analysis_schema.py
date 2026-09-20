"""The JSON Schema a web/manual analysis must satisfy (ADR-0029).

Closed and total, like every other structured output in this project: every field is required, no
additional property is allowed, and the vocabulary is exactly what the domain models. What is
missing is the point — there is no field for a command, a task, a case, an action request, an
approval, a URL, an HTTP request, a tool call, a shell command, a confidence score or an account of
how the answer was reached.

`temporal_kind` keeps a deadline and an event start apart, exactly as mail analysis does, because
"due by Friday" and "happens on Friday" are different facts about the world.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.model import JsonSchemaOutput
from assistant.domain.observation_analysis import (
    MAX_ACTION_CANDIDATES,
    MAX_CANDIDATE_TEXT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TIME_TEXT_CHARS,
)

OBSERVATION_ANALYSIS_SCHEMA_NAME = "observation_analysis_v1"
OBSERVATION_ANALYSIS_SCHEMA_VERSION = 1

_CANDIDATE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_CANDIDATE_TEXT_CHARS},
        "temporal_kind": {"enum": ["none", "deadline", "event-start", "other"]},
        "time_text": {
            "type": ["string", "null"],
            "maxLength": MAX_TIME_TEXT_CHARS,
            "description": "The time expression exactly as it appears in the content, if any.",
        },
        "interpreted_at": {
            "type": ["string", "null"],
            "format": "date-time",
            "description": (
                "The instant the time expression names, as ISO 8601 with an explicit UTC offset, "
                "or null when it cannot be resolved."
            ),
        },
    },
    "required": ["text", "temporal_kind", "time_text", "interpreted_at"],
}

OBSERVATION_ANALYSIS_SCHEMA_V1 = JsonSchemaOutput(
    name=OBSERVATION_ANALYSIS_SCHEMA_NAME,
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": OBSERVATION_ANALYSIS_SCHEMA_NAME,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {
                "enum": ["informational", "actionable", "ignore", "unknown"],
                "description": "Whether the content is information, something to act on, or noise.",
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SUMMARY_CHARS,
                "description": "One or two sentences describing what the content says.",
            },
            "action_candidates": {
                "type": "array",
                "maxItems": MAX_ACTION_CANDIDATES,
                "items": _CANDIDATE,
                "description": "Possible things to do, named as sentences. Never commands.",
            },
        },
        "required": ["category", "summary", "action_candidates"],
    },
)


__all__ = [
    "OBSERVATION_ANALYSIS_SCHEMA_NAME",
    "OBSERVATION_ANALYSIS_SCHEMA_V1",
    "OBSERVATION_ANALYSIS_SCHEMA_VERSION",
]
