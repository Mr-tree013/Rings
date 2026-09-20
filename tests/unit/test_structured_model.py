"""Local structured-output validation, independent of any provider (ADR-0017)."""

from __future__ import annotations

import json

import pytest

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.structured_model import (
    StructuredModel,
    parse_structured_output,
    validate_json_schema,
)
from assistant.domain.errors import (
    InvalidModelSchema,
    ModelOutputNotJson,
    ModelOutputSchemaViolation,
    ModelProtocolError,
)
from assistant.domain.model import (
    JsonSchemaOutput,
    ModelMessage,
    ModelOutputMode,
    ModelRequest,
    ModelResponse,
    ModelRole,
)

INTENT_SCHEMA = JsonSchemaOutput(
    name="intent",
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string"},
            "confidence": {"type": "number"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"title": {"type": "string"}, "minutes": {"type": "integer"}},
                    "required": ["title"],
                },
            },
        },
        "required": ["intent", "confidence"],
    },
)


def _request() -> ModelRequest:
    return ModelRequest(
        instructions="Classify the request.",
        messages=(ModelMessage(role=ModelRole.USER, content="remind me on Friday"),),
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=INTENT_SCHEMA,
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(text=text, model="fake-model")


async def test_a_valid_structured_answer_is_returned_as_data() -> None:
    payload = {"intent": "unknown", "confidence": 0.5}
    model = FakeModelAdapter().queue_text(json.dumps(payload))

    result = await StructuredModel(model).complete_json(_request())

    assert result == payload
    assert len(model.requests) == 1  # exactly one call, no retry, no second prompt
    assert model.requests[0].json_schema is INTENT_SCHEMA


async def test_nested_objects_are_validated_too() -> None:
    payload = {
        "intent": "plan",
        "confidence": 0.9,
        "steps": [{"title": "draft", "minutes": 30}, {"title": "review"}],
    }
    model = FakeModelAdapter().queue_text(json.dumps(payload))

    assert await StructuredModel(model).complete_json(_request()) == payload


async def test_text_that_is_not_json_is_rejected() -> None:
    model = FakeModelAdapter().queue_text("Sure! Here is your JSON: {...}")

    with pytest.raises(ModelOutputNotJson):
        await StructuredModel(model).complete_json(_request())


async def test_json_that_is_not_an_object_is_rejected() -> None:
    model = FakeModelAdapter().queue_text("[1, 2, 3]")

    with pytest.raises(ModelOutputNotJson):
        await StructuredModel(model).complete_json(_request())


async def test_an_empty_answer_is_never_a_success() -> None:
    model = FakeModelAdapter().queue_text("   ")

    with pytest.raises(ModelOutputNotJson):
        await StructuredModel(model).complete_json(_request())


async def test_a_wrong_type_is_rejected_locally() -> None:
    """The provider claimed structured output; code still decides whether it is acceptable."""
    model = FakeModelAdapter().queue_text(json.dumps({"intent": 5, "confidence": 0.5}))

    with pytest.raises(ModelOutputSchemaViolation) as excinfo:
        await StructuredModel(model).complete_json(_request())

    assert "intent" in str(excinfo.value)
    assert "'type'" in str(excinfo.value)  # the failing validator is named


async def test_a_missing_required_property_is_rejected() -> None:
    model = FakeModelAdapter().queue_text(json.dumps({"intent": "plan"}))

    with pytest.raises(ModelOutputSchemaViolation) as excinfo:
        await StructuredModel(model).complete_json(_request())

    assert "required" in str(excinfo.value)


async def test_extra_properties_are_rejected_when_the_schema_forbids_them() -> None:
    model = FakeModelAdapter().queue_text(
        json.dumps({"intent": "plan", "confidence": 1.0, "execute": True})
    )

    with pytest.raises(ModelOutputSchemaViolation) as excinfo:
        await StructuredModel(model).complete_json(_request())

    assert "additionalProperties" in str(excinfo.value)


async def test_the_violation_message_never_contains_the_whole_output() -> None:
    secret = "x" * 400
    model = FakeModelAdapter().queue_text(
        json.dumps({"intent": secret, "confidence": "not a number"})
    )

    with pytest.raises(ModelOutputSchemaViolation) as excinfo:
        await StructuredModel(model).complete_json(_request())

    assert secret not in str(excinfo.value)


async def test_an_invalid_schema_is_refused_before_the_model_is_called() -> None:
    broken = JsonSchemaOutput(name="broken", schema={"type": "object", "properties": "nope"})
    model = FakeModelAdapter().queue_text("{}")
    request = ModelRequest(
        instructions="x",
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=broken,
    )

    with pytest.raises(InvalidModelSchema):
        await StructuredModel(model).complete_json(request)

    assert model.requests == []  # nothing was sent to the provider


def test_validate_json_schema_accepts_a_valid_schema() -> None:
    validate_json_schema(INTENT_SCHEMA)


async def test_a_text_request_is_not_a_structured_request() -> None:
    model = FakeModelAdapter().queue_text("plain text")

    with pytest.raises(ModelProtocolError):
        await StructuredModel(model).complete_json(
            ModelRequest(instructions="x", output_mode=ModelOutputMode.TEXT)
        )


def test_parse_structured_output_validates_without_any_port() -> None:
    payload = {"intent": "unknown", "confidence": 0.1}

    assert parse_structured_output(_response(json.dumps(payload)), INTENT_SCHEMA) == payload
