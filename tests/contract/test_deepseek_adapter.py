"""DeepSeek Responses API adapter contract, exercised entirely through MockTransport.

No test here touches the network: every provider answer, malformed body and error status is
fabricated. What is checked is the *adapter's* promises — the exact outbound JSON, the exact
error taxonomy, the refusal to treat a partial or tool-calling answer as success, and the
guarantee that reasoning text and credentials never leave this module.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from assistant.adapters.model.deepseek import DEEPSEEK_BASE_URL, DeepSeekAdapter
from assistant.domain.errors import (
    ModelAuthenticationError,
    ModelBillingError,
    ModelCredentialsMissing,
    ModelInvalidRequest,
    ModelProtocolError,
    ModelRateLimited,
    ModelResponseIncomplete,
    ModelTransientError,
    ModelUnavailable,
)
from assistant.domain.model import (
    JsonSchemaOutput,
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelRole,
)

API_KEY = "super-secret-key-123"
SCHEMA = JsonSchemaOutput(
    name="intent",
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
    },
)


class RecordingTransport(httpx.MockTransport):
    """A MockTransport that keeps the request it was handed."""

    def __init__(self, handler: object) -> None:
        self.seen: list[httpx.Request] = []
        self.bodies: list[dict[str, object]] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if request.content:
                self.bodies.append(json.loads(request.content.decode()))
            return handler(request)  # type: ignore[operator]

        super().__init__(wrapped)


def _adapter(
    handler: object,
    *,
    timeout_seconds: float = 30,
    transport: httpx.MockTransport | None = None,
) -> tuple[DeepSeekAdapter, httpx.MockTransport]:
    recording = transport if transport is not None else RecordingTransport(handler)
    client = httpx.AsyncClient(
        base_url=DEEPSEEK_BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        transport=recording,
    )
    adapter = DeepSeekAdapter(
        api_key=API_KEY,
        model="deepseek-flash",
        timeout_seconds=timeout_seconds,
        client=client,
    )
    return adapter, recording


def _completed(
    text: str = '{"intent": "unknown"}',
    *,
    reasoning: str | None = None,
    extra_items: list[dict[str, object]] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    output: list[dict[str, object]] = []
    if reasoning is not None:
        output.append(
            {
                "type": "reasoning",
                "id": "rs_1",
                "status": "completed",
                "content": [{"type": "reasoning_text", "text": reasoning}],
                "summary": [],
            }
        )
    output.append(
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )
    if extra_items:
        output.extend(extra_items)
    body: dict[str, object] = {
        "id": "resp_123",
        "object": "response",
        "status": "completed",
        "model": "deepseek-flash",
        "output": output,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _request(
    *,
    output_mode: ModelOutputMode = ModelOutputMode.JSON_SCHEMA,
    effort: ModelReasoningEffort = ModelReasoningEffort.LOW,
    instructions: str | None = "Classify the request.",
    messages: tuple[ModelMessage, ...] | None = None,
) -> ModelRequest:
    return ModelRequest(
        instructions=instructions,
        messages=(
            messages
            if messages is not None
            else (ModelMessage(role=ModelRole.USER, content="write the report"),)
        ),
        output_mode=output_mode,
        json_schema=SCHEMA if output_mode is ModelOutputMode.JSON_SCHEMA else None,
        reasoning_effort=effort,
        max_output_tokens=512,
    )


# ------------------------------------------------------------- request mapping


async def test_a_structured_request_maps_onto_the_responses_api() -> None:
    adapter, transport = _adapter(lambda request: httpx.Response(200, json=_completed()))

    await adapter.complete(_request())

    (request,) = transport.seen
    assert str(request.url) == f"{DEEPSEEK_BASE_URL}/responses"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"] == "application/json"
    body = transport.bodies[0]
    assert body["model"] == "deepseek-flash"
    assert body["instructions"] == "Classify the request."
    assert body["max_output_tokens"] == 512
    assert body["stream"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["input"] == [
        {"type": "message", "role": "user", "content": "write the report"}
    ]
    assert body["text"] == {
        "format": {"type": "json_schema", "name": "intent", "schema": SCHEMA.schema}
    }


async def test_a_text_request_uses_the_text_format() -> None:
    adapter, transport = _adapter(
        lambda request: httpx.Response(200, json=_completed("hello"))
    )

    await adapter.complete(_request(output_mode=ModelOutputMode.TEXT))

    assert transport.bodies[0]["text"] == {"format": {"type": "text"}}


@pytest.mark.parametrize(
    ("effort", "mapped"),
    (
        (ModelReasoningEffort.NONE, "none"),
        (ModelReasoningEffort.LOW, "low"),
        (ModelReasoningEffort.HIGH, "high"),
        (ModelReasoningEffort.MAX, "max"),
    ),
)
async def test_reasoning_effort_is_mapped_without_inventing_aliases(
    effort: ModelReasoningEffort, mapped: str
) -> None:
    adapter, transport = _adapter(lambda request: httpx.Response(200, json=_completed()))

    await adapter.complete(_request(effort=effort))

    assert transport.bodies[0]["reasoning"] == {"effort": mapped}


async def test_the_adapter_never_sends_tools_or_a_user_identifier() -> None:
    adapter, transport = _adapter(lambda request: httpx.Response(200, json=_completed()))

    await adapter.complete(_request())

    body = transport.bodies[0]
    for forbidden in ("tools", "tool_choice", "function_call", "user", "user_id", "stream_options"):
        assert forbidden not in body


async def test_a_multi_turn_request_keeps_message_order() -> None:
    adapter, transport = _adapter(lambda request: httpx.Response(200, json=_completed()))
    messages = (
        ModelMessage(role=ModelRole.USER, content="first"),
        ModelMessage(role=ModelRole.ASSISTANT, content="second"),
        ModelMessage(role=ModelRole.USER, content="third"),
    )

    await adapter.complete(_request(messages=messages))

    assert transport.bodies[0]["input"] == [
        {"type": "message", "role": "user", "content": "first"},
        {"type": "message", "role": "assistant", "content": "second"},
        {"type": "message", "role": "user", "content": "third"},
    ]


async def test_instructions_are_omitted_when_absent() -> None:
    adapter, transport = _adapter(lambda request: httpx.Response(200, json=_completed()))

    await adapter.complete(_request(instructions=None))

    assert "instructions" not in transport.bodies[0]


# ------------------------------------------------------------ response reading


async def test_a_completed_response_returns_only_final_text() -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(
            200, json=_completed('{"intent": "unknown"}', reasoning="I should think first")
        )
    )

    response = await adapter.complete(_request())

    assert response.text == '{"intent": "unknown"}'
    assert response.model == "deepseek-flash"
    assert response.response_id == "resp_123"
    assert response.provider == "deepseek"


async def test_reasoning_is_discarded_and_never_reaches_the_response_or_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private = "PRIVATE-REASONING-TEXT-SHOULD-NEVER-ESCAPE"
    adapter, _ = _adapter(
        lambda request: httpx.Response(200, json=_completed("final answer", reasoning=private))
    )

    with caplog.at_level(logging.DEBUG):
        response = await adapter.complete(_request())

    assert response.text == "final answer"
    assert private not in repr(response)
    assert private not in caplog.text


async def test_multiple_output_text_parts_are_concatenated_in_order() -> None:
    body = _completed()
    body["output"] = [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": '{"intent": '},
                {"type": "output_text", "text": '"unknown"}'},
            ],
        }
    ]
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    assert (await adapter.complete(_request())).text == '{"intent": "unknown"}'


async def test_usage_is_parsed_including_nested_details() -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(
            200,
            json=_completed(
                usage={
                    "input_tokens": 22,
                    "input_tokens_details": {"cached_tokens": 6},
                    "output_tokens": 29,
                    "output_tokens_details": {"reasoning_tokens": 27},
                    "total_tokens": 51,
                }
            ),
        )
    )

    response = await adapter.complete(_request())

    assert response.usage is not None
    assert response.usage.input_tokens == 22
    assert response.usage.cached_tokens == 6
    assert response.usage.output_tokens == 29
    assert response.usage.reasoning_tokens == 27
    assert response.usage.total_tokens == 51


async def test_missing_usage_is_allowed() -> None:
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=_completed()))

    assert (await adapter.complete(_request())).usage is None


async def test_garbage_usage_numbers_do_not_fail_a_successful_call() -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(
            200, json=_completed(usage={"input_tokens": "many", "total_tokens": None})
        )
    )

    response = await adapter.complete(_request())

    assert response.usage is not None and response.usage.total_tokens == 0


async def test_a_response_without_final_text_is_a_protocol_error() -> None:
    body = _completed()
    body["output"] = [{"type": "message", "role": "assistant", "content": []}]
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelProtocolError):
        await adapter.complete(_request())


async def test_a_tool_call_is_refused_because_no_tools_were_offered() -> None:
    body = _completed()
    body["output"] = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "delete_everything",
            "arguments": "{}",
        }
    ]
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelProtocolError) as excinfo:
        await adapter.complete(_request())

    assert "tool call" in str(excinfo.value)


async def test_an_unexpected_output_item_type_is_a_protocol_error() -> None:
    body = _completed()
    body["output"] = [{"type": "audio", "data": "..."}]
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelProtocolError):
        await adapter.complete(_request())


async def test_an_incomplete_response_is_never_treated_as_success() -> None:
    body = _completed()
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelResponseIncomplete) as excinfo:
        await adapter.complete(_request())

    assert "max_output_tokens" in str(excinfo.value)


async def test_a_failed_response_maps_onto_a_provider_neutral_error() -> None:
    body = _completed()
    body["status"] = "failed"
    body["error"] = {"code": "server_error", "message": "generation failed"}
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelUnavailable):
        await adapter.complete(_request())


async def test_an_unknown_status_is_a_protocol_error() -> None:
    body = _completed()
    body["status"] = "in_progress"
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ModelProtocolError):
        await adapter.complete(_request())


async def test_a_malformed_body_is_a_protocol_error() -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(200, content=b"<html>not json</html>")
    )

    with pytest.raises(ModelProtocolError):
        await adapter.complete(_request())


async def test_a_body_without_an_output_list_is_a_protocol_error() -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(200, json={"status": "completed", "id": "x"})
    )

    with pytest.raises(ModelProtocolError):
        await adapter.complete(_request())


# ---------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (400, ModelInvalidRequest),
        (401, ModelAuthenticationError),
        (402, ModelBillingError),
        (422, ModelInvalidRequest),
        (429, ModelRateLimited),
        (500, ModelTransientError),
        (503, ModelUnavailable),
        (504, ModelUnavailable),
        (418, ModelInvalidRequest),
    ),
)
async def test_http_status_codes_map_onto_provider_neutral_errors(
    status: int, expected: type[Exception]
) -> None:
    adapter, _ = _adapter(
        lambda request: httpx.Response(
            status, json={"error": {"message": "provider says no", "code": "x"}}
        )
    )

    with pytest.raises(expected):
        await adapter.complete(_request())


async def test_error_messages_are_short_and_sanitised() -> None:
    long_message = "y" * 5000
    adapter, _ = _adapter(
        lambda request: httpx.Response(400, json={"error": {"message": long_message}})
    )

    with pytest.raises(ModelInvalidRequest) as excinfo:
        await adapter.complete(_request())

    message = str(excinfo.value)
    assert len(message) < 700
    assert API_KEY not in message


async def test_an_error_body_that_is_not_json_still_maps_cleanly() -> None:
    adapter, _ = _adapter(lambda request: httpx.Response(503, content=b"overloaded"))

    with pytest.raises(ModelUnavailable) as excinfo:
        await adapter.complete(_request())

    assert "503" in str(excinfo.value)


# ------------------------------------------------------- transport and lifecycle


async def test_a_timeout_becomes_a_transient_error() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    adapter, _ = _adapter(timeout)

    with pytest.raises(ModelTransientError):
        await adapter.complete(_request())


@pytest.mark.parametrize(
    "error",
    (
        httpx.ConnectError("no route"),
        httpx.RemoteProtocolError("bad framing"),
        httpx.WriteError("broken pipe"),
    ),
)
async def test_network_failures_become_transient_errors(error: httpx.HTTPError) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise error

    adapter, _ = _adapter(failing)

    with pytest.raises(ModelTransientError):
        await adapter.complete(_request())


async def test_the_adapter_never_retries_by_itself() -> None:
    calls = {"count": 0}

    def failing(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("no route")

    adapter, _ = _adapter(failing)

    with pytest.raises(ModelTransientError):
        await adapter.complete(_request())

    assert calls["count"] == 1


async def test_cancellation_propagates_untouched() -> None:
    started = asyncio.Event()

    async def never_answers(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=_completed())

    adapter, _ = _adapter(None, transport=httpx.MockTransport(never_answers))

    task = asyncio.create_task(adapter.complete(_request()))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_adapter_refuses_to_construct_without_a_credential() -> None:
    with pytest.raises(ModelCredentialsMissing) as excinfo:
        DeepSeekAdapter(api_key="   ", model="deepseek-flash", timeout_seconds=10)

    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


async def test_an_injected_client_is_not_closed_by_the_adapter() -> None:
    adapter, _ = _adapter(lambda request: httpx.Response(200, json=_completed()))
    client = adapter._client

    await adapter.aclose()

    assert client.is_closed is False
    await client.aclose()


async def test_a_self_owned_client_is_closed() -> None:
    adapter = DeepSeekAdapter(api_key=API_KEY, model="deepseek-flash", timeout_seconds=10)
    client = adapter._client

    await adapter.aclose()

    assert client.is_closed is True


async def test_no_secret_appears_in_any_error_we_raise(caplog: pytest.LogCaptureFixture) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    adapter, _ = _adapter(failing)

    with caplog.at_level(logging.DEBUG), pytest.raises(ModelTransientError) as excinfo:
        await adapter.complete(_request())

    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value.__cause__)
    assert API_KEY not in caplog.text
