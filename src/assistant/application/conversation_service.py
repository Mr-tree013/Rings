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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from assistant.application import conversation_render as render
from assistant.application.conversation_capabilities.registry import (
    ConfirmationPolicy,
    ConversationCapabilityRegistry,
    OperationResult,
    PreflightContext,
)
from assistant.application.conversation_context import ConversationContextBuilder
from assistant.application.conversation_external_review import (
    ConfirmationOutcome,
    ConversationExternalReviewService,
    PreparedReview,
)
from assistant.application.conversation_prompt import CONFIRM_PHRASES, REJECT_PHRASES
from assistant.application.conversation_recurring_intent import (
    declares_mutation,
    unsupported_recurrence,
)
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
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.conversation_plan import (
    CalendarRecurringCreateWeeklyArguments,
    ConversationOperationArguments,
    ConversationOperationType,
    ConversationPlan,
    ConversationPlanMode,
    PlannedOperation,
)
from assistant.domain.conversation_review import (
    ConversationExternalReview,
    ConversationExternalReviewStatus,
    matches_send_cancellation,
    matches_send_confirmation,
)
from assistant.domain.errors import (
    ConversationCapabilityUnavailable,
    ConversationInterpretationFailed,
    ConversationThreadNotFound,
    DomainError,
    InvalidConversationPlan,
    InvalidConversationThread,
    ModelAuthenticationError,
    ModelBillingError,
    ModelCredentialsMissing,
    ModelNotConfigured,
    ModelOutputNotJson,
    ModelOutputSchemaViolation,
    ModelRateLimited,
    ModelTransientError,
    ModelUnavailable,
)
from assistant.ports.clock import Clock
from assistant.ports.conversation_interpreter import ConversationInterpreter
from assistant.ports.conversation_repository import ConversationRepository

CONFIRMATION_INTERPRETER_VERSION = "deterministic-confirmation-v1"
"""Recorded on a turn the confirmation vocabulary answered, so no model call is implied."""

EXTERNAL_CONFIRMATION_INTERPRETER_VERSION = "deterministic-external-confirmation-v1"
"""Recorded on a turn that settled a reviewed external action without a model call."""

CONFIRMATION_INTENT_CONFIRM = "confirm"
CONFIRMATION_INTENT_REJECT = "reject"

_ACTIVITY_HINTS = {
    ConversationOperationType.MAIL_SYNC: "正在查看邮箱……",
    ConversationOperationType.KNOWLEDGE_ASK: "正在查资料……",
}
"""Short, non-persisted hints for the operations that visibly take time (ADR-0035 §26)."""


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
    error_code: ConversationErrorCode | None = None
    debug_detail: str | None = None


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
        external: ConversationExternalReviewService | None = None,
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
        self._external = external

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

    async def pending_external_preview(self, thread_id: ConversationThreadId) -> str | None:
        """A waiting reviewed send, re-rendered from its immutable payload (ADR-0034 §17).

        Called when a session starts. It never executes anything: the point is that a human sees
        the exact bytes again before deciding.
        """
        if self._external is None:
            return None
        await self._external.expire_due(thread_id)
        reviews = await self._external.waiting(thread_id)
        if not reviews:
            return None
        payload = await self._external.payload_of(reviews[0])
        if payload is None:
            return render.render_review_stale()
        return "\n".join(
            (render.render_review_pending_notice(), render.render_mail_send_preview(payload))
        )

    async def waiting_external_reviews(
        self, thread_id: ConversationThreadId
    ) -> tuple[ConversationExternalReview, ...]:
        """The live reviews of one thread, without settling anything."""
        if self._external is None:
            return ()
        return tuple(await self._external.waiting(thread_id))

    async def send(
        self,
        thread_id: ConversationThreadId,
        text: str,
        *,
        activity: Callable[[str], None] | None = None,
    ) -> ConversationReply:
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
        if self._external is not None:
            # An external review is settled before anything is interpreted, and only by the words
            # the human actually typed (ADR-0034 §11, §23). A generic "可以" is not one of them.
            reviews = await self._external.waiting(thread.id)
            if reviews and matches_send_confirmation(text):
                return await self._settle_external(thread, user_message.id, reviews)
            if reviews and matches_send_cancellation(text):
                for review in reviews:
                    await self._external.cancel(review)
                return await self._finish(
                    thread,
                    await self._control_turn(thread, user_message.id),
                    ConversationTurnStatus.COMPLETED,
                    render.render_review_withdrawn(),
                )
            # Only once the deterministically handled phrases are out of the way is an expired
            # review closed, so "确认发送" after the window answered, never a model call.
            await self._external.expire_due(thread.id)
        pending = await self._repository.operations_waiting_for_confirmation(thread.id)
        intent = _confirmation_intent(text)
        if pending and intent is not None:
            return await self._answer_confirmation(thread, user_message.id, pending, intent, text)
        if pending:
            # Say what is waiting, and keep waiting: a real request still gets interpreted below.
            await self._expire_stale(thread.id, pending)
        return await self._interpret_and_run(thread, user_message.id, text, activity=activity)

    async def _interpret_and_run(
        self,
        thread: ConversationThread,
        user_message_id: ConversationMessageId,
        text: str,
        *,
        activity: Callable[[str], None] | None = None,
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
                error_code=ConversationErrorCode.CAPABILITY_UNAVAILABLE,
            )
        except ConversationInterpretationFailed as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(exc.code, exc.detail),
                error_code=exc.code,
                debug_detail=exc.detail,
            )
        except (ModelOutputNotJson, ModelOutputSchemaViolation, InvalidConversationPlan) as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(ConversationErrorCode.MODEL_INVALID_OUTPUT, str(exc)),
                error_code=ConversationErrorCode.MODEL_INVALID_OUTPUT,
                debug_detail=str(exc),
            )
        except (ModelTransientError, ModelUnavailable, ModelRateLimited) as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(ConversationErrorCode.MODEL_TIMEOUT, str(exc)),
                error_code=ConversationErrorCode.MODEL_TIMEOUT,
                debug_detail=str(exc),
            )
        except (
            ModelAuthenticationError,
            ModelBillingError,
            ModelCredentialsMissing,
            ModelNotConfigured,
        ) as exc:
            # A host problem, not a request problem: say what to fix, in the user's terms.
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(ConversationErrorCode.CAPABILITY_UNAVAILABLE, str(exc)),
                error_code=ConversationErrorCode.CAPABILITY_UNAVAILABLE,
                debug_detail=str(exc),
            )
        except DomainError as exc:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(ConversationErrorCode.OPERATION_FAILED, str(exc)),
                error_code=ConversationErrorCode.OPERATION_FAILED,
                debug_detail=str(exc),
            )
        except Exception as exc:
            # Nothing has been executed at this point: the plan never became a plan. The session
            # stays alive, the turn is recorded as failed, and only the class name is kept.
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_error(ConversationErrorCode.INTERNAL_ERROR, type(exc).__name__),
                error_code=ConversationErrorCode.INTERNAL_ERROR,
                debug_detail=f"{type(exc).__name__}: {exc}",
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
        return await self._run_plan(thread, turn, plan, text=text, activity=activity)

    async def _run_plan(
        self,
        thread: ConversationThread,
        turn: ConversationTurn,
        plan: ConversationPlan,
        *,
        text: str = "",
        activity: Callable[[str], None] | None = None,
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

        recurring = _recurring_creates(plan)
        if recurring:
            # Two deterministic checks the model cannot argue with (ADR-0036 §12, §19): a
            # recurrence v1.1 cannot express is refused, and a statement is answered with a
            # question rather than with durable state.
            named = unsupported_recurrence(text)
            if named is not None:
                for ordinal, operation in enumerate(plan.operations):
                    await self._store_operation(
                        turn, ordinal, operation, status=ConversationOperationStatus.REJECTED
                    )
                return await self._finish(
                    thread,
                    turn,
                    ConversationTurnStatus.FAILED,
                    render.render_unsupported_recurrence(named),
                    operation_types=tuple(
                        operation.operation_type.value for operation in plan.operations
                    ),
                    error_code=ConversationErrorCode.UNSUPPORTED_SEMANTICS,
                )
            if not declares_mutation(text):
                return await self._hold_recurring_for_confirmation(thread, turn, recurring)

        refusal = await self._preflight(plan)
        if refusal is not None:
            # Nothing has run yet, and nothing will: a plan that cannot be carried out in full
            # applies none of itself (ADR-0035 §15-§16).
            for ordinal, operation in enumerate(plan.operations):
                await self._store_operation(
                    turn, ordinal, operation, status=ConversationOperationStatus.REJECTED
                )
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.FAILED,
                render.render_preflight_refused(refusal),
                operation_types=tuple(
                    operation.operation_type.value for operation in plan.operations
                ),
                error_code=ConversationErrorCode.UNSUPPORTED_SEMANTICS,
            )

        results: list[OperationResult] = []
        operation_types: list[str] = []
        waiting: ConversationOperation | None = None
        offer_in_text = False
        for ordinal, planned in enumerate(plan.operations):
            capability = self._capabilities.require(planned.operation_type)
            hint = _ACTIVITY_HINTS.get(planned.operation_type)
            if hint is not None and activity is not None:
                # A transient line, never persisted as conversation content (ADR-0035 §26).
                activity(hint)
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
                planned.operation_type
                is ConversationOperationType.MAIL_PREPARE_REPLY_SEND
                and outcome.result is not None
                and outcome.operation is not None
            ):
                prepared = await self._open_external_review(
                    thread, outcome.operation, outcome.result.ref
                )
                if prepared is not None:
                    # The user is shown the payload, not a summary of it (ADR-0034 §7-8).
                    text_out = "\n".join(
                        part
                        for part in (
                            render.render_results(
                                results[:-1], timezone=self._planning_timezone
                            ),
                            render.render_mail_send_preview(prepared.payload),
                        )
                        if part
                    )
                    return await self._finish(
                        thread,
                        turn,
                        ConversationTurnStatus.COMPLETED,
                        text_out,
                        operation_types=tuple(operation_types),
                    )
            if planned.operation_type is ConversationOperationType.MAIL_REPLY_DRAFT:
                # A draft that moved on invalidates the review that was showing the old text.
                await self._supersede_waiting_reviews(thread)
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
        if len(pending) > 1 and not _is_recurring_batch(pending):
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
        if _is_recurring_batch(pending):
            # One sentence about Monday and Wednesday is one confirmation, not two questions.
            return await self._answer_recurring_batch(thread, turn, pending, intent)
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

    # ------------------------------------------------------- external action settlement

    async def _hold_recurring_for_confirmation(
        self,
        thread: ConversationThread,
        turn: ConversationTurn,
        recurring: Sequence[tuple[int, PlannedOperation]],
    ) -> ConversationReply:
        """Ask before a described weekly commitment becomes durable state (ADR-0036 §19).

        Nothing at all runs in this turn: the unconfirmed commitment is the input the rest of the
        plan was built on, so a plan that includes one applies none of itself (ADR-0035 §16). The
        creates are stored as `WAITING_CONFIRMATION`, which is the runtime's existing local
        confirmation — no second approval framework exists for this.
        """
        for _, planned in recurring:
            capability = self._capabilities.get(planned.operation_type)
            if capability is None or capability.preflight is None:
                continue
            try:
                refusal = await capability.preflight(planned.arguments, PreflightContext())
            except DomainError as exc:
                refusal = str(exc)
            except Exception:
                refusal = f"我现在无法确认「{planned.operation_type.value}」是否可以执行"
            if refusal is not None:
                # Asking about a commitment that could not be saved anyway would be a trap.
                for position, planned_operation in recurring:
                    await self._store_operation(
                        turn,
                        position,
                        planned_operation,
                        status=ConversationOperationStatus.REJECTED,
                    )
                return await self._finish(
                    thread,
                    turn,
                    ConversationTurnStatus.FAILED,
                    render.render_preflight_refused(refusal),
                    operation_types=tuple(
                        planned.operation_type.value for _, planned in recurring
                    ),
                    error_code=ConversationErrorCode.UNSUPPORTED_SEMANTICS,
                )
        expires_at = self._clock.now() + self._confirmation_ttl
        entries: list[dict[str, str | int]] = []
        for position, planned in recurring:
            await self._store_operation(
                turn,
                position,
                planned,
                status=ConversationOperationStatus.WAITING_CONFIRMATION,
                expires_at=expires_at,
            )
            entries.append(_recurring_confirmation_entry(planned))
        return await self._finish(
            thread,
            turn,
            ConversationTurnStatus.WAITING_CONFIRMATION,
            render.render_recurring_confirmation_request(entries),
            operation_types=tuple(
                planned.operation_type.value for _, planned in recurring
            ),
            waiting=True,
        )

    async def _answer_recurring_batch(
        self,
        thread: ConversationThread,
        turn: ConversationTurn,
        pending: Sequence[ConversationOperation],
        intent: str,
    ) -> ConversationReply:
        """Settle one confirmation that covers several weekly commitments at once."""
        now = self._clock.now()
        if any(_is_expired(operation, now) for operation in pending):
            for operation in pending:
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
            for operation in pending:
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
        results: list[OperationResult] = []
        for operation in pending:
            outcome = await self._execute_existing(turn, operation)
            if outcome.failure is not None:
                applied = render.render_results(results, timezone=self._planning_timezone)
                failure = render.render_failure(operation.operation_type.value, outcome.failure)
                return await self._finish(
                    thread,
                    turn,
                    ConversationTurnStatus.FAILED,
                    "\n".join(part for part in (applied, failure) if part),
                )
            if outcome.result is not None:
                results.append(outcome.result)
        return await self._finish(
            thread,
            turn,
            ConversationTurnStatus.COMPLETED,
            render.render_results(results, timezone=self._planning_timezone),
            operation_types=tuple(operation.operation_type.value for operation in pending),
        )

    async def _control_turn(
        self, thread: ConversationThread, user_message_id: ConversationMessageId
    ) -> ConversationTurn:
        """The turn row for a message no model interpreted."""
        context = await self._context_builder.build(thread.id, confirmation_pending=True)
        return await self._repository.add_turn(
            ConversationTurn(
                thread_id=thread.id,
                user_message_id=user_message_id,
                interpreter_version=EXTERNAL_CONFIRMATION_INTERPRETER_VERSION,
                context_fingerprint=_fingerprint(context.to_json()),
                created_at=self._clock.now(),
            )
        )

    async def _settle_external(
        self,
        thread: ConversationThread,
        user_message_id: ConversationMessageId,
        reviews: Sequence[ConversationExternalReview],
    ) -> ConversationReply:
        """Handle one explicit send confirmation: deterministic, no model, no guessing."""
        turn = await self._control_turn(thread, user_message_id)
        external = self._external
        if external is None:  # pragma: no cover - the caller only routes here when it exists
            return await self._finish(
                thread, turn, ConversationTurnStatus.FAILED, render.render_empty_turn()
            )
        if len(reviews) > 1:
            return await self._finish(
                thread,
                turn,
                ConversationTurnStatus.COMPLETED,
                render.render_review_ambiguous(len(reviews)),
            )
        outcome = await external.confirm(reviews[0])
        return await self._finish(
            thread, turn, ConversationTurnStatus.COMPLETED, _settlement_text(outcome)
        )

    async def _open_external_review(
        self,
        thread: ConversationThread,
        operation: ConversationOperation,
        action_reference: str | None,
    ) -> PreparedReview | None:
        """Freeze one prepared action for review, and remember the pointer to it."""
        if self._external is None or action_reference is None:
            return None
        from uuid import UUID

        try:
            action_id = UUID(action_reference)
        except ValueError:  # pragma: no cover - the handler always returns its own action id
            return None
        try:
            return await self._external.open(
                thread_id=thread.id, operation_id=operation.id, action_id=action_id
            )
        except DomainError:
            # The action is gone or no longer reviewed: the operation itself already succeeded, and
            # the user is told about the review failure rather than shown a broken preview.
            return None

    async def _supersede_waiting_reviews(self, thread: ConversationThread) -> None:
        """Mark every waiting review of a thread stale, because what it reviewed changed."""
        if self._external is None:
            return
        for review in await self._external.waiting(thread.id):
            await self._external.supersede(review)

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

    async def _preflight(self, plan: ConversationPlan) -> str | None:
        """Check every operation in a plan before the first mutation runs.

        Returns the first refusal, or `None` when the whole plan is ready. Only read-only
        readiness checks run here: capability existence, and each capability's own precondition
        (does the task exist, is there a draft for this message, is the proposal still pending).
        """
        preceding: tuple[ConversationOperationType, ...] = ()
        for planned in plan.operations:
            capability = self._capabilities.get(planned.operation_type)
            if capability is None:
                return f"这个版本还不支持 {planned.operation_type.value}"
            if capability.preflight is None:
                preceding = (*preceding, planned.operation_type)
                continue
            try:
                refusal = await capability.preflight(
                    planned.arguments, PreflightContext(preceding=preceding)
                )
            except DomainError as exc:
                return str(exc)
            except Exception:
                return f"我现在无法确认「{planned.operation_type.value}」是否可以执行"
            if refusal is not None:
                return refusal
            preceding = (*preceding, planned.operation_type)
        return None

    async def _execute(
        self, turn: ConversationTurn, ordinal: int, planned: PlannedOperation
    ) -> _Execution:
        stored = await self._store_operation(
            turn, ordinal, planned, status=ConversationOperationStatus.PROPOSED
        )
        outcome = await self._execute_existing(turn, stored)
        return _Execution(
            result=outcome.result, failure=outcome.failure, operation=stored
        )

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
        error_code: ConversationErrorCode | None = None,
        debug_detail: str | None = None,
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
            error_code=error_code,
            debug_detail=debug_detail,
        )


@dataclass(frozen=True, slots=True)
class _Execution:
    """The outcome of one handler call."""

    result: OperationResult | None
    failure: str | None
    operation: ConversationOperation | None = None


def _apply_arguments(proposal_id: str) -> ConversationOperationArguments:
    from assistant.domain.conversation_plan import PlanApplyProposalArguments

    return PlanApplyProposalArguments(proposal_id=proposal_id)


def _recurring_creates(
    plan: ConversationPlan,
) -> tuple[tuple[int, PlannedOperation], ...]:
    """Every weekly-commitment creation in a plan, with the ordinal it will be stored at."""
    return tuple(
        (ordinal, planned)
        for ordinal, planned in enumerate(plan.operations)
        if planned.operation_type is ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY
    )


def _recurring_confirmation_entry(
    planned: PlannedOperation,
) -> dict[str, str | int]:
    """The four fields the question about one weekly commitment needs."""
    arguments = planned.arguments
    if not isinstance(arguments, CalendarRecurringCreateWeeklyArguments):
        raise AssertionError(  # pragma: no cover - the caller filters by operation type
            "a recurring confirmation entry needs a weekly-commitment creation"
        )
    return {
        "title": arguments.title,
        "weekday": arguments.weekday,
        "start_local_time": arguments.start_local_time,
        "end_local_time": arguments.end_local_time,
    }


def _is_recurring_batch(pending: Sequence[ConversationOperation]) -> bool:
    """Whether every waiting operation is one weekly-commitment creation."""
    if not pending:
        return False
    return all(
        operation.operation_type
        is ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY
        for operation in pending
    )


def _settlement_text(outcome: ConfirmationOutcome) -> str:
    """One settled review, in the words the user needs (ADR-0034 §19)."""
    status = outcome.review.status
    if status is ConversationExternalReviewStatus.SUCCEEDED:
        return render.render_send_result("succeeded")
    if status is ConversationExternalReviewStatus.UNKNOWN:
        return render.render_send_result("unknown")
    if status is ConversationExternalReviewStatus.FAILED:
        return render.render_send_result("failed", outcome.failure_reason)
    if status is ConversationExternalReviewStatus.EXPIRED:
        return render.render_review_expired()
    return render.render_review_stale(outcome.failure_reason)


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
