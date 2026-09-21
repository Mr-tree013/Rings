"""One user message in, one typed conversation plan out (ADR-0033 §5-6).

```text
text ──► bounded context ──► ModelPort (JSON schema) ──► local validation ──► ConversationPlan
                                                              │
                                                    schema + arguments + references
                                                    + planning-timezone policy
```

Nothing here can mutate anything: the dependency is a `StructuredModel` and nothing else, so
"the interpreter does not execute" is a property of the types rather than a promise in a comment.

Two deterministic checks run after the schema has been satisfied, and both matter more than the
schema:

* **references.** An existing entity may only be referenced by an id that was in the context this
  turn was given. A hallucinated task id, or one remembered from an earlier turn, is rejected
  before it can name anything real.
* **time.** If any operation's meaning depends on the planning timezone and none is configured,
  the interpreter answers with a clarification question instead of a plan. Nothing is executed, so
  nothing is mutated before the user answers.
"""

from __future__ import annotations

import json
from typing import Any

from assistant.application.conversation_plan_normalizer import (
    describe_issue,
    normalize_wire_plan,
)
from assistant.application.conversation_prompt import (
    CONVERSATION_INTERPRETER_VERSION,
    TREE_INSTRUCTIONS,
)
from assistant.application.conversation_schema import CONVERSATION_SCHEMA_V1
from assistant.application.structured_model import parse_structured_output, validate_json_schema
from assistant.domain.config import ModelConfig
from assistant.domain.conversation_context import (
    ConversationContext,
    ConversationEntityKind,
)
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.conversation_plan import (
    CalendarRecurringEditArguments,
    CalendarRecurringRetireArguments,
    ConversationOperationArguments,
    ConversationOperationType,
    ConversationPlan,
    ConversationPlanMode,
    MailPrepareReplySendArguments,
    MailReplyDraftArguments,
    MailShowArguments,
    MailThreadArguments,
    NotificationReadArguments,
    PlanApplyProposalArguments,
    PlannedOperation,
    TaskClearDeadlineArguments,
    TaskCompleteArguments,
    TaskEditArguments,
    TaskSetDeadlineArguments,
    TaskShowArguments,
    WorkRecordArguments,
    build_arguments,
    requires_planning_timezone,
)
from assistant.domain.errors import (
    ConversationCapabilityUnavailable,
    ConversationInterpretationFailed,
    InvalidConversationPlan,
    ModelOutputNotJson,
    ModelOutputSchemaViolation,
)
from assistant.domain.model import (
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from assistant.ports.model import ModelPort

MISSING_TIMEZONE_QUESTION = (
    "我还不知道按哪个时区理解你说的这个时间。"
    "请在配置里设置 [planning].timezone（例如 Asia/Shanghai），然后再说一次。"
)
"""The fixed answer when a time-bearing request arrives with no planning timezone (ADR-0033 §21)."""


class ConversationInterpreterService:
    """Turns one conversation message into one typed, non-executing plan."""

    version = CONVERSATION_INTERPRETER_VERSION

    def __init__(self, model: ModelPort, config: ModelConfig) -> None:
        validate_json_schema(CONVERSATION_SCHEMA_V1)
        self._model = model
        self._config = config

    async def plan(self, text: str, context: ConversationContext) -> ConversationPlan:
        """Interpret `text`, with one bounded repair attempt before giving up.

        The pipeline is fixed and has no side effect at any step:

        ```text
        complete → JSON → normalize benign variation → closed schema → typed plan
           │                                                    │
           └────────────── one repair call with the issue ──────┘  (never a second)
        ```

        Raises:
            InvalidConversationPlan: the message itself is empty.
            ConversationCapabilityUnavailable: the answer named an operation this build does not
                offer — a refusal, never something to "repair" into a different operation.
            ConversationInterpretationFailed: two answers in a row were unusable.
            Model* errors: the provider failed; nothing was executed.
        """
        cleaned = _validate_input(text)
        request = self._build_request(cleaned, context)
        response = await self._model.complete(request)
        plan, issue = _plan_or_issue(response.text, context)
        if plan is None:
            repaired = await self._model.complete(self._build_repair_request(response.text, issue))
            plan, issue = _plan_or_issue(repaired.text, context)
        if plan is None:
            raise ConversationInterpretationFailed(
                ConversationErrorCode.MODEL_REPAIR_FAILED, issue
            )
        if context.planning_timezone is None and _needs_timezone(plan):
            return ConversationPlan(
                mode=ConversationPlanMode.CLARIFICATION,
                clarification=MISSING_TIMEZONE_QUESTION,
            )
        return plan

    def _build_repair_request(self, answer: str, issue: str | None) -> ModelRequest:
        """One bounded repair request: the same schema, the answer, and a compact issue."""
        payload = {
            "task": "repair",
            "previous_answer": answer[:4000],
            "issue": describe_issue(issue or "the answer did not match the requested schema"),
        }
        return ModelRequest(
            instructions=TREE_INSTRUCTIONS,
            messages=(
                ModelMessage(role=ModelRole.USER, content=_canonical_json(payload)),
            ),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=CONVERSATION_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._config.reasoning_effort),
            max_output_tokens=self._config.max_output_tokens,
        )

    def _build_request(self, text: str, context: ConversationContext) -> ModelRequest:
        """One USER message holding canonical JSON; instructions carry the fixed rules."""
        payload = {"request": text, "context": context.to_payload()}
        return ModelRequest(
            instructions=TREE_INSTRUCTIONS,
            messages=(
                ModelMessage(role=ModelRole.USER, content=_canonical_json(payload)),
            ),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=CONVERSATION_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._config.reasoning_effort),
            max_output_tokens=self._config.max_output_tokens,
        )


def _validate_input(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise InvalidConversationPlan("there is nothing to interpret")
    return cleaned


def _plan_or_issue(
    raw_text: str, context: ConversationContext
) -> tuple[ConversationPlan | None, str | None]:
    """One pass of the pipeline: JSON, benign normalization, schema, typed plan."""
    try:
        decoded = json.loads(raw_text.strip())
    except (ValueError, AttributeError):
        return None, "the answer was not JSON"
    _refuse_unknown_operation_types(json.dumps(decoded))
    normalized, issue = normalize_wire_plan(decoded)
    if normalized is None:
        return None, issue
    try:
        payload = parse_structured_output(_as_response(normalized), CONVERSATION_SCHEMA_V1)
    except ModelOutputNotJson as exc:
        return None, str(exc)
    except ModelOutputSchemaViolation as exc:
        return None, str(exc)
    try:
        return parse_conversation_plan(payload, context), None
    except InvalidConversationPlan as exc:
        return None, str(exc)


def _as_response(payload: dict[str, Any]) -> ModelResponse:
    """Wrap a normalized payload so the existing parser can validate it unchanged."""
    return ModelResponse(
        text=json.dumps(payload, ensure_ascii=False),
        model="normalized-local",
        provider="local",
    )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _needs_timezone(plan: ConversationPlan) -> bool:
    return any(
        requires_planning_timezone(operation.operation_type, operation.arguments)
        for operation in plan.operations
    )


def _refuse_unknown_operation_types(raw_text: str) -> None:
    """Name the operations a model asked for that this build does not have.

    The schema already refuses them — an unknown `type` matches no `oneOf` branch — but a schema
    violation explains itself in validator language, and "you asked me to send an email and I
    cannot" deserves to be said in the user's words. This reads only the `type` fields of an
    untrusted answer, before validation, and turns exactly that case into a capability refusal.
    """
    try:
        decoded = json.loads(raw_text.strip())
    except (ValueError, AttributeError):
        return
    if not isinstance(decoded, dict):
        return
    raw_operations = decoded.get("operations")
    if not isinstance(raw_operations, list):
        return
    known = {member.value for member in ConversationOperationType}
    unknown = sorted(
        {
            entry["type"]
            for entry in raw_operations
            if isinstance(entry, dict)
            and isinstance(entry.get("type"), str)
            and entry["type"] not in known
        }
    )
    if unknown:
        raise ConversationCapabilityUnavailable(", ".join(unknown))


def parse_conversation_plan(
    payload: dict[str, Any], context: ConversationContext
) -> ConversationPlan:
    """Validate one schema-conformant answer into a `ConversationPlan`.

    Raises:
        InvalidConversationPlan: the answer is structurally wrong, names an operation this build
            does not offer, carries arguments the operation does not accept, or references an
            entity that was not in the context.
    """
    mode_value = payload.get("mode")
    if not isinstance(mode_value, str):
        raise InvalidConversationPlan(f"unknown mode: {mode_value!r}")
    try:
        mode = ConversationPlanMode(mode_value)
    except ValueError as exc:
        raise InvalidConversationPlan(f"unknown mode: {mode_value!r}") from exc
    raw_operations = payload.get("operations") or ()
    if not isinstance(raw_operations, (list, tuple)):
        raise InvalidConversationPlan("operations must be a list")
    known_ids = _context_ids(context)
    operations = tuple(_planned_operation(entry, known_ids) for entry in raw_operations)
    return ConversationPlan(
        mode=mode,
        reply=_optional_text(payload.get("reply"), "reply"),
        clarification=_optional_text(payload.get("clarification"), "clarification"),
        operations=operations,
    )


def _planned_operation(
    entry: object, known_ids: dict[ConversationEntityKind, frozenset[str]]
) -> PlannedOperation:
    if not isinstance(entry, dict):
        raise InvalidConversationPlan("each operation must be an object")
    operation_type_value = entry.get("type")
    if not isinstance(operation_type_value, str):
        raise InvalidConversationPlan("an operation needs a string type")
    arguments = build_arguments(operation_type_value, entry.get("arguments"))
    _validate_references(arguments, known_ids)
    return PlannedOperation(
        operation_type=ConversationOperationType(operation_type_value),
        arguments=arguments,
        note=_optional_text(entry.get("note"), "note"),
    )


_REFERENCE_KINDS: dict[ConversationOperationType, ConversationEntityKind] = {
    ConversationOperationType.TASK_SHOW: ConversationEntityKind.TASK,
    ConversationOperationType.TASK_EDIT: ConversationEntityKind.TASK,
    ConversationOperationType.TASK_SET_DEADLINE: ConversationEntityKind.TASK,
    ConversationOperationType.TASK_CLEAR_DEADLINE: ConversationEntityKind.TASK,
    ConversationOperationType.TASK_COMPLETE: ConversationEntityKind.TASK,
    ConversationOperationType.WORK_RECORD: ConversationEntityKind.TASK,
    ConversationOperationType.NOTIFICATION_READ: ConversationEntityKind.NOTIFICATION,
    ConversationOperationType.MAIL_SHOW: ConversationEntityKind.MAIL_MESSAGE,
    ConversationOperationType.MAIL_REPLY_DRAFT: ConversationEntityKind.MAIL_MESSAGE,
    ConversationOperationType.MAIL_THREAD: ConversationEntityKind.MAIL_THREAD,
    ConversationOperationType.CALENDAR_RECURRING_EDIT: (
        ConversationEntityKind.RECURRING_CALENDAR_RULE
    ),
    ConversationOperationType.CALENDAR_RECURRING_RETIRE: (
        ConversationEntityKind.RECURRING_CALENDAR_RULE
    ),
}
"""Which entity kind each operation may reference, and what it must have seen to do so."""


def _validate_references(
    arguments: ConversationOperationArguments,
    known_ids: dict[ConversationEntityKind, frozenset[str]],
) -> None:
    kind = _REFERENCE_KINDS.get(arguments.operation_type)
    if kind is not None:
        reference = _referenced_id(arguments)
        if str(reference) not in known_ids.get(kind, frozenset()):
            raise InvalidConversationPlan(
                f"{arguments.operation_type.value} referenced an entity that was not in the "
                "context it was given"
            )
    if arguments.operation_type is ConversationOperationType.PLAN_APPLY_PROPOSAL:
        proposal_id = (
            arguments.proposal_id
            if isinstance(arguments, PlanApplyProposalArguments)
            else None
        )
        if proposal_id is not None and proposal_id not in known_ids.get(
            ConversationEntityKind.PROPOSAL, frozenset()
        ):
            raise InvalidConversationPlan(
                "plan.apply_proposal referenced a proposal that was not in the context it was "
                "given"
            )
    if arguments.operation_type is ConversationOperationType.MAIL_PREPARE_REPLY_SEND and isinstance(
        arguments, MailPrepareReplySendArguments
    ):
        # A turn that drafts and prepares in one go cannot know the draft's id yet, so it names
        # the message instead; either way the identity must have come from the context.
        if arguments.draft_id is not None:
            allowed = known_ids.get(ConversationEntityKind.MAIL_DRAFT, frozenset())
            reference = arguments.draft_id
        else:
            allowed = known_ids.get(ConversationEntityKind.MAIL_MESSAGE, frozenset())
            reference = arguments.message_id
        if reference not in allowed:
            raise InvalidConversationPlan(
                "mail.prepare_reply_send referenced a draft or message that was not in the "
                "context it was given"
            )


def _referenced_id(arguments: ConversationOperationArguments) -> object:
    """The identity an operation is allowed to mention, read through its own type."""
    if isinstance(arguments, (TaskShowArguments, TaskEditArguments, TaskCompleteArguments)):
        return arguments.task_id
    if isinstance(arguments, (TaskSetDeadlineArguments, TaskClearDeadlineArguments)):
        return arguments.task_id
    if isinstance(arguments, WorkRecordArguments):
        return arguments.task_id
    if isinstance(arguments, NotificationReadArguments):
        return arguments.notification_id
    if isinstance(arguments, (MailShowArguments, MailReplyDraftArguments)):
        return arguments.message_id
    if isinstance(arguments, MailThreadArguments):
        return arguments.thread_id
    if isinstance(arguments, MailPrepareReplySendArguments):
        return arguments.draft_id or arguments.message_id
    if isinstance(arguments, (CalendarRecurringEditArguments, CalendarRecurringRetireArguments)):
        return arguments.rule_id
    return None


def _context_ids(
    context: ConversationContext,
) -> dict[ConversationEntityKind, frozenset[str]]:
    collected: dict[ConversationEntityKind, set[str]] = {}
    for entity in context.entities:
        collected.setdefault(entity.kind, set()).add(entity.id)
    return {kind: frozenset(ids) for kind, ids in collected.items()}


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidConversationPlan(f"{field_name} must be a string or null")
    cleaned = value.strip()
    return cleaned or None


__all__ = [
    "MISSING_TIMEZONE_QUESTION",
    "ConversationInterpreterService",
    "parse_conversation_plan",
]
