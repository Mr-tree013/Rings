"""Executing an approved action, or refusing to (ADR-0023).

```text
resolve executor for action_type
        ├── none ──────────────► CapabilityUnavailable   (no approval is consumed)
        ├── supports(action) false ─► CapabilityUnavailable (no approval is consumed)
        ▼
re-hash the stored payload ──► ActionFingerprintMismatch (the executor is never called)
        ▼
begin_execution   one transaction: exact approval consumed + RUNNING run created
        ▼
executor.execute(action)
        ├── SUCCEEDED ──► run SUCCEEDED + action EXECUTED   (same transaction)
        ├── FAILED ─────► run FAILED,    action stays PREPARED
        ├── UNKNOWN ────► run UNKNOWN,   action stays PREPARED, no blind retry
        ├── raises ─────► run UNKNOWN,   then ActionExecutionUnknown is raised
        └── CancelledError ─► propagates; the run stays RUNNING
```

Everything about this module is conservative in the same direction: an unclear outcome never
becomes a retry, and a consumed approval is never handed back. The one thing it will not do is
guess whether an external side effect happened.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from assistant.domain.action import ActionRequest, ActionRequestId, ActionType
from assistant.domain.errors import (
    ActionExecutionUnknown,
    ActionExecutionUnresolved,
    ActionFingerprintMismatch,
    ActionNotExecutable,
    ActionRequestNotFound,
    CapabilityUnavailable,
)
from assistant.domain.execution import (
    ExecutionOutcome,
    ExecutionRun,
    ExecutionRunStatus,
    new_execution_run_id,
)
from assistant.ports.action_executor import ActionExecutor
from assistant.ports.action_repository import ActionRepository, StartedExecution
from assistant.ports.clock import Clock

LOGGER = logging.getLogger("assistant.actions")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What one `execute` call did, in the terms a user can read."""

    action_id: ActionRequestId
    run: ExecutionRun
    action_status: str

    @property
    def status(self) -> ExecutionRunStatus:
        """The status of the run this call produced."""
        return self.run.status


class ActionExecutionService:
    """Runs one approved action, once, through whatever executor is registered for its type."""

    def __init__(
        self,
        actions: ActionRepository,
        executors: Mapping[ActionType, ActionExecutor],
        clock: Clock,
    ) -> None:
        registry: dict[str, ActionExecutor] = {}
        for action_type, executor in executors.items():
            if executor.action_type != action_type:
                raise ValueError(
                    f"executor for {action_type} reports {executor.action_type}"
                )
            registry[action_type.value] = executor
        self._registry = registry
        self._actions = actions
        self._clock = clock

    @property
    def capability_set(self) -> tuple[str, ...]:
        """The action types this deployment can actually perform, sorted.

        Phase 6A ships this empty: every action type that can be named is one that cannot be
        performed.
        """
        return tuple(sorted(self._registry))

    async def execute(self, action_id: ActionRequestId) -> ExecutionResult:
        """Execute one prepared, approved action.

        Raises:
            ActionRequestNotFound: no such action.
            CapabilityUnavailable: no executor is registered for this action type. No approval is
                consumed and no execution run is created.
            ActionNotExecutable: the action is not PREPARED.
            ActionFingerprintMismatch: the stored payload no longer matches its fingerprint.
            ActionExecutionUnresolved: an earlier attempt has no decidable outcome.
            ApprovalUnavailable: no usable approval exists for this exact content.
            ActionExecutionUnknown: the attempt ended without a decidable result and was
                recorded as UNKNOWN. It is never retried automatically.
        """
        action = await self._actions.get_action(action_id)
        if action is None:
            raise ActionRequestNotFound(action_id)
        executor = self._registry.get(action.action_type.value)
        if executor is None:
            # Refuse before touching the approval: a missing capability must cost nothing.
            raise CapabilityUnavailable(action.action_type)
        if not executor.supports(action):
            # The preflight is pure and offline, and it runs *before* the approval is consumed:
            # a missing credential or an unconfigured account must never spend a human decision.
            raise CapabilityUnavailable(
                f"{action.action_type} is registered but cannot perform this action "
                "(check the account's outbound configuration and credential)"
            )
        if not action.is_prepared:
            raise ActionNotExecutable(
                f"action request {action.id} is {action.status}, not prepared"
            )
        if not action.fingerprint_matches():
            # Re-hashed here as well as in the store: never trust the stored field alone.
            raise ActionFingerprintMismatch(action.id)

        started = await self._begin(action)
        try:
            outcome = await executor.execute(action)
        except asyncio.CancelledError:
            # The run stays RUNNING under its consumed approval. A later reconciliation decides;
            # this process must not guess.
            raise
        except Exception as exc:
            await self._record_unknown(
                started.run, f"the executor raised {type(exc).__name__}: {exc}"
            )
            raise ActionExecutionUnknown(action.id) from exc
        finished = await self._actions.finish_execution(
            run_id=started.run.id, outcome=outcome, at=self._clock.now()
        )
        LOGGER.info(
            "action execution finished type=%s status=%s",
            action.action_type.value,
            finished.status.value,
        )
        return ExecutionResult(
            action_id=action.id,
            run=finished,
            action_status=_action_status_after(finished),
        )

    async def _begin(self, action: ActionRequest) -> StartedExecution:
        """Consume the exact approval and start the run, atomically."""
        unresolved = await self._actions.latest_execution(action.id)
        if unresolved is not None and unresolved.blocks_retry:
            # Belt and braces: the store enforces this too, inside its transaction.
            raise ActionExecutionUnresolved(action.id, unresolved.status.value)
        return await self._actions.begin_execution(
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            run_id=new_execution_run_id(),
            now=self._clock.now(),
        )

    async def _record_unknown(self, run: ExecutionRun, summary: str) -> None:
        await self._actions.finish_execution(
            run_id=run.id,
            outcome=ExecutionOutcome.unknown(summary),
            at=self._clock.now(),
        )


def _action_status_after(run: ExecutionRun) -> str:
    """The action status a finished run leaves behind."""
    return "executed" if run.status is ExecutionRunStatus.SUCCEEDED else "prepared"


__all__ = ["ActionExecutionService", "ExecutionResult"]
