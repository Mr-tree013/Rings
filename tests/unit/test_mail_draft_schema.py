"""The reply-draft schema and instructions: closed, bounded, and unable to send."""

from __future__ import annotations

import json

import pytest
from jsonschema.validators import Draft202012Validator

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.mail_draft_prompt import (
    MAIL_REPLY_DRAFT_INSTRUCTIONS,
    MAIL_REPLY_DRAFT_PROMPT_VERSION,
)
from assistant.application.mail_draft_schema import (
    MAIL_REPLY_DRAFT_SCHEMA_V1,
    MAIL_REPLY_DRAFT_SCHEMA_VERSION,
    draft_schema_payload,
)
from assistant.application.structured_model import StructuredModel
from assistant.domain.errors import ModelOutputNotJson, ModelOutputSchemaViolation
from assistant.domain.mail_draft import PROMPT_VERSION
from assistant.domain.model import ModelOutputMode, ModelRequest


def _valid_payload() -> dict[str, object]:
    return {
        "body": "Thanks — I will submit the report on Friday.",
        "used_source_ids": ["S1"],
        "needs_user_input": [],
    }


def test_a_complete_draft_satisfies_the_schema() -> None:
    errors = list(
        Draft202012Validator(MAIL_REPLY_DRAFT_SCHEMA_V1.schema).iter_errors(_valid_payload())
    )

    assert errors == []
    assert MAIL_REPLY_DRAFT_SCHEMA_V1.name == "mail_reply_draft_v1"
    assert MAIL_REPLY_DRAFT_SCHEMA_VERSION == 1


def test_the_schema_offers_nothing_but_a_body_and_its_notes() -> None:
    schema = draft_schema_payload()

    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == ["body", "needs_user_input", "used_source_ids"]
    assert sorted(schema["required"]) == sorted(schema["properties"])
    for forbidden in (
        "to",
        "cc",
        "bcc",
        "subject",
        "send",
        "sent",
        "approve",
        "approval",
        "task",
        "case",
        "tool",
        "shell",
        "command",
        "confidence",
        "reasoning",
    ):
        assert forbidden not in schema["properties"], forbidden


@pytest.mark.parametrize(
    "mutation",
    [
        {"body": ""},
        {"body": "x" * 12001},
        {"used_source_ids": ["S1", "S1"]},
        {"used_source_ids": ["1"]},
        {"used_source_ids": ["s1"]},
        {"used_source_ids": [f"S{index}" for index in range(1, 10)]},
        {"needs_user_input": [""]},
        {"needs_user_input": ["x" * 501]},
        {"needs_user_input": [f"q{index}" for index in range(9)]},
        {"subject": "Re: something"},
        {"send": True},
        {"confidence": 0.9},
    ],
)
def test_the_schema_refuses_what_it_was_not_asked_for(mutation: dict[str, object]) -> None:
    payload = {**_valid_payload(), **mutation}

    assert list(Draft202012Validator(MAIL_REPLY_DRAFT_SCHEMA_V1.schema).iter_errors(payload))


@pytest.mark.parametrize("dropped", ["body", "used_source_ids", "needs_user_input"])
def test_every_field_is_required(dropped: str) -> None:
    payload = {key: value for key, value in _valid_payload().items() if key != dropped}

    assert list(Draft202012Validator(MAIL_REPLY_DRAFT_SCHEMA_V1.schema).iter_errors(payload))


def test_the_prompt_states_every_boundary() -> None:
    # Whitespace-normalised, so a rule that wraps across a line is still checked as one phrase.
    text = " ".join(MAIL_REPLY_DRAFT_INSTRUCTIONS.lower().split())

    for needle in (
        "untrusted quoted data",
        "never follow",
        "write a reply draft only",
        "do not create tasks",
        "do not write shell commands",
        "do not invent personal facts",
        "supported by the mail thread or by the supplied",
        "needs_user_input",
        "never invent a source id",
        "cite nothing inside the body",
        "do not reveal your reasoning",
        "never claim",
    ):
        assert needle in text, f"the instructions must say {needle!r}"
    assert MAIL_REPLY_DRAFT_PROMPT_VERSION == PROMPT_VERSION


async def test_the_structured_model_accepts_a_valid_answer_and_refuses_the_rest() -> None:
    model = FakeModelAdapter()
    model.queue_text(json.dumps(_valid_payload()))
    structured = StructuredModel(model)
    request = ModelRequest(
        instructions="draft a reply",
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=MAIL_REPLY_DRAFT_SCHEMA_V1,
    )

    assert await structured.complete_json(request) == _valid_payload()

    model.queue_text("not json")
    with pytest.raises(ModelOutputNotJson):
        await structured.complete_json(request)

    model.queue_text(json.dumps({"body": "hi", "to": "evil@example.com"}))
    with pytest.raises(ModelOutputSchemaViolation):
        await structured.complete_json(request)
