"""Provider-neutral model value objects and their invariants (ADR-0017)."""

from __future__ import annotations

import pytest

from assistant.domain.errors import (
    InvalidModelRequest,
    InvalidModelSchema,
)
from assistant.domain.model import (
    JsonSchemaOutput,
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"intent": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["intent", "confidence"],
}


def test_the_vocabularies_are_provider_neutral() -> None:
    assert {role.value for role in ModelRole} == {"user", "assistant"}
    assert {effort.value for effort in ModelReasoningEffort} == {"none", "low", "high", "max"}
    assert {mode.value for mode in ModelOutputMode} == {"text", "json_schema"}


def test_messages_hold_non_blank_text() -> None:
    message = ModelMessage(role=ModelRole.USER, content="hello")

    assert message.role is ModelRole.USER
    with pytest.raises(InvalidModelRequest):
        ModelMessage(role=ModelRole.USER, content="   ")


def test_a_schema_needs_a_usable_name_and_object_shape() -> None:
    assert JsonSchemaOutput(name="intent_v1", schema=SCHEMA).schema == SCHEMA

    for name in ("", "has spaces", "a" * 129, "dot.name"):
        with pytest.raises(InvalidModelSchema):
            JsonSchemaOutput(name=name, schema=SCHEMA)
    with pytest.raises(InvalidModelSchema):
        JsonSchemaOutput(name="ok", schema=["not", "an", "object"])  # type: ignore[arg-type]


def test_a_request_needs_instructions_or_messages() -> None:
    with pytest.raises(InvalidModelRequest):
        ModelRequest()
    with pytest.raises(InvalidModelRequest):
        ModelRequest(instructions="   ")

    assert ModelRequest(instructions="be terse").instructions == "be terse"
    assert ModelRequest(messages=(ModelMessage(role=ModelRole.USER, content="hi"),)).messages


def test_output_mode_and_schema_must_agree() -> None:
    message = (ModelMessage(role=ModelRole.USER, content="hi"),)

    with pytest.raises(InvalidModelRequest):
        ModelRequest(
            messages=message, output_mode=ModelOutputMode.TEXT, json_schema=_schema()
        )
    with pytest.raises(InvalidModelRequest):
        ModelRequest(messages=message, output_mode=ModelOutputMode.JSON_SCHEMA)

    structured = ModelRequest(
        messages=message, output_mode=ModelOutputMode.JSON_SCHEMA, json_schema=_schema()
    )
    assert structured.json_schema is not None


def test_a_request_needs_at_least_one_output_token() -> None:
    with pytest.raises(InvalidModelRequest):
        ModelRequest(instructions="hi", max_output_tokens=0)


def test_usage_is_non_negative_accounting() -> None:
    usage = ModelUsage(input_tokens=10, output_tokens=5, reasoning_tokens=3, total_tokens=15)

    assert usage.cached_tokens == 0  # absent detail is simply zero
    with pytest.raises(InvalidModelRequest):
        ModelUsage(input_tokens=-1)
    with pytest.raises(InvalidModelRequest):
        ModelUsage(total_tokens=True)  # type: ignore[arg-type]


def test_a_response_carries_text_model_and_optional_accounting() -> None:
    response = ModelResponse(text="answer", model="some-model")

    assert response.response_id is None
    assert response.usage is None
    assert response.provider is None
    with pytest.raises(InvalidModelRequest):
        ModelResponse(text="answer", model="  ")


def test_a_response_cannot_represent_reasoning_or_raw_payloads() -> None:
    """The type itself is the privacy boundary: there is nowhere to put private reasoning."""
    fields = set(ModelResponse.__dataclass_fields__)

    assert fields == {"text", "model", "response_id", "usage", "provider", "metadata"}
    for forbidden in ("reasoning", "reasoning_content", "chain_of_thought", "raw", "headers"):
        assert forbidden not in fields


def test_a_request_cannot_represent_a_credential_or_a_provider_endpoint() -> None:
    fields = set(ModelRequest.__dataclass_fields__)

    for forbidden in ("api_key", "base_url", "authorization", "headers", "tools", "stream"):
        assert forbidden not in fields


def _schema() -> JsonSchemaOutput:
    return JsonSchemaOutput(name="intent_v1", schema=SCHEMA)
