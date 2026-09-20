"""The grounded-answer JSON Schema, checked against realistic model answers (ADR-0019)."""

from __future__ import annotations

import pytest
from jsonschema.validators import Draft202012Validator

from assistant.application.grounded_answer_schema import (
    GROUNDED_ANSWER_SCHEMA_NAME,
    GROUNDED_ANSWER_SCHEMA_V1,
    GROUNDED_ANSWER_SCHEMA_VERSION,
)
from assistant.application.structured_model import validate_json_schema


def _valid(payload: dict[str, object]) -> bool:
    return not list(Draft202012Validator(GROUNDED_ANSWER_SCHEMA_V1.schema).iter_errors(payload))


def test_the_schema_is_valid_and_named() -> None:
    validate_json_schema(GROUNDED_ANSWER_SCHEMA_V1)

    assert GROUNDED_ANSWER_SCHEMA_V1.name == GROUNDED_ANSWER_SCHEMA_NAME
    assert GROUNDED_ANSWER_SCHEMA_VERSION == 1
    assert GROUNDED_ANSWER_SCHEMA_V1.schema["additionalProperties"] is False


def test_an_answered_result_with_one_segment_is_valid() -> None:
    assert _valid(
        {
            "status": "answered",
            "segments": [{"text": "The deadline is October 23.", "source_ids": ["S1"]}],
            "reason": None,
        }
    )


def test_an_answered_result_with_several_segments_and_citations_is_valid() -> None:
    assert _valid(
        {
            "status": "answered",
            "segments": [
                {"text": "The submission deadline is October 23.", "source_ids": ["S1"]},
                {"text": "The event starts earlier.", "source_ids": ["S1", "S2", "S3"]},
            ],
            "reason": None,
        }
    )


def test_an_insufficient_result_is_valid_without_segments() -> None:
    assert _valid(
        {
            "status": "insufficient_evidence",
            "segments": [],
            "reason": "The indexed notice does not mention a deadline.",
        }
    )


@pytest.mark.parametrize(
    "payload",
    (
        # A segment must cite something.
        {
            "status": "answered",
            "segments": [{"text": "No citation here.", "source_ids": []}],
            "reason": None,
        },
        # Citations must look like the ids we hand out.
        {
            "status": "answered",
            "segments": [{"text": "Answer.", "source_ids": ["source-1"]}],
            "reason": None,
        },
        {
            "status": "answered",
            "segments": [{"text": "Answer.", "source_ids": ["S0"]}],
            "reason": None,
        },
        # Repeated citations in one segment are pointless.
        {
            "status": "answered",
            "segments": [{"text": "Answer.", "source_ids": ["S1", "S1"]}],
            "reason": None,
        },
        # An answered result without segments is not an answer.
        {"status": "answered", "segments": [], "reason": None},
        # An answered result may not carry a reason.
        {
            "status": "answered",
            "segments": [{"text": "Answer.", "source_ids": ["S1"]}],
            "reason": "because",
        },
        # An insufficient result may not carry segments or an empty reason.
        {
            "status": "insufficient_evidence",
            "segments": [{"text": "Answer.", "source_ids": ["S1"]}],
            "reason": "no",
        },
        {"status": "insufficient_evidence", "segments": [], "reason": ""},
        {"status": "insufficient_evidence", "segments": [], "reason": "x" * 501},
        # No room for model-authored source metadata.
        {
            "status": "answered",
            "segments": [
                {
                    "text": "Answer.",
                    "source_ids": ["S1"],
                    "logical_uri": "vault://archive-main/x.md",
                }
            ],
            "reason": None,
        },
        # No confidence, no reasoning, no extra top-level keys.
        {
            "status": "answered",
            "segments": [{"text": "Answer.", "source_ids": ["S1"]}],
            "reason": None,
            "confidence": 0.9,
        },
        {"status": "likely", "segments": [], "reason": "hmm"},
    ),
)
def test_answers_that_must_not_validate(payload: dict[str, object]) -> None:
    assert not _valid(payload)


def test_segment_and_citation_counts_are_bounded() -> None:
    too_many_segments = {
        "status": "answered",
        "segments": [
            {"text": f"Segment {index}.", "source_ids": ["S1"]} for index in range(13)
        ],
        "reason": None,
    }
    too_many_citations = {
        "status": "answered",
        "segments": [
            {
                "text": "Answer.",
                "source_ids": [f"S{index}" for index in range(1, 10)],
            }
        ],
        "reason": None,
    }

    assert not _valid(too_many_segments)
    assert not _valid(too_many_citations)
