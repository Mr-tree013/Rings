"""What one browser-dispatched turn may report, and when it may be stopped (ADR-0041 §11, §30-§34).

```text
UNDERSTANDING ──► (plan validated) ──► READING / PLANNING / KNOWLEDGE / MAIL / UPDATING
      │                                              │
   Stop is safe                                 Stop is refused
      │                                              │
   no mutation yet                            a mutation or an external effect has begun
```

Two small objects carry that contract:

* `TurnRuntime` is the optional companion a queued turn receives. It holds the progress sink (an
  application-owned callback that persists a coarse `ConversationProgressStage` and notifies the
  ephemeral event stream) and the cancellation token. A turn started from the terminal passes
  `None`, which is why every existing entry point keeps working unchanged.
* `ConversationCancellation` is the token itself. It is *cooperative*: nothing here cancels a
  task. A checkpoint either sees the request and raises, or the work proceeds. Abandoning a model
  call that is already in flight is safe — its answer is simply never applied — while cancelling
  a task mid-mutation would not be.

Nothing in this module knows what a stage *means*: the mapping from a validated plan to a stage
lives in `stage_for_plan`, and the model never chooses a label (ADR-0041 §11, §41).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from assistant.domain.conversation_plan import (
    READ_OPERATIONS,
    ConversationOperationType,
    ConversationPlan,
)
from assistant.domain.conversation_request import ConversationProgressStage
from assistant.domain.errors import ConversationRequestCancelled

T = TypeVar("T")

ProgressSink = Callable[[ConversationProgressStage], Awaitable[None]]
"""Where a coarse stage goes. The caller decides whether that is durable, ephemeral or both."""

PREPARING_MAIL_OPERATIONS = frozenset(
    {
        ConversationOperationType.MAIL_PREPARE_REPLY_SEND,
        ConversationOperationType.MAIL_PREPARE_NEW_SEND,
    }
)
"""Operations that end in a reviewable outbound message."""

PLANNING_OPERATIONS = frozenset(
    {
        ConversationOperationType.PLAN_PROPOSE_WEEK,
        ConversationOperationType.PLAN_APPLY_PROPOSAL,
    }
)
"""Operations that schedule or commit time."""

KNOWLEDGE_OPERATIONS = frozenset({ConversationOperationType.KNOWLEDGE_ASK})
"""Operations that answer from indexed personal knowledge."""


def stage_for_plan(plan: ConversationPlan) -> ConversationProgressStage:
    """The one coarse stage a validated plan puts the user in front of.

    Deterministic and application-owned: it is a function of the operation types the registry
    accepted, never of the model's words. The order of the checks is the order of the product's
    risk: an outbound message is "preparing mail" even when the same turn also drafted it, a
    scheduling write is "planning" even when the same turn also wrote a task, and anything that
    writes locally is "updating local state" rather than "reading" even if it also reads.
    """
    types = {operation.operation_type for operation in plan.operations}
    if types & PREPARING_MAIL_OPERATIONS:
        return ConversationProgressStage.PREPARING_MAIL
    if types & PLANNING_OPERATIONS:
        return ConversationProgressStage.PLANNING
    if types & KNOWLEDGE_OPERATIONS:
        return ConversationProgressStage.QUERYING_KNOWLEDGE
    if types - READ_OPERATIONS:
        return ConversationProgressStage.UPDATING_LOCAL_STATE
    return ConversationProgressStage.READING_LOCAL_STATE


class ConversationCancellation:
    """A cooperative stop request for one queued conversation request."""

    def __init__(self) -> None:
        self._requested = asyncio.Event()

    @property
    def requested(self) -> bool:
        """Whether the user has asked to stop."""
        return self._requested.is_set()

    def request(self) -> None:
        """Record the stop request. Idempotent."""
        self._requested.set()

    def checkpoint(self) -> None:
        """Raise if a stop has been requested.

        Raises:
            ConversationRequestCancelled: the user asked to stop, and this call is a safe point.
        """
        if self.requested:
            raise ConversationRequestCancelled("the user stopped this request")

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Run one cancellable await, abandoning it if the user stops first.

        Nothing is cancelled underneath: if the stop wins the race, the pending work is left to
        finish on its own and its answer is dropped on the floor. That is the whole safety
        argument — a model reply that arrives after a stop is never applied, and no task is torn
        down in a place that might already be writing.
        """
        self.checkpoint()
        work = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self._requested.wait())
        try:
            done, _ = await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            stop.cancel()
            work.cancel()
            raise
        if work in done:
            stop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop
            return work.result()
        work.add_done_callback(_discard)
        raise ConversationRequestCancelled("the user stopped this request")


@dataclass(frozen=True, slots=True)
class TurnRuntime:
    """The companion a queued turn gets: where to report, and how to be stopped."""

    progress: ProgressSink | None = None
    cancellation: ConversationCancellation | None = None

    async def report(self, stage: ConversationProgressStage) -> None:
        """Tell the caller what coarse stage this turn is in."""
        if self.progress is not None:
            await self.progress(stage)

    def checkpoint(self) -> None:
        """Refuse to continue if the user has stopped this request.

        Raises:
            ConversationRequestCancelled: a stop was requested at a safe point.
        """
        if self.cancellation is not None:
            self.cancellation.checkpoint()

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Run one cancellable await under this runtime's token."""
        if self.cancellation is None:
            return await awaitable
        return await self.cancellation.guard(awaitable)


def _discard[V](task: asyncio.Future[V]) -> None:
    """Consume an abandoned result so a late provider answer is not an unhandled exception."""
    if task.cancelled():  # pragma: no cover - nothing cancels the abandoned work
        return
    with contextlib.suppress(Exception):
        task.exception()


__all__ = [
    "KNOWLEDGE_OPERATIONS",
    "PLANNING_OPERATIONS",
    "PREPARING_MAIL_OPERATIONS",
    "ConversationCancellation",
    "ProgressSink",
    "TurnRuntime",
    "stage_for_plan",
]
