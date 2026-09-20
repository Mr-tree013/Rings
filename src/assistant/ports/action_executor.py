"""ActionExecutor port: the one place an external side effect could happen (ADR-0023).

An executor is registered for exactly one `ActionType` and receives the exact `ActionRequest` it
was approved for. It returns an `ExecutionOutcome`; it does not decide whether it may run — the
execution service consumed an approval before calling it.

There is deliberately no generic implementation of this protocol anywhere in the project: no HTTP
executor, no shell executor, no browser executor, no dynamic-import executor. Phase 6A ships an
**empty** registry, so every action type that can be named today is one that cannot be performed.
That is the whole reason high-risk capabilities are safe: they are missing, not forbidden.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.execution import ExecutionOutcome


class ActionExecutor(Protocol):
    """Performs one kind of approved external side effect."""

    @property
    def action_type(self) -> ActionType:
        """The single action type this executor handles."""
        ...

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        """Perform `action` and say how it ended.

        Returning normally means the attempt reached a definite outcome. Raising is allowed and
        is treated conservatively: the run is recorded as `UNKNOWN`, because an executor that
        raised after starting may already have changed the outside world. `asyncio.CancelledError`
        must be allowed to propagate: the run stays `RUNNING` for a later reconciliation.
        """
        ...


__all__ = ["ActionExecutor"]
