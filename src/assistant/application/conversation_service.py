"""The Tree Conversation Runtime: the deterministic half of a conversation turn (ADR-0033 §14).

```text
user text ──► durable message ──► bounded context ──► typed plan ──► capabilities
                                                          │              │
                                              registry decides     handlers execute
                                                          │              │
                                            WAITING_CONFIRMATION ◄── APPLYING ──► APPLIED
```

Everything the model is *not* allowed to decide lives here: which operation is permitted, whether
it needs a confirmation, when it is executed, what the user is told happened, and what happens if
the process dies in the middle. The model's contribution is one thing — a typed plan — and its
`reply` is deliberately ignored for an operations turn, because the runtime writes the result.

The service depends on the repository port, the interpreter port, the capability registry and a
clock. It imports no adapter, no SQLite, no SMTP and no eHall code, which is what
`tests/unit/test_conversation_architecture.py` enforces.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from assistant.application import conversation_render as render
from assistant.application.conversation_capabilities.registry import (
    ConfirmationPolicy,
    ConversationCapabilityRegistry,
    OperationResult,
)
from assistant.application.conversation_context import ConversationContextBuilder
from assistant.application.conversation_prompt import CONFIRM_PHRASES, REJECT_PHRASES
from assistant.domain.conversation import (
    CONFIRMATION_TTL_MINUTES,
    ConversationMessage,
    ConversationMessageId,
    ConversationMessageRole,
    ConversationOperation,
    ConversationOperationStatus,
    ConversationThread,
    ConversationThreadId,
    ConversationThreadStatus,
    ConversationTurn,
    ConversationTurnStatus,
)
from assistant.domain.conversation_plan import (
    ConversationOperationArguments,
    ConversationOperationType,
    ConversationPlan,
    ConversationPlanMode,
    PlannedOperation,
)
from assistant.domain.errors import (
    ConversationCapabilityUnavailable,
    ConversationThreadNotFound,
    DomainError,
    InvalidConversationThread,
)
from assistant.ports.clock import Clock
from assistant.ports.conversation_interpreter import ConversationInterpreter
from assistant.ports.conversation_repository import ConversationRepository

CONFIRMATION_INTERPRETER_VERSION = "deterministic-confirmation-v1"
"""Recorded on a turn the confirmation vocabulary answered, so no model call is implied."""

CONFIRMATION_INTENT_CONFIRM = "confirm"
CONFIRMATION_INTENT_REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ConversationReply:
    """What one `send` produced, for the caller to print."""

    thread_id: str
    turn_id: str
    text: str
    status: ConversationTurnStatus
    waiting_for_confirmation: bool = False
    operation_types: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()


class ConversationService:
    """One conversation turn: interpret, decide, execute, record."""

    def __init__(
        self,
        *,
        repository: ConversationRepository,
        interpreter: ConversationInterpreter,
        capabilities: ConversationCapabilityRegistry,
        context_builder: ConversationContextBuilder,
        clock: Clock,
        planning_timezone: str | None = None,
        confirmation_ttl: timedelta | None = None,
    ) -> None:
        self._repository = repository
        self._interpreter = interpreter
        self._capabilities = capabilities
        self._context_builder = context_builder
        self._clock = clock
        self._planning_timezone = planning_timezone
        self._confirmation_ttl = confirmation_ttl or timedelta(
            minutes=CONFIRMATION_TTL_MINUTES
        )

    # ------------------------------------------------------------------ session surface

    async def start_thread(self, *, title: str | None = None) -> ConversationThread:
        """Start a new ACTIVE thread."""
        now = self._clock.now()
        thread = ConversationThread(title=title, created_at=now, updated_at=now)
        return await self._repository.add_thread(thread)

    async def active_thread(self) -> ConversationThread | None:
        """The most recently updated ACTIVE thread, if there is one."""
        return await self._repository.latest_active_thread()

    async def resume_or_start(self, *, title: str | None = None) -> tuple[ConversationThread, bool]:
        """Resume the ACTIVE thread, or start one. The flag says whether it was resumed."""
        existing = await self._repository.latest_active_thread()
        if existing is not None:
            return existing, True
        return await self.start_thread(title=title), False

    async def list_threads(self, *, limit: int = 20) -> list[ConversationThread]:
        """Recent threads, newest first."""
        return await self._repository.list_threads(limit=limit)

    async def reopen_thread(self, reference: str) -> ConversationThread:
        """Look a thread up by id or unique id prefix.

        Raises:
            ConversationThreadNotFound: nothing matches.
            InvalidConversationThread: the prefix is ambiguous.
        """
        cleaned = reference.strip().lower()
        if not cleaned:
            raise ConversationThreadNotFound(reference)
        threads = await self._repository.list_threads(limit=200)
        exact = [thread for thread in threads if str(thread.id) == cleaned]
        if exact:
            return exact[0]
        matches = [thread for thread in threads if str(thread.id).startswith(cleaned)]
        if not matches:
            raise ConversationThreadNotFound(reference)
        if len(matches) > 1:
            raise InvalidConversationThread(f"{reference} matches several threads")
        return matches[0]

    async def archive_thread(self, thread_id: ConversationThreadId) -> ConversationThread:
        """Archive a thread: history stays, it is no longer resumed by default."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None:
            raise ConversationThreadNotFound(thread_id)
        now = self._clock.now()
        archived = ConversationThread(
            id=thread.id,
            title=thread.title,
            status=ConversationThreadStatus.ARCHIVED,
            created_at=thread.created_at,
            updated_at=now,
            archived_at=now,
        )
        return await self._repository.update_thread(archived)

    async def pending_confirmation(
        self, thread_id: ConversationThreadId
    ) -> ConversationOperation | None:
        """The one operation waiting for a yes, or `None`. More than one is not a guess."""
        pending = await self._repository.operations_waiting_for_confirmation(thread_id)
        return pending[0] if len(pending) == 1 else None

    async def pending_confirmation_count(self, thread_id: ConversationThreadId) -> int:
        """How many operations are waiting for a yes."""
        return len(await self._repository.operations_waiting_for_confirmation(thread_id))

    async def recover_interrupted(self) -> tuple[str, ...]:
        """Apply the crash fence: `APPLYING` becomes `UNKNOWN_LOCAL`, and is never replayed.

        Called when a session starts. It returns the notices the caller should show, because an
        interrupted local write is something the user has to know about.
        """
        applying = await self._repository.list_operations_by_status(
            ConversationOperationStatus.APPLYING
        )
        if not applying:
            return ()
        now = self._clock.now()
        for operation in applying:
            resolved = _replace_operation(
                operation, status=ConversationOperationStatus.UNKNOWN_LOCAL, at=now
            )
            await self._repository.update_operation(resolved)
            turn = await self._repository.get_turn(operation.turn_id)
            if turn is not None and turn.status not in (
                ConversationTurnStatus.COMPLETED,
                ConversationTurnStatus.FAILED,
                ConversationTurnStatus.INTERRUPTED,
            ):
                await self._repository.update_turn(
                    _finish_turn(turn, ConversationTurnStatus.INTERRUPTED, at=now)
                )
        return (render.render_unknown_local(),)

    # ------------------------------------------------------------------------- the turn

    async def send(self, thread_id: ConversationThreadId, text: str) -> ConversationReply:
        """Record one user message and do whatever the runtime is allowed to do about it."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None:
            raise ConversationThreadNotFound(thread_id)
        if thread.status is not ConversationThreadStatus.ACTIVE:
            raise InvalidConversationThread("this conversation is archived")
        now = self._clock.now()
        user_message = await self._repository.add_message(
            ConversationMessage(
                thread_id=thread.id,
                role=ConversationMessageRole.USER,
                text=text,
                created_at=now,
            )
        )
        pending = await self._repository.operations_waiting_for_confirmation(thread.id)
        intent = _confirmation_intent(text)
        if pending and intent is not None:
            return await self._answer_confirmation(thread, user_message.id, pending, intent, text)
        if pending:
            # Say what is waiting, and keep waiting: a real request still gets interpreted below.
            await self._expire_stale(thread.id, pending)
        return await self._interpret_and_run(thread, user_message.id, text)

    async def _interpret_and_run(
        self, thread: ConversationThread, user_message_id: ConversationMessageId, text: str
    ) -> ConversationReply:
        context = await self._context_builder.build(
            thread.id,
            confirmation_pending=bool(
                await self._repository.operations_waiting_for_confirmation(thread.id)
            ),
        )
        now = self._clock.now()
        turn = await self._repository.add_turn(
            ConversationTurn(
                thread_id=thread.id,
                user_message_id=user_message_id,
                interpreter_version=self._interpreter.version,
                context_fingerprint=_fingerprint(context.to_json()),
                created_at=now,
            )
        )
        try:
            plan = await self._interpreter.plan(text, context)
        except ConversationCapabilityUnavailable as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_unsupported(str(exc).split(", ")),
            )
        except DomainError as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_interpretation_refused(str(exc)),
            )
        if plan.mode is not ConversationPlanMode.OPERATIONS:
            spoken = (
                plan.reply
                if plan.mode is ConversationPlanMode.DIRECT_REPLY
                else plan.clarification
            )
            return await self._finish(
                thread, turn, ConversationTurnStatus.COMPLETED, (spoken or "").strip()
            )
        return await self._run_plan(thread, turn, plan)

    async def _run_plan(
        self, thread: ConversationThread, turn: ConversationTurn, plan: ConversationPlan
    ) -> ConversationReply:
        unsupported = [
            operation.operation_type.value
            for operation in plan.operations
            if self._capabilities.get(operation.operation_type) is None
        ]
        if unsupported:
            for ordinal, operation in enumerate(plan.operations):
                await self._store_operation(turn, ordinal, operation, status=(
                    ConversationOperationStatus.REJECTED
                ))
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_unsupported(unsupported),
            )

        results: list[OperationResult] = []
        operation_types: list[str] = []
        waiting: ConversationOperation | None = None
        offer_in_text = False
        for ordinal, planned in enumerate(plan.operations):
            capability = self._capabilities.require(planned.operation_type)
            if capability.policy is ConfirmationPolicy.CONFIRM_LOCAL:
                stored = await self._store_operation(
                    turn,
                    ordinal,
                    planned,
                    status=ConversationOperationStatus.WAITING_CONFIRMATION,
                    expires_at=self._clock.now() + self._confirmation_ttl,
                )
                waiting = stored
                operation_types.append(planned.operation_type.value)
                break
            outcome = await self._execute(turn, ordinal, planned)
            operation_types.append(planned.operation_type.value)
            if outcome.failure is not None:
                results_text = "\n".join(
                    [render.render_results(results, timezone=self._planning_timezone)]
                    if results
                    else []
                )
                failure = render.render_failure(planned.operation_type.value, outcome.failure)
                return await self._finish(
                    thread,
                    turn,
                    ConversationTurnStatus.FAILED,
                    "\n".join(part for part in (results_text, failure) if part),
                    operation_types=tuple(operation_types),
                )
            if outcome.result is not None:
                results.append(outcome.result)
            if (
                planned.operation_type is ConversationOperationType.PLAN_PROPOSE_WEEK
                and outcome.result is not None
                and outcome.result.ref is not None
                and not any(
                    other.operation_type is ConversationOperationType.PLAN_APPLY_PROPOSAL
                    for other in plan.operations
                )
            ):
                # Creating a proposal is immediately followed by exactly one pending offer to
                # apply it. It is still CONFIRM_LOCAL: nothing is applied until the user says yes.
                waiting = await self._store_operation(
                    turn,
                    ordinal + 1,
                    PlannedOperation(
                        operation_type=ConversationOperationType.PLAN_APPLY_PROPOSAL,
                        arguments=_apply_arguments(outcome.result.ref),
                    ),
                    status=ConversationOperationStatus.WAITING_CONFIRMATION,
                    expires_at=self._clock.now() + self._confirmation_ttl,
                )
                operation_types.append(ConversationOperationType.PLAN_APPLY_PROPOSAL.value)
                offer_in_text = True
                break

        text_out = render.render_results(results, timezone=self._planning_timezone)
        if waiting is not None:
            if not offer_in_text:
                text_out = "\n".join(
                    part
                    for part in (
                        text_out,
                        render.render_confirmation_request(waiting.operation_type.value),
                    )
                    if part
                )
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.WAITING_CONFIRMATION,
                text_out,
                operation_types=tuple(operation_types),
                waiting=True,
            )
        return await self._finish(
            thread,
            turn,
            ConversationTurnStatus.COMPLETED,
            text_out,
            operation_types=tuple(operation_types),
        )

    async def _answer_confirmation(
        self,
        thread: ConversationThread,
        user_message_id: ConversationMessageId,
        pending: Sequence[ConversationOperation],
        intent: str,
        text: str,
    ) -> ConversationReply:
        context = await self._context_builder.build(thread.id, confirmation_pending=True)
        now = self._clock.now()
        turn = await self._repository.add_turn(
            ConversationTurn(
                thread_id=thread.id,
                user_message_id=user_message_id,
                interpreter_version=CONFIRMATION_INTERPRETER_VERSION,
                context_fingerprint=_fingerprint(context.to_json()),
                created_at=now,
            )
        )
        if len(pending) > 1:
            listing = [
                f"- {operation.operation_type.value}（{str(operation.id)[:8]}）"
                for operation in pending
            ]
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.COMPLETED,
                render.render_multiple_pending(listing),
            )
        operation = pending[0]
        if _is_expired(operation, now):
            await self._repository.update_operation(
                _replace_operation(
                    operation, status=ConversationOperationStatus.REJECTED, at=now
                )
            )
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.COMPLETED,
                render.render_confirmation_expired(),
            )
        if intent == CONFIRMATION_INTENT_REJECT:
            await self._repository.update_operation(
                _replace_operation(
                    operation, status=ConversationOperationStatus.REJECTED, at=now
                )
            )
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.COMPLETED,
                render.render_confirmation_rejected(),
            )
        outcome = await self._execute_existing(turn, operation)
        if outcome.failure is not None:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_failure(operation.operation_type.value, outcome.failure),
            )
        rendered = (
            "" if outcome.result is None else render.render_result(
                outcome.result, timezone=self._planning_timezone
            )
        )
        return await self._finish(
            thread,
            turn,
            ConversationTurnStatus.COMPLETED,
            rendered,
            operation_types=(operation.operation_type.value,),
        )

    # ------------------------------------------------------------------- execution fence

    async def _store_operation(
        self,
        turn: ConversationTurn,
        ordinal: int,
        planned: PlannedOperation,
        *,
        status: ConversationOperationStatus,
        expires_at: datetime | None = None,
    ) -> ConversationOperation:
        now = self._clock.now()
        operation = ConversationOperation(
            turn_id=turn.id,
            ordinal=ordinal,
            operation_type=planned.operation_type,
            arguments=planned.arguments,
            operation_fingerprint=planned.fingerprint,
            status=status,
            confirmation_expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        return await self._repository.add_operation(operation)

    async def _execute(
        self, turn: ConversationTurn, ordinal: int, planned: PlannedOperation
    ) -> _Execution:
        stored = await self._store_operation(
            turn, ordinal, planned, status=ConversationOperationStatus.PROPOSED
        )
        return await self._execute_existing(turn, stored)

    async def _execute_existing(
        self, turn: ConversationTurn, operation: ConversationOperation
    ) -> _Execution:
        """Persist APPLYING, call the handler, then APPLIED or FAILED. Never retried by itself."""
        now = self._clock.now()
        applying = _replace_operation(
            operation, status=ConversationOperationStatus.APPLYING, at=now
        )
        await self._repository.update_operation(applying)
        capability = self._capabilities.require(applying.operation_type)
        try:
            result = await capability.handler(applying.arguments)
        except DomainError as exc:
            failed = _replace_operation(
                applying, status=ConversationOperationStatus.FAILED, at=self._clock.now()
            )
            await self._repository.update_operation(failed)
            return _Execution(result=None, failure=str(exc))
        applied = _replace_operation(
            applying,
            status=ConversationOperationStatus.APPLIED,
            at=self._clock.now(),
            result_kind=result.kind,
            result_ref=result.ref,
        )
        await self._repository.update_operation(applied)
        return _Execution(result=result, failure=None)

    async def _expire_stale(
        self, thread_id: ConversationThreadId, pending: Sequence[ConversationOperation]
    ) -> None:
        now = self._clock.now()
        for operation in pending:
            if _is_expired(operation, now):
                await self._repository.update_operation(
                    _replace_operation(
                        operation, status=ConversationOperationStatus.REJECTED, at=now
                    )
                )

    # -------------------------------------------------------------------------- finishing

    async def _finish(
        self,
        thread: ConversationThread,
        turn: ConversationTurn,
        status: ConversationTurnStatus,
        text: str,
        *,
        operation_types: tuple[str, ...] = (),
        waiting: bool = False,
    ) -> ConversationReply:
        now = self._clock.now()
        assistant = await self._repository.add_message(
            ConversationMessage(
                thread_id=thread.id,
                role=ConversationMessageRole.ASSISTANT,
                text=text or render.render_empty_turn(),
                created_at=now,
            )
        )
        finished = ConversationTurn(
            id=turn.id,
            thread_id=turn.thread_id,
            user_message_id=turn.user_message_id,
            assistant_message_id=assistant.id,
            interpreter_version=turn.interpreter_version,
            context_fingerprint=turn.context_fingerprint,
            status=status,
            created_at=turn.created_at,
            completed_at=None if status is ConversationTurnStatus.WAITING_CONFIRMATION else now,
        )
        await self._repository.update_turn(finished)
        await self._repository.update_thread(
            ConversationThread(
                id=thread.id,
                title=thread.title,
                status=thread.status,
                created_at=thread.created_at,
                updated_at=now,
                archived_at=thread.archived_at,
            )
        )
        return ConversationReply(
            thread_id=str(thread.id),
            turn_id=str(turn.id),
            text=assistant.text,
            status=finished.status,
            waiting_for_confirmation=waiting,
            operation_types=operation_types,
        )


@dataclass(frozen=True, slots=True)
class _Execution:
    """The outcome of one handler call."""

    result: OperationResult | None
    failure: str | None


def _apply_arguments(proposal_id: str) -> ConversationOperationArguments:
    from assistant.domain.conversation_plan import PlanApplyProposalArguments

    return PlanApplyProposalArguments(proposal_id=proposal_id)


def _replace_operation(
    operation: ConversationOperation,
    *,
    status: ConversationOperationStatus,
    at: datetime,
    result_kind: str | None = None,
    result_ref: str | None = None,
) -> ConversationOperation:
    return ConversationOperation(
        id=operation.id,
        turn_id=operation.turn_id,
        ordinal=operation.ordinal,
        operation_type=operation.operation_type,
        arguments=operation.arguments,
        operation_fingerprint=operation.operation_fingerprint,
        status=status,
        result_kind=result_kind,
        result_ref=result_ref,
        confirmation_expires_at=(
            operation.confirmation_expires_at
            if status is ConversationOperationStatus.WAITING_CONFIRMATION
            else None
        ),
        created_at=operation.created_at,
        updated_at=at,
    )


def _finish_turn(
    turn: ConversationTurn, status: ConversationTurnStatus, *, at: datetime
) -> ConversationTurn:
    return ConversationTurn(
        id=turn.id,
        thread_id=turn.thread_id,
        user_message_id=turn.user_message_id,
        assistant_message_id=turn.assistant_message_id,
        interpreter_version=turn.interpreter_version,
        context_fingerprint=turn.context_fingerprint,
        status=status,
        created_at=turn.created_at,
        completed_at=at,
    )


def _confirmation_intent(text: str) -> str | None:
    """The deterministic yes/no vocabulary. No model is consulted (ADR-0033 §12)."""
    cleaned = text.strip().lower().rstrip("。.!！")
    if cleaned in CONFIRM_PHRASES:
        return CONFIRMATION_INTENT_CONFIRM
    if cleaned in REJECT_PHRASES:
        return CONFIRMATION_INTENT_REJECT
    return None


def _is_expired(operation: ConversationOperation, now: datetime) -> bool:
    expires_at = operation.confirmation_expires_at
    return expires_at is not None and expires_at <= now


def _fingerprint(context_json: str) -> str:
    return hashlib.sha256(context_json.encode("utf-8")).hexdigest()


__all__ = [
    "CONFIRMATION_INTERPRETER_VERSION",
    "ConversationReply",
    "ConversationService",
]
