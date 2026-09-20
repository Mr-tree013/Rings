"""ActionExecutor port: the one place an external side effect could happen (ADR-0023, ADR-0024).

An executor is registered for exactly one `ActionType` and receives the exact `ActionRequest` it
was approved for. It returns an `ExecutionOutcome`; it does not decide whether it may run — the
execution service consumed an approval before calling it.

`supports` is the preflight: a **pure, offline** question — "could this action be performed by
this deployment right now?" — asked *before* an approval is consumed. Without it, an
unconfigured account or a missing credential would be discovered after the only approval had
already been spent, which turns a configuration mistake into a lost human decision.

There is deliberately no generic implementation of this protocol anywhere in the project: no HTTP
executor, no shell executor, no browser executor, no dynamic-import executor. An executor exists
only where a specific capability was written, reviewed and configured.
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

    def supports(self, action: ActionRequest) -> bool:
        """Whether this deployment could perform `action` right now.

        Pure and offline: it may read the action's payload and local configuration, and it must
        not contact anything. Returning `False` means the execution service refuses with
        `CapabilityUnavailable` *without* consuming an approval.
        """
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
