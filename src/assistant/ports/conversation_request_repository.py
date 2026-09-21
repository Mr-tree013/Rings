"""Durable accepted browser input (Phase 11A, ADR-0041 §4-§10).

The application layer depends on this protocol and never on SQLite. Every method is one durable
fact about one accepted message; the *order* in which they are written is what makes a crash
recoverable, so no method batches two of them.

Two operations carry the phase's safety properties:

* `accept` is idempotent on `(thread_id, client_request_id)`, so a retried POST can never become a
  second turn;
* `claim_next` is a single atomic transaction that selects the oldest queued request of a thread
  *and* refuses to claim while the thread already has one being processed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from assistant.domain.conversation_request import (
    ConversationProgressStage,
    ConversationRequest,
    ConversationRequestId,
    ConversationRequestStatus,
)


class ConversationRequestRepository(Protocol):
    """The durable queue of accepted conversation input."""

    async def accept(self, request: ConversationRequest) -> tuple[ConversationRequest, bool]:
        """Store a request, or return the one an identical accept already produced.

        Returns `(request, created)`. The second element is `False` when the
        `(thread_id, client_request_id)` pair was already accepted, in which case the stored row is
        returned unchanged and nothing new is written.
        """

    async def get(self, request_id: ConversationRequestId) -> ConversationRequest | None:
        """Return one request, or `None`."""

    async def get_by_client_id(
        self, thread_id: UUID, client_request_id: str
    ) -> ConversationRequest | None:
        """Return the request one browser-generated id already produced, or `None`."""

    async def list_for_thread(
        self, thread_id: UUID, *, limit: int = 50
    ) -> list[ConversationRequest]:
        """Requests of one thread, newest first."""

    async def list_by_status(
        self, status: ConversationRequestStatus, *, limit: int = 100
    ) -> list[ConversationRequest]:
        """Requests in one status, oldest first — the restart scan uses this."""

    async def claim_next(
        self, *, at: datetime, thread_id: UUID | None = None
    ) -> ConversationRequest | None:
        """Atomically claim the oldest queued request, or return `None`.

        The claim is one transaction: it selects the oldest `QUEUED` row of the thread and moves it
        to `PROCESSING` in the same statement pair. A thread that already has a `PROCESSING`
        request has nothing claimable, which is how "one active request per thread" is enforced
        rather than remembered.
        """

    async def set_stage(
        self, request_id: ConversationRequestId, stage: ConversationProgressStage
    ) -> ConversationRequest:
        """Record the coarse stage the runtime entered."""

    async def attach_turn(
        self, request_id: ConversationRequestId, turn_id: UUID
    ) -> ConversationRequest:
        """Bind the durable turn this request produced, in both directions.

        Raises:
            InvalidConversationRequest: the request or the turn is already bound to something else.
        """

    async def request_cancel(
        self, request_id: ConversationRequestId, *, at: datetime
    ) -> ConversationRequest:
        """Record that the user asked to stop, without deciding anything about the outcome."""

    async def complete(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        stage: ConversationProgressStage | None = None,
        error_code: str | None = None,
    ) -> ConversationRequest:
        """Finish one processing request successfully."""

    async def fail(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        error_code: str | None,
        stage: ConversationProgressStage | None = None,
    ) -> ConversationRequest:
        """Finish one processing request as a product-level failure."""

    async def cancel(
        self, request_id: ConversationRequestId, *, at: datetime
    ) -> ConversationRequest:
        """Finish one request as cancelled. A queued request never becomes a turn."""

    async def interrupt(
        self,
        request_id: ConversationRequestId,
        *,
        at: datetime,
        error_code: str | None = None,
        stage: ConversationProgressStage | None = None,
    ) -> ConversationRequest:
        """Finish one request as interrupted: started, and never blindly replayed."""

    async def recover(self, *, at: datetime) -> tuple[ConversationRequest, ...]:
        """Resolve every `PROCESSING` row left behind by a crash, fail-closed.

        Returns the rows that were resolved. A linked turn that is already terminal is mirrored; a
        turn that is parked waiting for a human is accepted as finished; anything else — including
        a claim with no turn evidence at all — becomes `INTERRUPTED` and is never replayed.
        """


__all__ = ["ConversationRequestRepository"]
