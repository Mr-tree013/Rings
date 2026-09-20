"""`pw model` — the model boundary's status and its one live smoke test (ADR-0017).

`pw model status` is pure inspection: it reports the configured provider, model name and
bounds, and whether a credential is present in the environment, without touching the network
and without ever printing the credential.

`pw model test` is the **only** command in this phase that makes a real provider call. It asks
for a tiny structured object and validates the answer locally, which is exactly the contract
Phase 4B's interpreter will rely on. Its help text says so, because the call may cost money.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.structured_model import parse_structured_output
from assistant.cli_support import console, fail
from assistant.domain.config import AssistantConfig, ModelConfig
from assistant.domain.errors import (
    DomainError,
    InvalidAssistantConfig,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
from assistant.domain.model import (
    JsonSchemaOutput,
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelResponse,
    ModelRole,
)

SMOKE_TEST_SCHEMA = JsonSchemaOutput(
    name="model_smoke_test",
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean"},
            "message": {"type": "string"},
        },
        "required": ["ok", "message"],
    },
)
"""The smallest possible structured request: two fields, no room for interpretation."""

SMOKE_TEST_INSTRUCTIONS = (
    "You are answering a connectivity check. Reply with a JSON object that has "
    '"ok" set to true and a one-sentence "message".'
)

model_app = typer.Typer(
    help="Model boundary status and a live structured-output smoke test.",
    no_args_is_help=True,
)


def _load_config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


@model_app.command("status")
def model_status() -> None:
    """Show the configured model boundary. This never contacts the provider."""
    config = _load_config_or_fail()
    try:
        settings = bootstrap.require_model_config(config)
    except ModelNotConfigured:
        console.print("Model not configured.")
        return

    table = Table(title="model status", show_header=False, title_justify="left")
    table.add_row("Provider", settings.provider)
    table.add_row("Model", settings.model)
    table.add_row("Reasoning effort", settings.reasoning_effort)
    table.add_row("Max output tokens", str(settings.max_output_tokens))
    table.add_row("Timeout", f"{settings.timeout_seconds}s")
    table.add_row(
        "API key",
        "present" if bootstrap.model_api_key() is not None else "[yellow]missing[/yellow]",
    )
    console.print(table)
    console.print("Natural-language interpretation is not implemented yet.")


@model_app.command("test")
def model_test(
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", help="Override the configured timeout, in seconds."),
    ] = None,
) -> None:
    """Send one small live request and validate its structured answer.

    Sends a small live API request and may incur provider usage charges.
    """
    config = _load_config_or_fail()
    try:
        settings = bootstrap.require_model_config(config)
    except ModelNotConfigured:
        console.print("Model not configured.")
        raise typer.Exit(code=1) from None
    if timeout is not None:
        settings = _with_timeout(settings, timeout)
    try:
        response, payload = asyncio.run(_smoke_test(config, settings))
    except ModelCredentialsMissing as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))

    table = Table(title="model test", show_header=False, title_justify="left")
    table.add_row("Provider", settings.provider)
    table.add_row("Model", response.model)
    table.add_row("Structured output", "[green]OK[/green]")
    if response.response_id is not None:
        table.add_row("Response id", response.response_id)
    table.add_row("Tokens", _token_summary(response))
    table.add_row("Message", str(payload["message"]))
    console.print(table)


def _with_timeout(settings: ModelConfig, timeout: int) -> ModelConfig:
    try:
        return replace(settings, timeout_seconds=timeout)
    except InvalidAssistantConfig as exc:
        fail(f"invalid --timeout: {exc}")


async def _smoke_test(
    config: AssistantConfig, settings: ModelConfig
) -> tuple[ModelResponse, dict[str, Any]]:
    """Run the smoke test once through the configured adapter, then close it."""
    adapter = bootstrap.model_adapter(config)
    request = ModelRequest(
        instructions=SMOKE_TEST_INSTRUCTIONS,
        messages=(ModelMessage(role=ModelRole.USER, content="Connectivity check."),),
        output_mode=ModelOutputMode.JSON_SCHEMA,
        json_schema=SMOKE_TEST_SCHEMA,
        reasoning_effort=ModelReasoningEffort(settings.reasoning_effort),
        max_output_tokens=settings.max_output_tokens,
    )
    try:
        response = await adapter.complete(request)
        payload = parse_structured_output(response, SMOKE_TEST_SCHEMA)
    finally:
        await bootstrap.close_model(adapter)
    return response, payload


def _token_summary(response: ModelResponse) -> str:
    usage = response.usage
    if usage is None:
        return "not reported"
    return (
        f"input {usage.input_tokens}, output {usage.output_tokens}, "
        f"reasoning {usage.reasoning_tokens}, cached {usage.cached_tokens}, "
        f"total {usage.total_tokens}"
    )


def register(app: typer.Typer) -> None:
    """Register the model commands on the root app."""
    app.add_typer(model_app, name="model")


__all__ = ["SMOKE_TEST_INSTRUCTIONS", "SMOKE_TEST_SCHEMA", "model_app", "register"]
