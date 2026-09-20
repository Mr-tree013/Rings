"""`pw interpret` — a preview, never an execution (ADR-0018).

The command prints three things: the interpretation status, a human-readable description of the
draft, and the *equivalent structured CLI command* the user could run themselves. It never runs
that command, and there is deliberately no `--apply` to run it later.

The equivalent command is rendered locally from the typed draft. The model's output is data; it
is never pasted into a shell string. Every user-provided value goes through `shlex.quote`, so a
title like `Lab"; rm -rf ~; echo "` becomes one harmless argument.
"""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.cli_support import console, fail
from assistant.domain.command import (
    CancelTaskDraft,
    ClearDeadlineDraft,
    CommandDraft,
    CompleteTaskDraft,
    CreateCalendarEventDraft,
    CreateTaskDraft,
    RequestWeekPlanDraft,
    SetDeadlineDraft,
)
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    DomainError,
    InvalidAssistantConfig,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
from assistant.domain.interpreter import InterpretationResult, InterpretationStatus

NO_CHANGES_MADE = "No changes were made."


def render_equivalent_command(draft: CommandDraft) -> str:
    """The structured CLI command a user could run to do exactly this, safely quoted."""
    if isinstance(draft, CreateTaskDraft):
        parts = ["pw", "task", "add", draft.title]
        if draft.description is not None:
            parts += ["--description", draft.description]
        if draft.priority is not None:
            parts += ["--priority", draft.priority.value]
        if draft.estimated_minutes is not None:
            parts += ["--estimate", str(draft.estimated_minutes)]
        if draft.deadline is not None:
            parts += ["--deadline", _instant(draft.deadline)]
        return _quote(parts)
    if isinstance(draft, CompleteTaskDraft):
        return _quote(["pw", "task", "done", str(draft.task_id)])
    if isinstance(draft, CancelTaskDraft):
        return _quote(["pw", "task", "cancel", str(draft.task_id)])
    if isinstance(draft, SetDeadlineDraft):
        return _quote(["pw", "task", "deadline", str(draft.task_id), _instant(draft.due_at)])
    if isinstance(draft, ClearDeadlineDraft):
        return _quote(["pw", "task", "deadline", str(draft.task_id), "--clear"])
    if isinstance(draft, CreateCalendarEventDraft):
        parts = ["pw", "calendar", "add", draft.title, "--start", _instant(draft.starts_at)]
        parts += ["--end", _instant(draft.ends_at)]
        if draft.description is not None:
            parts += ["--description", draft.description]
        return _quote(parts)
    if isinstance(draft, RequestWeekPlanDraft):
        parts = ["pw", "plan", "week"]
        if draft.next_week:
            parts.append("--next")
        return _quote(parts)
    raise AssertionError(f"unrendered command draft: {draft!r}")  # pragma: no cover


def render_summary(draft: CommandDraft) -> list[tuple[str, str]]:
    """Human-readable field/value rows for the preview table."""
    if isinstance(draft, CreateTaskDraft):
        rows = [("Action", "Create task"), ("Title", draft.title)]
        if draft.description is not None:
            rows.append(("Description", draft.description))
        rows.append(("Priority", draft.priority.value))
        rows.append(
            (
                "Estimate",
                "-" if draft.estimated_minutes is None else f"{draft.estimated_minutes} minutes",
            )
        )
        rows.append(
            ("Deadline", "-" if draft.deadline is None else _instant(draft.deadline))
        )
        return rows
    if isinstance(draft, CompleteTaskDraft):
        return [("Action", "Complete task"), ("Task", str(draft.task_id))]
    if isinstance(draft, CancelTaskDraft):
        return [("Action", "Cancel task"), ("Task", str(draft.task_id))]
    if isinstance(draft, SetDeadlineDraft):
        return [
            ("Action", "Set deadline"),
            ("Task", str(draft.task_id)),
            ("Due", _instant(draft.due_at)),
        ]
    if isinstance(draft, ClearDeadlineDraft):
        return [("Action", "Clear deadline"), ("Task", str(draft.task_id))]
    if isinstance(draft, CreateCalendarEventDraft):
        rows = [
            ("Action", "Create calendar event"),
            ("Title", draft.title),
        ]
        if draft.description is not None:
            rows.append(("Description", draft.description))
        rows.append(("Starts", _instant(draft.starts_at)))
        rows.append(("Ends", _instant(draft.ends_at)))
        return rows
    if isinstance(draft, RequestWeekPlanDraft):
        return [
            ("Action", "Request weekly plan proposal"),
            ("Week", "next week" if draft.next_week else "this week"),
        ]
    raise AssertionError(f"unrendered command draft: {draft!r}")  # pragma: no cover


def _quote(parts: list[str]) -> str:
    """POSIX-quote every part; nothing raw ever reaches a shell string."""
    return " ".join(shlex.quote(part) for part in parts)


def _instant(value: datetime) -> str:
    """Render an instant as UTC ISO 8601, so the preview is unambiguous."""
    return value.astimezone(UTC).isoformat()


def _print_result(result: InterpretationResult) -> None:
    """Print status, preview and the equivalent command; never execute anything."""
    console.print(f"Interpretation: [bold]{result.status.value.replace('_', ' ')}[/bold]")
    console.print()
    if result.status is InterpretationStatus.READY and result.command is not None:
        table = Table(show_header=False, title_justify="left")
        for label, value in render_summary(result.command):
            table.add_row(label, value)
        console.print(table)
        console.print()
        console.print(f"[bold]{NO_CHANGES_MADE}[/bold]")
        console.print()
        console.print("Equivalent structured command:")
        console.print(render_equivalent_command(result.command))
        return
    if result.status is InterpretationStatus.NEEDS_CLARIFICATION:
        console.print(result.question or "")
        console.print()
        console.print(f"[bold]{NO_CHANGES_MADE}[/bold]")
        return
    console.print(result.reason or "")
    console.print()
    console.print(f"[bold]{NO_CHANGES_MADE}[/bold]")


def _load_config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def interpret(
    text: Annotated[
        str, typer.Argument(help="One natural-language action to interpret (preview only).")
    ],
) -> None:
    """Interpret one natural-language action and print a structured command preview.

    It does not execute the command.

    This sends the request and a bounded list of open task metadata to the configured model
    provider and may incur provider usage charges. Task descriptions, knowledge contents,
    files, notifications and scheduler payloads are not sent.
    """
    config = _load_config_or_fail()
    try:
        result = asyncio.run(_interpret(config, text))
    except ModelNotConfigured as exc:
        fail(str(exc))
    except ModelCredentialsMissing as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    _print_result(result)
    if result.status is InterpretationStatus.UNSUPPORTED:
        raise typer.Exit(code=1)


async def _interpret(config: AssistantConfig, text: str) -> InterpretationResult:
    """Build the interpreter for this one run, use it, and release the HTTP client."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    adapter = bootstrap.model_adapter(config)
    try:
        service = bootstrap.interpreter_service(database, clock, config, model=adapter)
        return await service.interpret(text)
    finally:
        await bootstrap.close_model(adapter)


def register(app: typer.Typer) -> None:
    """Register `pw interpret` on the root app."""
    app.command("interpret")(interpret)


__all__ = [
    "NO_CHANGES_MADE",
    "interpret",
    "register",
    "render_equivalent_command",
    "render_summary",
]
