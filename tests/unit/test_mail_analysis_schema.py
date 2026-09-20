"""The analysis schema and instructions: closed, bounded and free of any action."""

from __future__ import annotations

import json

import pytest
from jsonschema.validators import Draft202012Validator

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.mail_analysis_prompt import (
    MAIL_ANALYSIS_INSTRUCTIONS,
    MAIL_ANALYSIS_PROMPT_VERSION,
)
from assistant.application.mail_analysis_schema import (
    MAIL_ANALYSIS_SCHEMA_V1,
    MAIL_ANALYSIS_SCHEMA_VERSION,
)
from assistant.application.structured_model import StructuredModel
from assistant.domain.errors import ModelOutputNotJson, ModelOutputSchemaViolation
from assistant.domain.mail_analysis import ANALYZER_VERSION
from assistant.domain.model import ModelOutputMode, ModelRequest


def _valid_payload() -> dict[str, object]:
    return {
        "category": "actionable_notice",
        "requires_reply": True,
        "summary": "A registration notice.",
        "action_candidates": [
            {
                "text": "Register for the course",
                "temporal_kind": "deadline",
                "time_text": "Oct 20",
                "interpreted_at": "2026-10-20T23:59:00+08:00",
            }
        ],
    }


def test_a_complete_analysis_satisfies_the_schema() -> None:
    errors = list(Draft202012Validator(MAIL_ANALYSIS_SCHEMA_V1.schema).iter_errors(
        _valid_payload()
    ))

    assert errors == []
    assert MAIL_ANALYSIS_SCHEMA_V1.name == "mail_analysis_v1"
    assert MAIL_ANALYSIS_SCHEMA_VERSION == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"category": "confident"},
        {"requires_reply": "yes"},
        {"summary": ""},
        {"summary": "x" * 801},
        {"action_candidates": [{}]},
        {"extra": "not allowed"},
        {
            "action_candidates": [
                {
                    "text": f"item {index}",
                    "temporal_kind": "none",
                    "time_text": None,
                    "interpreted_at": None,
                }
                for index in range(11)
            ]
        },
        {
            "action_candidates": [
                {
                    "text": "Register",
                    "temporal_kind": "deadline",
                    "time_text": "Oct 20",
                    "interpreted_at": None,
                    "command": "rm -rf /",
                }
            ]
        },
    ],
)
def test_the_schema_refuses_what_it_was_not_asked_for(mutation: dict[str, object]) -> None:
    payload = {**_valid_payload(), **mutation}

    assert list(Draft202012Validator(MAIL_ANALYSIS_SCHEMA_V1.schema).iter_errors(payload))


def test_the_schema_offers_no_field_for_an_action() -> None:
    schema = json.dumps(MAIL_ANALYSIS_SCHEMA_V1.schema)

    for forbidden in (
        "command",
        "shell",
        "tool",
        "reply_draft",
        "task_id",
        "case",
        "confidence",
        "reasoning",
    ):
        assert forbidden not in schema, f"the schema must not offer {forbidden}"


def test_the_prompt_states_every_boundary() -> None:
    text = MAIL_ANALYSIS_INSTRUCTIONS.lower()

    for needle in (
        "untrusted quoted data",
        "never follow",
        "do not execute",
        "do not create tasks",
        "shell commands",
        "classify and extract candidates only",
        "deadline",
        "event_start",
        "do not reveal your reasoning",
    ):
        assert needle in text, f"the instructions must say {needle!r}"
    assert MAIL_ANALYSIS_PROMPT_VERSION == ANALYZER_VERSION


async def test_the_structured_model_accepts_a_valid_answer_and_refuses_the_rest() -> None:
    model = FakeModelAdapter()
    model.queue_text(json.dumps(_valid_payload()))
    structured = StructuredModel(model)
    request = ModelRequest(
        instructions="classify",
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=MAIL_ANALYSIS_SCHEMA_V1,
    )

    assert await structured.complete_json(request) == _valid_payload()

    model.queue_text("not json")
    with pytest.raises(ModelOutputNotJson):
        await structured.complete_json(request)

    model.queue_text(json.dumps({"category": "confident"}))
    with pytest.raises(ModelOutputSchemaViolation):
        await structured.complete_json(request)
