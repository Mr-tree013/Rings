"""The ModelPort contract itself, and the fake that implements it (ADR-0017)."""

from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest

from assistant.adapters.model.deepseek import DeepSeekAdapter
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.domain.errors import ModelTransientError
from assistant.domain.model import (
    JsonSchemaOutput,
    ModelMessage,
    ModelOutputMode,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
)
from assistant.ports.model import ModelPort

SCHEMA = JsonSchemaOutput(
    name="intent",
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
    },
)


def _request() -> ModelRequest:
    return ModelRequest(
        instructions="Classify.",
        messages=(ModelMessage(role=ModelRole.USER, content="write the report"),),
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=SCHEMA,
    )


def _deepseek() -> DeepSeekAdapter:
    return DeepSeekAdapter(
        api_key="test-secret",
        model="deepseek-flash",
        timeout_seconds=30,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        ),
    )


def test_the_port_has_exactly_one_operation() -> None:
    """No tools, no streaming, no memory: the port says one thing and only one thing."""
    members = {
        name
        for name, _ in inspect.getmembers(ModelPort, predicate=callable)
        if not name.startswith("_")
    }

    assert members == {"complete"}


def test_both_adapters_satisfy_the_port() -> None:
    assert isinstance(FakeModelAdapter(), ModelPort)
    assert isinstance(_deepseek(), ModelPort)


async def test_the_port_is_awaited_the_same_way_for_every_adapter() -> None:
    model = FakeModelAdapter().queue_text('{"intent": "unknown"}')

    async def call(adapter: ModelPort) -> ModelResponse:
        return await adapter.complete(_request())

    response = await call(model)

    assert response.text == '{"intent": "unknown"}'


# ------------------------------------------------------------------ fake adapter


async def test_the_fake_returns_scripted_responses_in_order() -> None:
    model = FakeModelAdapter()
    model.queue_text("first", response_id="r1")
    model.queue_text("second", response_id="r2")

    first = await model.complete(_request())
    second = await model.complete(_request())

    assert (first.text, first.response_id) == ("first", "r1")
    assert (second.text, second.response_id) == ("second", "r2")
    assert first.provider == "fake"


async def test_the_fake_records_every_request_it_received() -> None:
    model = FakeModelAdapter().queue_text("a").queue_text("b")

    await model.complete(_request())
    await model.complete(ModelRequest(instructions="other"))

    assert [request.instructions for request in model.requests] == ["Classify.", "other"]
    assert model.requests[0].messages[0].content == "write the report"


async def test_the_fake_can_raise_a_scripted_error() -> None:
    model = FakeModelAdapter()
    model.queue_error(ModelTransientError("the provider blew up"))
    model.queue_text("recovered")

    with pytest.raises(ModelTransientError):
        await model.complete(_request())
    assert (await model.complete(_request())).text == "recovered"  # the queue keeps going


async def test_the_fake_reports_usage_when_scripted() -> None:
    model = FakeModelAdapter()
    model.queue_text("x", usage=ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7))

    response = await model.complete(_request())

    assert response.usage is not None and response.usage.total_tokens == 7


async def test_the_fake_exhausts_loudly_instead_of_returning_nothing() -> None:
    with pytest.raises(IndexError):
        await FakeModelAdapter().complete(_request())


async def test_the_fake_is_asynchronous_and_does_not_block() -> None:
    model = FakeModelAdapter().queue_text("x")

    task = asyncio.create_task(model.complete(_request()))
    await asyncio.sleep(0)

    assert task.done() is False or task.result().text == "x"
    assert (await task).text == "x"
