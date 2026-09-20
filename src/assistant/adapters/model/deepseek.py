"""DeepSeek adapter: the Responses API, and nothing else outside this file (ADR-0017).

Everything provider-specific lives here: the base URL, the request shape, the header, the
output-item walk, the HTTP status mapping, the timeout policy and the fact that reasoning text
is thrown away. The rest of the project only sees `ModelPort` and provider-neutral errors.

Three behaviours are deliberate:

- **no automatic retry.** A connection can fail after the provider already generated (and
  billed) a response; repeating the request behind the caller's back would be invisible
  duplicate work. Failures surface as `ModelTransientError` and the caller decides.
- **reasoning is discarded.** The Responses API may return a `reasoning` item. It is parsed
  only far enough to be skipped: it is never returned, logged or stored.
- **final text only.** `status = completed` plus at least one `output_text` part is required;
  `incomplete`, `failed`, an unexpected tool call or an empty answer are all errors.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

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
from assistant.domain.model import ModelOutputMode, ModelRequest, ModelResponse, ModelUsage

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
"""The provider endpoint. It is a constant of this adapter, not of the project."""

DEEPSEEK_PROVIDER = "deepseek"

_ERROR_BODY_LIMIT = 500
"""How much provider error text may reach a caller: enough to act on, never a whole payload."""

_CONNECT_TIMEOUT_SECONDS = 10.0
_WRITE_TIMEOUT_SECONDS = 30.0
_POOL_TIMEOUT_SECONDS = 10.0


class DeepSeekAdapter:
    """A `ModelPort` implementation over the DeepSeek Responses API."""

    provider = DEEPSEEK_PROVIDER

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        base_url: str = DEEPSEEK_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ModelCredentialsMissing(
                "no DeepSeek credential available; set DEEPSEEK_API_KEY in the environment"
            )
        if not model.strip():
            raise ValueError("model must be a non-empty name")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=min(_CONNECT_TIMEOUT_SECONDS, self._timeout_seconds),
                read=self._timeout_seconds,
                write=min(_WRITE_TIMEOUT_SECONDS, self._timeout_seconds),
                pool=min(_POOL_TIMEOUT_SECONDS, self._timeout_seconds),
            ),
        )

    @property
    def model(self) -> str:
        """The provider model name this adapter asks for."""
        return self._model

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this adapter created it."""
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send one non-streaming request and return its validated-shape final text.

        Raises:
            ModelTransientError: connect/read timeout or transport failure.
            ModelResponseIncomplete: the provider stopped early.
            ModelProtocolError: the answer is not the documented shape.
        """
        payload = self._build_payload(request)
        try:
            response = await self._client.post("/responses", json=payload)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelTransientError(
                f"the provider did not answer within {self._timeout_seconds:g}s"
            ) from exc
        except httpx.TransportError as exc:
            raise ModelTransientError(
                f"the provider could not be reached ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - defensive, httpx is broad
            raise ModelUnavailable(f"the provider call failed ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise _error_for_status(response)
        return self._extract(_decode_body(response))

    # ------------------------------------------------------------------ request

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        """Map a provider-neutral request onto the Responses API body.

        Crucially absent: `tools`, `tool_choice`, `stream`, and any user identifier. The
        project never tells the provider who the user is.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "reasoning": {"effort": request.reasoning_effort.value},
            "max_output_tokens": request.max_output_tokens,
            "stream": False,
            "text": {"format": _output_format(request)},
        }
        if request.instructions is not None:
            payload["instructions"] = request.instructions
        if request.messages:
            payload["input"] = [
                {
                    "type": "message",
                    "role": message.role.value,
                    "content": message.content,
                }
                for message in request.messages
            ]
        return payload

    # ----------------------------------------------------------------- response

    def _extract(self, body: dict[str, Any]) -> ModelResponse:
        status = body.get("status")
        if status == "incomplete":
            reason = body.get("incomplete_details")
            detail = reason.get("reason") if isinstance(reason, dict) else None
            raise ModelResponseIncomplete(
                f"the provider stopped early ({detail or 'incomplete'})"
            )
        if status == "failed":
            raise _error_for_body(body)
        if status != "completed":
            raise ModelProtocolError(f"unexpected provider response status {status!r}")
        output = body.get("output")
        if not isinstance(output, list):
            raise ModelProtocolError("provider response has no output list")
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                raise ModelProtocolError("provider response contains a malformed output item")
            item_type = item.get("type")
            if item_type == "reasoning":
                continue  # parsed only to be discarded: reasoning never leaves the adapter
            if item_type == "message":
                parts.extend(_message_text(item))
                continue
            if item_type in {"function_call", "custom_tool_call"}:
                raise ModelProtocolError(
                    "the provider returned a tool call, but no tools were offered"
                )
            raise ModelProtocolError(f"unexpected output item type {item_type!r}")
        text = "".join(parts)
        if not text.strip():
            raise ModelProtocolError("provider response contains no final text")
        model = body.get("model")
        return ModelResponse(
            text=text,
            model=model if isinstance(model, str) and model else self._model,
            response_id=body.get("id") if isinstance(body.get("id"), str) else None,
            usage=_usage(body.get("usage")),
            provider=DEEPSEEK_PROVIDER,
        )


def _output_format(request: ModelRequest) -> dict[str, Any]:
    """The `text.format` object for this request's output mode."""
    if request.output_mode is ModelOutputMode.JSON_SCHEMA:
        schema = request.json_schema
        if schema is None:  # pragma: no cover - ModelRequest guarantees this
            raise ModelProtocolError("a JSON schema request needs a schema")
        return {"type": "json_schema", "name": schema.name, "schema": schema.schema}
    return {"type": "text"}


def _message_text(item: dict[str, Any]) -> list[str]:
    """Collect the `output_text` parts of one message item, in order."""
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(part["text"])
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    ]


def _usage(raw: object) -> ModelUsage | None:
    """Read token accounting if the provider reported it; metadata never fails a call."""
    if not isinstance(raw, dict):
        return None
    input_details = raw.get("input_tokens_details")
    output_details = raw.get("output_tokens_details")
    return ModelUsage(
        input_tokens=_token(raw.get("input_tokens")),
        output_tokens=_token(raw.get("output_tokens")),
        reasoning_tokens=_token(
            output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
        ),
        cached_tokens=_token(
            input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        ),
        total_tokens=_token(raw.get("total_tokens")),
    )


def _token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _decode_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ModelProtocolError("provider response body is not JSON") from exc
    if not isinstance(body, dict):
        raise ModelProtocolError("provider response body is not a JSON object")
    return body


def _error_for_status(response: httpx.Response) -> Exception:
    """Map a provider status code onto the project's provider-neutral error vocabulary."""
    message = _error_message(_safe_body(response))
    status = response.status_code
    if status == 401:
        return ModelAuthenticationError(
            "the provider rejected the credential (401 authentication failed)"
        )
    if status == 402:
        return ModelBillingError("the provider account has insufficient balance (402)")
    if status == 429:
        return ModelRateLimited(f"the provider is rate limiting this client (429): {message}")
    if status in (400, 422):
        return ModelInvalidRequest(f"the provider rejected the request ({status}): {message}")
    if status == 500:
        return ModelTransientError(f"the provider reported a server error (500): {message}")
    if status == 503:
        return ModelUnavailable(f"the provider is overloaded (503): {message}")
    if status >= 500:
        return ModelUnavailable(f"the provider is unavailable ({status}): {message}")
    return ModelInvalidRequest(f"the provider rejected the request ({status}): {message}")


def _error_for_body(body: dict[str, Any]) -> Exception:
    """Map a 200 response that reports `status = failed` onto the same vocabulary."""
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(error, dict) and error.get("type") == "insufficient_quota":
        return ModelBillingError(f"the provider reported a billing failure: {_error_message(body)}")
    return ModelUnavailable(
        f"the provider reported a failed response ({code or 'unknown'}): {_error_message(body)}"
    )


def _safe_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error_message(body: dict[str, Any] | None) -> str:
    """A short, sanitised description: no prompt, no headers, no credential, no full payload."""
    if not body:
        return "no error detail provided"
    error = body.get("error")
    candidate: object = None
    if isinstance(error, dict):
        candidate = error.get("message") or error.get("code")
    elif isinstance(error, str):
        candidate = error
    if candidate is None:
        candidate = body.get("message")
    if not isinstance(candidate, str) or not candidate.strip():
        return "no error detail provided"
    cleaned = " ".join(candidate.split())
    return cleaned[:_ERROR_BODY_LIMIT]


__all__ = ["DEEPSEEK_BASE_URL", "DEEPSEEK_PROVIDER", "DeepSeekAdapter"]
