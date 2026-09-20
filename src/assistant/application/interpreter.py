"""The interpreter: one natural-language request in, one typed draft out (ADR-0018).

```text
text ──► bounded context ──► ModelPort (JSON schema) ──► local validation ──► CommandDraft
                                                              │
                                                     schema + semantic
                                                     + reference + timezone
```

Nothing in this module can mutate anything. It holds a `StructuredModel` and a read-only
context builder; there is no `TaskService`, no `CalendarService`, no planner and no scheduler
anywhere in its dependency graph, so "the interpreter does not execute" is a property of the
types rather than a promise in a comment.

The model's answer is untrusted input even after schema validation. It is then checked three
more times, deterministically: against the domain rules for the draft it describes, against the
exact task identities that were supplied in the context, and against the timezone policy.
Anything that fails is rejected — never guessed at, corrected, or executed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from assistant.application.interpreter_context import (
    InterpreterContext,
    InterpreterContextBuilder,
)
from assistant.application.interpreter_prompt import INTERPRETER_INSTRUCTIONS
from assistant.application.interpreter_schema import INTERPRETER_SCHEMA_V1
from assistant.application.structured_model import StructuredModel
from assistant.domain.command import (
    CancelTaskDraft,
    ClearDeadlineDraft,
    CommandDraft,
    CompleteTaskDraft,
    CreateCalendarEventDraft,
    CreateTaskDraft,
    RequestWeekPlanDraft,
    SetDeadlineDraft,
    is_time_bearing,
)
from assistant.domain.config import ModelConfig
from assistant.domain.errors import (
    InterpreterInputTooLong,
    InterpreterInvalidReference,
    InterpreterSemanticError,
    InvalidCommandDraft,
)
from assistant.domain.instants import parse_iso_instant
from assistant.domain.interpreter import InterpretationResult, InterpretationStatus
from assistant.domain.model import (
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelRole,
)
from assistant.domain.task import TaskPriority

MAX_INTERPRETER_INPUT_CHARS = 4000
"""Beyond this the request is not an instruction, it is pasted content."""

MISSING_TIMEZONE_QUESTION = (
    "Planning timezone is not configured. Configure [planning].timezone before using "
    "natural-language date or time interpretation."
)
"""The fixed answer when a time-bearing command arrives without a configured timezone."""


class InterpreterService:
    """Turns one natural-language action into one typed, non-executing draft."""

    def __init__(
        self,
        model: StructuredModel,
        context_builder: InterpreterContextBuilder,
        config: ModelConfig,
    ) -> None:
        self._model = model
        self._context_builder = context_builder
        self._config = config

    async def interpret(self, text: str) -> InterpretationResult:
        """Interpret `text`, or explain what is missing.

        Raises:
            InterpreterInputTooLong: the request exceeds the accepted length.
            ModelOutputSchemaViolation: the answer does not match the schema.
            InterpreterSemanticError: a schema-valid answer is not a usable command.
            InterpreterInvalidReference: a referenced task was not in the supplied context.
        """
        cleaned = _validate_input(text)
        context = await self._context_builder.build()
        response = await self._model.complete_json(self._build_request(cleaned, context))
        return parse_interpretation_output(response, context)

    def _build_request(self, text: str, context: InterpreterContext) -> ModelRequest:
        """One USER message holding canonical JSON; instructions carry the fixed rules."""
        payload = {"request": text, "context": context.to_payload()}
        return ModelRequest(
            instructions=INTERPRETER_INSTRUCTIONS,
            messages=(
                ModelMessage(
                    role=ModelRole.USER,
                    content=_canonical_json(payload),
                ),
            ),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=INTERPRETER_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._config.reasoning_effort),
            max_output_tokens=self._config.max_output_tokens,
        )


def _validate_input(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise InterpreterSemanticError("there is nothing to interpret")
    if len(cleaned) > MAX_INTERPRETER_INPUT_CHARS:
        raise InterpreterInputTooLong(
            f"the request is {len(cleaned)} characters; the interpreter accepts at most "
            f"{MAX_INTERPRETER_INPUT_CHARS}"
        )
    return cleaned


def _canonical_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_interpretation_output(
    payload: dict[str, Any], context: InterpreterContext
) -> InterpretationResult:
    """Turn a schema-valid answer into a typed result, or refuse it.

    Pure on purpose: this is the part of interpretation that must be testable without any model
    at all, because it is where every security-relevant decision lives.
    """
    status = payload.get("status")
    if status == InterpretationStatus.NEEDS_CLARIFICATION.value:
        question = _message(payload.get("question"), "question")
        return InterpretationResult.needs_clarification(question)
    if status == InterpretationStatus.UNSUPPORTED.value:
        reason = _message(payload.get("reason"), "reason")
        return InterpretationResult.unsupported(reason)
    if status != InterpretationStatus.READY.value:
        raise InterpreterSemanticError(f"unknown interpretation status {status!r}")

    command = payload.get("command")
    if not isinstance(command, dict):
        raise InterpreterSemanticError("a ready interpretation needs a command object")
    draft = _draft_from_command(command)
    _validate_references(draft, context)
    if is_time_bearing(draft) and context.planning_timezone is None:
        # The model cannot escape the timezone policy by guessing an offset: without a
        # configured zone, the project does not accept interpreted times at all.
        return InterpretationResult.needs_clarification(MISSING_TIMEZONE_QUESTION)
    return InterpretationResult.ready(draft)


def _draft_from_command(command: dict[str, Any]) -> CommandDraft:
    kind = command.get("kind")
    try:
        if kind == "create_task":
            return CreateTaskDraft(
                title=_text(command.get("title"), "title"),
                description=_optional_text(command.get("description")),
                priority=_priority(command.get("priority")),
                estimated_minutes=_optional_int(
                    command.get("estimated_minutes"), "estimated_minutes"
                ),
                deadline=_optional_instant(command.get("deadline"), "deadline"),
            )
        if kind == "complete_task":
            return CompleteTaskDraft(task_id=_task_id(command.get("task_id")))
        if kind == "cancel_task":
            return CancelTaskDraft(task_id=_task_id(command.get("task_id")))
        if kind == "set_deadline":
            return SetDeadlineDraft(
                task_id=_task_id(command.get("task_id")),
                due_at=_instant(command.get("due_at"), "due_at"),
            )
        if kind == "clear_deadline":
            return ClearDeadlineDraft(task_id=_task_id(command.get("task_id")))
        if kind == "create_calendar_event":
            return CreateCalendarEventDraft(
                title=_text(command.get("title"), "title"),
                description=_optional_text(command.get("description")),
                starts_at=_instant(command.get("starts_at"), "starts_at"),
                ends_at=_instant(command.get("ends_at"), "ends_at"),
            )
        if kind == "request_week_plan":
            next_week = command.get("next_week")
            if not isinstance(next_week, bool):
                raise InterpreterSemanticError("next_week must be a boolean")
            return RequestWeekPlanDraft(next_week=next_week)
    except InvalidCommandDraft as exc:
        raise InterpreterSemanticError(str(exc)) from exc
    raise InterpreterSemanticError(f"unsupported command kind {kind!r}")


def _validate_references(draft: CommandDraft, context: InterpreterContext) -> None:
    """Identity must come from the context that was actually supplied to the model."""
    task_id = getattr(draft, "task_id", None)
    if task_id is None:
        return
    if task_id not in context.task_ids:
        raise InterpreterInvalidReference("task", task_id)


def _message(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InterpreterSemanticError(f"{field_name} must be a non-blank string")
    return value.strip()


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise InterpreterSemanticError(f"{field_name} must be a string")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InterpreterSemanticError("description must be a string or null")
    return value


def _priority(value: object) -> TaskPriority:
    if value is None:
        return TaskPriority.NORMAL
    if not isinstance(value, str):
        raise InterpreterSemanticError("priority must be a string")
    try:
        return TaskPriority(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in TaskPriority)
        raise InterpreterSemanticError(f"priority must be one of: {allowed}") from exc


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InterpreterSemanticError(f"{field_name} must be an integer or null")
    return value


def _task_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise InterpreterSemanticError("task_id must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise InterpreterSemanticError(f"task_id is not a UUID: {value!r}") from exc


def _instant(value: object, field_name: str) -> datetime:
    """Parse one model-provided timestamp: ISO 8601, with an offset, or nothing."""
    if not isinstance(value, str):
        raise InterpreterSemanticError(f"{field_name} must be an ISO 8601 string")
    try:
        return parse_iso_instant(value)
    except ValueError as exc:
        raise InterpreterSemanticError(f"{field_name}: {exc}") from exc


def _optional_instant(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _instant(value, field_name)


__all__ = [
    "MAX_INTERPRETER_INPUT_CHARS",
    "MISSING_TIMEZONE_QUESTION",
    "InterpreterService",
    "parse_interpretation_output",
]
