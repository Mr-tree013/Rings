"""Provider-neutral model types: the language the core speaks to any model (ADR-0017).

Nothing here knows about a provider. There is no base URL, no HTTP status, no SDK type and no
provider model name — those belong to an adapter. What the core owns is the *vocabulary* of a
model call: role-tagged messages, instructions, a reasoning budget, an output mode and a
validated response envelope.

Two boundaries are deliberate:

- `ModelResponse` carries final text and accounting metadata only. Provider reasoning is
  discarded inside the adapter: it is never returned, stored, logged or shown.
- Tool calls do not exist. `complete` answers with text; anything that could take an action
  needs a separately designed application boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from assistant.domain.errors import InvalidModelRequest, InvalidModelSchema

_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ModelRole(StrEnum):
    """Who wrote a message. System-level guidance travels in `instructions`, not here."""

    USER = "user"
    ASSISTANT = "assistant"


class ModelReasoningEffort(StrEnum):
    """How much reasoning a model may spend, in provider-neutral terms.

    An adapter maps these onto whatever its provider calls them; the core never learns the
    provider's spelling.
    """

    NONE = "none"
    LOW = "low"
    HIGH = "high"
    MAX = "max"


class ModelOutputMode(StrEnum):
    """What the caller wants back: prose, or JSON that must satisfy a schema."""

    TEXT = "text"
    JSON_SCHEMA = "json_schema"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One turn of text. Multimodal parts are not part of this phase."""

    role: ModelRole
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise InvalidModelRequest("a model message needs non-blank content")


@dataclass(frozen=True, slots=True)
class JsonSchemaOutput:
    """A named JSON Schema the structured response must satisfy.

    Only the provider-neutral shape is checked here. Whether the schema is *valid* as a JSON
    Schema is decided by the structured-output service, because the domain layer stays free of
    third-party libraries (ADR-0017).
    """

    name: str
    schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not _SCHEMA_NAME_PATTERN.match(self.name):
            raise InvalidModelSchema(
                "schema name must match ^[A-Za-z0-9_-]{1,128}$ "
                f"(got {self.name!r})"
            )
        if not isinstance(self.schema, dict):
            raise InvalidModelSchema("a JSON schema must be a JSON object")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One provider-neutral completion request.

    Credentials, base URLs and provider model names are *not* here: they are adapter concerns,
    injected by the composition root.
    """

    messages: tuple[ModelMessage, ...] = ()
    instructions: str | None = None
    output_mode: ModelOutputMode = ModelOutputMode.TEXT
    json_schema: JsonSchemaOutput | None = None
    reasoning_effort: ModelReasoningEffort = ModelReasoningEffort.LOW
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        if not self.messages and not (self.instructions or "").strip():
            raise InvalidModelRequest(
                "a model request needs instructions, messages, or both"
            )
        if self.instructions is not None and not self.instructions.strip():
            raise InvalidModelRequest("instructions must not be blank when provided")
        if self.output_mode is ModelOutputMode.TEXT and self.json_schema is not None:
            raise InvalidModelRequest("a text request must not carry a JSON schema")
        if self.output_mode is ModelOutputMode.JSON_SCHEMA and self.json_schema is None:
            raise InvalidModelRequest("a JSON schema request needs a schema")
        if self.max_output_tokens < 1:
            raise InvalidModelRequest("max_output_tokens must be at least 1")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token accounting as reported by the provider, when it reports it."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
            ("cached_tokens", self.cached_tokens),
            ("total_tokens", self.total_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidModelRequest(f"usage.{name} must be an integer")
            if value < 0:
                raise InvalidModelRequest(f"usage.{name} must not be negative")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A finished model answer: final text, accounting, and nothing else.

    Provider reasoning, raw HTTP payloads and credentials are not representable here, so they
    cannot leak by accident through a `repr`, a log line or an error message.
    """

    text: str
    model: str
    response_id: str | None = None
    usage: ModelUsage | None = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidModelRequest("a model response text must be a string")
        if not self.model.strip():
            raise InvalidModelRequest("a model response needs a model name")


__all__ = [
    "JsonSchemaOutput",
    "ModelMessage",
    "ModelOutputMode",
    "ModelReasoningEffort",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "ModelUsage",
]
