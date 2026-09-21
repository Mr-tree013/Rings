"""The bridge between accepted browser input and the existing conversation runtime (ADR-0041 §7).

```text
POST ──► accept (idempotent, durable, QUEUED)
             │
             ├─► per-thread worker ──► claim_next (one active per thread)
             │                              │
             │                              ├─► ConversationService.send(runtime=…)
             │                              └─► terminal status + durable turn correlation
             └─► Stop ──► cancel (queued) / cancellation token (understanding)
```

This is not a task executor and must never become one. It knows three things: how to accept a
message, how to feed exactly that text to the *existing* `ConversationService`, and how to record
what happened. Capability decisions, confirmations, mutations and error wording all stay where they
already were.

The queue's guarantees are the phase's guarantees:

* **idempotent accept** — `(thread_id, client_request_id)` names one request, so a retried POST
  cannot become a second turn, a second model call or a second mutation;
* **one active request per thread** — the claim refuses while a thread is busy, so a thread's
  conversational order is preserved while different threads stay independent;
* **no blind replay** — a request left `PROCESSING` by a crash is resolved fail-closed at startup,
  never re-run (ADR-0041 §10).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from uuid import UUID

from assistant.application.conversation_event_broker import ConversationEventBroker
from assistant.application.conversation_progress import (
    ConversationCancellation,
    ProgressSink,
    TurnRuntime,
)
from assistant.application.conversation_service import ConversationReply, ConversationService
from assistant.domain.conversation import (
    ConversationThreadStatus,
    ConversationTurnStatus,
)
from assistant.domain.conversation_request import (
    ConversationProgressStage,
    ConversationRequest,
    ConversationRequestId,
    ConversationRequestStatus,
)
from assistant.domain.errors import (
    CannotCancelSafely,
    ConversationCapabilityUnavailable,
    ConversationInterpretationFailed,
    ConversationRequestCancelled,
    ConversationRequestNotFound,
    ConversationThreadNotFound,
    DomainError,
    InvalidConversationRequest,
    InvalidConversationThread,
    ModelAuthenticationError,
    ModelBillingError,
    ModelCredentialsMissing,
    ModelNotConfigured,
    ModelRateLimited,
    ModelTransientError,
    ModelUnavailable,
)
from assistant.ports.clock import Clock
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.conversation_request_repository import ConversationRequestRepository

LOGGER = logging.getLogger("assistant.conversation.queue")

INTERNAL_FAILURE_CODE = "INTERNAL_ERROR"
"""The product-level code a request gets when nothing else could describe it."""

STOPPED_ERROR_CODE = "INTERRUPTED"
"""What a request records when a crash or a stop left it unfinished."""


class ConversationRequestCoordinator:
    """Accepts, queues, serialises and settles browser conversation requests."""

    def __init__(
        self,
        *,
        requests: ConversationRequestRepository,
        conversation: ConversationService,
        threads: ConversationRepository,
        broker: ConversationEventBroker,
        clock: Clock,
    ) -> None:
        self._requests = requests
        self._conversation = conversation
        self._threads = threads
        self._broker = broker
        self._clock = clock
        self._workers: dict[UUID, asyncio.Task[None]] = {}
        self._cancellations: dict[ConversationRequestId, ConversationCancellation] = {}
        self._current: dict[ConversationRequestId, ConversationRequest] = {}

    # ------------------------------------------------------------------ accepting

    async def accept(
        self, thread_id: UUID, *, client_request_id: str, text: str
    ) -> tuple[ConversationRequest, bool]:
        """Accept one browser message, durably, before anything is executed.

        Raises:
            ConversationThreadNotFound: no such thread.
            InvalidConversationThread: the thread is archived.
            InvalidConversationRequest: the text or the client id is unusable.
        """
        thread = await self._threads.get_thread(thread_id)
        if thread is None:
            raise ConversationThreadNotFound(thread_id)
        if thread.status is not ConversationThreadStatus.ACTIVE:
            raise InvalidConversationThread("this conversation is archived")
        request = ConversationRequest(
            thread_id=thread_id,
            client_request_id=client_request_id,
            input_text=text,
            created_at=self._clock.now(),
        )
        stored, created = await self._requests.accept(request)
        if created:
            self._publish(stored, "request.queued")
            self._ensure_worker(thread_id)
        else:
            # A retry settles on the accepted row; the work it already asked for is already queued
            # (or already running), so there is nothing new to start.
            self._ensure_worker(thread_id)
        return stored, created

    # ------------------------------------------------------------------ stopping

    async def cancel(self, request_id: ConversationRequestId) -> ConversationRequest:
        """Stop one request, or refuse and say why.

        A queued request is cancelled outright: it never became a turn. A request that is still
        being understood is asked to stop at its next checkpoint, and the runtime then leaves no
        mutation behind. Anything further along is refused with `CannotCancelSafely`, because
        claiming a stop after a write or an external effect would be a lie.

        Raises:
            ConversationRequestNotFound: no such request.
            CannotCancelSafely: the request has crossed a boundary that cannot be undone.
        """
        request = await self._requests.get(request_id)
        if request is None:
            raise ConversationRequestNotFound(request_id)
        if request.is_terminal:
            return request
        if not request.can_cancel:
            raise CannotCancelSafely(
                f"request {request_id} is {request.stage.value if request.stage else 'busy'} "
                "and can no longer be stopped safely"
            )
        now = self._clock.now()
        stored = await self._requests.request_cancel(request_id, at=now)
        if request.status is ConversationRequestStatus.QUEUED:
            # This is the easy half of Stop, and the half that must never claim more than it did:
            # the request is cancelled, and no turn, model call or mutation will ever happen.
            cancelled = await self._requests.cancel(request_id, at=now)
            self._publish(cancelled, "request.cancelled")
            return cancelled
        token = self._cancellations.get(request_id)
        if token is not None:
            token.request()
        self._publish(stored, "request.cancel_requested")
        return stored

    # ------------------------------------------------------------------ recovery

    async def recover(self) -> tuple[ConversationRequest, ...]:
        """Resolve what a previous process left behind, then resume only what is safe.

        `APPLYING` operations become `UNKNOWN_LOCAL` first (they may or may not have run), then
        every `PROCESSING` request is resolved fail-closed by the repository. Only `QUEUED` work is
        picked up again — it is the only state in which *nothing* has happened yet.
        """
        with contextlib.suppress(DomainError):
            await self._conversation.recover_interrupted()
        resolved = await self._requests.recover(at=self._clock.now())
        for request in resolved:
            self._publish(request, "request.interrupted" if request.status
                          is ConversationRequestStatus.INTERRUPTED else "request.completed")
            LOGGER.info(
                "recovered conversation request %s as %s", request.id, request.status.value
            )
        for thread_id in await self._threads_with_queued_work():
            self._ensure_worker(thread_id)
        return resolved

    async def _threads_with_queued_work(self) -> tuple[UUID, ...]:
        queued = await self._requests.list_by_status(ConversationRequestStatus.QUEUED)
        seen: list[UUID] = []
        for request in queued:
            if request.thread_id not in seen:
                seen.append(request.thread_id)
        return tuple(seen)

    async def drain(self) -> None:
        """Wait for every worker this process started, then return.

        Used by the daemon's shutdown path and by tests that want a deterministic point at which
        the queue is empty.
        """
        while True:
            tasks = [task for task in self._workers.values() if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop the workers without pretending their requests finished.

        A worker cancelled here leaves its request `PROCESSING`, which is exactly what the next
        start must see: unfinished business that is resolved fail-closed rather than replayed.
        """
        tasks = [task for task in self._workers.values() if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()

    # ------------------------------------------------------------------ snapshot helpers

    def cancellation_for(self, request_id: ConversationRequestId) -> ConversationCancellation:
        """The live token for one request, created on demand."""
        token = self._cancellations.get(request_id)
        if token is None:
            token = ConversationCancellation()
            self._cancellations[request_id] = token
        return token

    # ------------------------------------------------------------------ the worker

    def _ensure_worker(self, thread_id: UUID) -> None:
        task = self._workers.get(thread_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._drain_thread(thread_id), name=f"chat-worker:{thread_id}")
        self._workers[thread_id] = task
        task.add_done_callback(self._retirement(thread_id))

    def _retirement(self, thread_id: UUID) -> Callable[[asyncio.Task[None]], None]:
        """A done-callback that removes exactly the worker it was created for."""

        def _retire(finished: asyncio.Task[None]) -> None:
            if self._workers.get(thread_id) is finished:
                self._workers.pop(thread_id, None)

        return _retire

    async def _drain_thread(self, thread_id: UUID) -> None:
        """Process one thread's queue, oldest first, one request at a time."""
        try:
            while True:
                request = await self._requests.claim_next(
                    at=self._clock.now(), thread_id=thread_id
                )
                if request is None:
                    return
                await self._process(request)
        except asyncio.CancelledError:
            # Shutdown is not a failure of the conversation: the request stays `PROCESSING` and the
            # next start resolves it fail-closed rather than replaying it.
            raise
        except Exception:
            # A worker that dies must not take the queue with it, and its request stays
            # `PROCESSING` so the next start resolves it instead of replaying it.
            LOGGER.exception("conversation queue worker for %s stopped unexpectedly", thread_id)

    async def _process(self, request: ConversationRequest) -> None:
        text = request.input_text
        if text is None:  # pragma: no cover - the schema keeps queued/processing input present
            self._publish_terminal(
                await self._requests.interrupt(
                    request.id, at=self._clock.now(), error_code=INTERNAL_FAILURE_CODE
                )
            )
            return
        token = self.cancellation_for(request.id)
        if request.cancel_requested_at is not None:
            # The user stopped it between the accept and the claim: honour that immediately.
            token.request()
        self._current[request.id] = request
        self._publish(request, "request.started")
        runtime = TurnRuntime(progress=self._progress_for(request), cancellation=token)
        try:
            reply = await self._conversation.send(request.thread_id, text, runtime=runtime)
        except ConversationRequestCancelled:
            # Stopped before a turn existed: the durable message stays, and nothing else happened.
            self._publish_terminal(
                await self._requests.cancel(request.id, at=self._clock.now())
            )
            return
        except DomainError as exc:
            LOGGER.info("conversation request %s failed: %s", request.id, type(exc).__name__)
            self._publish_terminal(
                await self._requests.fail(
                    request.id,
                    at=self._clock.now(),
                    error_code=_error_code(exc),
                    stage=self._stage_of(request),
                )
            )
            return
        except Exception as exc:
            LOGGER.warning(
                "conversation request %s raised %s", request.id, type(exc).__name__
            )
            self._publish_terminal(
                await self._requests.fail(
                    request.id,
                    at=self._clock.now(),
                    error_code=INTERNAL_FAILURE_CODE,
                    stage=self._stage_of(request),
                )
            )
            return
        finally:
            self._cancellations.pop(request.id, None)
            self._current.pop(request.id, None)
        await self._settle(request, reply)

    async def _settle(self, request: ConversationRequest, reply: ConversationReply) -> None:
        """Record the outcome and bind the durable turn this request produced."""
        attached = request
        try:
            attached = await self._requests.attach_turn(request.id, UUID(reply.turn_id))
        except (InvalidConversationRequest, ValueError) as exc:
            # The turn exists but could not be correlated. That is a *durability* problem, not a
            # conversation problem: say so, and never replay the work that already happened.
            LOGGER.warning("request %s could not be correlated: %s", request.id, exc)
        stage = self._stage_for_reply(reply)
        now = self._clock.now()
        if reply.status is ConversationTurnStatus.INTERRUPTED:
            final = await self._requests.cancel(request.id, at=now)
        elif reply.status is ConversationTurnStatus.FAILED:
            final = await self._requests.fail(
                request.id,
                at=now,
                error_code=(
                    reply.error_code.value
                    if reply.error_code is not None
                    else INTERNAL_FAILURE_CODE
                ),
                stage=stage,
            )
        else:
            final = await self._requests.complete(request.id, at=now, stage=stage)
        if attached.turn_id is None and final.turn_id is None:
            LOGGER.info("request %s finished without a linked turn", request.id)
        self._publish_terminal(final)
        self._publish_message(final, reply)

    def _progress_for(self, request: ConversationRequest) -> ProgressSink:
        async def _progress(stage: ConversationProgressStage) -> None:
            try:
                stored = await self._requests.set_stage(request.id, stage)
            except DomainError:
                # The request was settled while the turn was still reporting: the stage of a
                # finished request is not worth failing the turn over.
                return
            self._current[request.id] = stored
            self._broker.publish(
                request.thread_id,
                "request.stage",
                {
                    "request_id": str(request.id),
                    "stage": stage.value,
                    "can_cancel": stored.can_cancel,
                },
            )

        return _progress

    def _stage_of(self, request: ConversationRequest) -> ConversationProgressStage | None:
        current = self._current.get(request.id, request)
        return current.stage

    def _stage_for_reply(self, reply: ConversationReply) -> ConversationProgressStage:
        if reply.waiting_for_confirmation:
            return ConversationProgressStage.WAITING_CONFIRMATION
        if reply.status is ConversationTurnStatus.INTERRUPTED:
            return ConversationProgressStage.UNDERSTANDING
        return ConversationProgressStage.FINALIZING

    # ------------------------------------------------------------------ events

    def _publish_terminal(self, request: ConversationRequest) -> None:
        """Announce one finished request under its own status name."""
        self._publish(request, f"request.{request.status.value}")

    def _publish(self, request: ConversationRequest, name: str) -> None:
        self._broker.publish(
            request.thread_id,
            name,
            {
                "request_id": str(request.id),
                "status": request.status.value,
                "stage": None if request.stage is None else request.stage.value,
                "can_cancel": request.can_cancel,
                "thread_id": str(request.thread_id),
            },
        )

    def _publish_message(self, request: ConversationRequest, reply: ConversationReply) -> None:
        """Tell an open browser that the thread has new durable content to fetch.

        The assistant's own words are user-facing text the authorized browser is already entitled
        to see; nothing else about the turn travels here.
        """
        self._broker.publish(
            request.thread_id,
            "assistant.message",
            {
                "request_id": str(request.id),
                "turn_id": reply.turn_id,
                "text": reply.text,
                "status": reply.status.value,
                "waiting_for_confirmation": reply.waiting_for_confirmation,
                "error_code": None if reply.error_code is None else reply.error_code.value,
            },
        )
        self._broker.publish(request.thread_id, "thread.updated", {"thread_id":
                                                                  str(request.thread_id)})


def _error_code(exc: DomainError) -> str:
    """A bounded product-level code for a failure that escaped before a reply was written.

    A closed mapping over exception classes, in the same spirit as the capability registry: no
    reflection, no attribute hunting, and no exception text ever reaching a browser.
    """
    if isinstance(exc, ConversationInterpretationFailed):
        return exc.code.value
    if isinstance(exc, ConversationCapabilityUnavailable):
        return "CAPABILITY_UNAVAILABLE"
    if isinstance(exc, (ModelTransientError, ModelUnavailable, ModelRateLimited)):
        return "MODEL_TIMEOUT"
    if isinstance(
        exc,
        (
            ModelAuthenticationError,
            ModelBillingError,
            ModelCredentialsMissing,
            ModelNotConfigured,
        ),
    ):
        return "CAPABILITY_UNAVAILABLE"
    return "OPERATION_FAILED"


__all__ = [
    "INTERNAL_FAILURE_CODE",
    "STOPPED_ERROR_CODE",
    "ConversationRequestCoordinator",
]
