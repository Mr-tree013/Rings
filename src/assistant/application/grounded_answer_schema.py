"""The JSON Schema a grounded answer must satisfy (ADR-0019).

Two mutually exclusive branches, both closed:

- `answered`: 1-12 segments, each with 1-8 citations drawn from the opaque `S<n>` ids the
  request supplied, and no reason;
- `insufficient_evidence`: no segments, and a short reason.

There is deliberately nowhere to put a path, a page number, a line range, a confidence or a
rationale: source metadata is resolved locally from the evidence map, and a model's
self-assessment is not a product fact. `source_ids` is only *shaped* here (`^S[1-9][0-9]*$`);
whether an id actually exists is decided after the call, against the exact request context.
"""

from __future__ import annotations

from typing import Any

from assistant.domain.grounded_answer import (
    MAX_CITATIONS_PER_SEGMENT,
    MAX_INSUFFICIENT_REASON_CHARS,
    MAX_SEGMENT_CHARS,
    MAX_SEGMENTS,
)
from assistant.domain.model import JsonSchemaOutput

GROUNDED_ANSWER_SCHEMA_NAME = "grounded_answer_v1"
GROUNDED_ANSWER_SCHEMA_VERSION = 1

_SOURCE_ID = {
    "type": "string",
    "pattern": "^S[1-9][0-9]*$",
    "description": "An opaque source id supplied in this request's evidence list.",
}

_SEGMENT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_SEGMENT_CHARS},
        "source_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_CITATIONS_PER_SEGMENT,
            "uniqueItems": True,
            "items": _SOURCE_ID,
        },
    },
    "required": ["text", "source_ids"],
}

_ANSWERED_BRANCH: dict[str, Any] = {
    "properties": {
        "status": {"const": "answered"},
        "segments": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SEGMENTS,
            "items": _SEGMENT,
        },
        "reason": {"type": "null"},
    },
    "required": ["status", "segments", "reason"],
}

_INSUFFICIENT_BRANCH: dict[str, Any] = {
    "properties": {
        "status": {"const": "insufficient_evidence"},
        "segments": {"type": "array", "maxItems": 0},
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_INSUFFICIENT_REASON_CHARS,
        },
    },
    "required": ["status", "segments", "reason"],
}

GROUNDED_ANSWER_SCHEMA_V1 = JsonSchemaOutput(
    name=GROUNDED_ANSWER_SCHEMA_NAME,
    schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "grounded_answer_v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"enum": ["answered", "insufficient_evidence"]},
            "segments": {
                "type": "array",
                "maxItems": MAX_SEGMENTS,
                "items": _SEGMENT,
            },
            "reason": {
                "type": ["string", "null"],
                "maxLength": MAX_INSUFFICIENT_REASON_CHARS,
            },
        },
        "required": ["status", "segments", "reason"],
        "oneOf": [_ANSWERED_BRANCH, _INSUFFICIENT_BRANCH],
    },
)
"""The exact schema sent as `json_schema` for every grounded-answer request."""


__all__ = [
    "GROUNDED_ANSWER_SCHEMA_NAME",
    "GROUNDED_ANSWER_SCHEMA_V1",
    "GROUNDED_ANSWER_SCHEMA_VERSION",
]
