"""Reading actions, and saying honestly what state they are in (ADR-0023).

This service is read-only. It exists so the CLI can show one coherent picture of an action —
content, the approval situation and the execution history — without the command reaching into
three repositories and inventing its own notion of "approved".

The vocabulary is deliberately blunt:

- **approval**: `none`, `valid`, `consumed`, `expired` or `superseded`, derived from the stored
  records and the clock, never from a boolean anyone set;
- **execution**: `none`, `running`, `succeeded`, `failed` or `unknown`, taken from the latest run.

Nothing here can create, approve or execute anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.approval import ApprovalRecord
from assistant.domain.errors import ActionRequestNotFound
from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock


class ApprovalState(StrEnum):
    """How much authority an action currently has, in one word."""

    NONE = "none"
    VALID = "valid"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ActionOverview:
    """One action, its latest approval and its latest execution."""

    action: ActionRequest
    approval_state: ApprovalState = ApprovalState.NONE
    approval: ApprovalRecord | None = None
    execution: ExecutionRun | None = None
    execution_count: int = 0
    case_title: str | None = None

    @property
    def execution_state(self) -> str:
        """The execution word for display: `none` when nothing ever ran."""
        return "none" if self.execution is None else self.execution.status.value


class ActionService:
    """Read-only views over prepared actions."""

    def __init__(self, actions: ActionRepository, clock: Clock) -> None:
        self._actions = actions
        self._clock = clock

    async def resolve_action_id(self, reference: str) -> ActionRequestId:
        """Resolve a full UUID or a unique prefix to an action id."""
        return await self._actions.resolve_action_id(reference)

    async def require_action(self, reference: ActionRequestId | str) -> ActionRequest:
        """Load one action by id or unique prefix.

        Raises:
            ActionRequestNotFound: no such action.
            AmbiguousId: a prefix matched several actions.
        """
        action_id = (
            reference
            if not isinstance(reference, str)
            else await self._actions.resolve_action_id(reference)
        )
        action = await self._actions.get_action(action_id)
        if action is None:
            raise ActionRequestNotFound(reference)
        return action

    async def overview(self, reference: ActionRequestId | str) -> ActionOverview:
        """Return one action with its approval and execution situation."""
        action = await self.require_action(reference)
        return await self._describe(action)

    async def list_overviews(
        self, *, case_id: UUID | None = None, limit: int | None = 20
    ) -> list[ActionOverview]:
        """List actions, newest first, each with its current situation."""
        actions = await self._actions.list_actions(case_id=case_id, limit=limit)
        return [await self._describe(action) for action in actions]

    async def approval_state_of(self, action: ActionRequest) -> ApprovalState:
        """The approval word for one action, computed from the stored records."""
        return _approval_state(
            await self._actions.latest_approval(action.id), self._clock.now()
        )

    async def _describe(self, action: ActionRequest) -> ActionOverview:
        approval = await self._actions.latest_approval(action.id)
        return ActionOverview(
            action=action,
            approval_state=_approval_state(approval, self._clock.now()),
            approval=approval,
            execution=await self._actions.latest_execution(action.id),
            execution_count=await self._actions.count_executions(action.id),
        )


def _approval_state(approval: ApprovalRecord | None, now: datetime) -> ApprovalState:
    """One word for the authority an approval still carries."""
    if approval is None:
        return ApprovalState.NONE
    if approval.is_consumed():
        return ApprovalState.CONSUMED
    if approval.is_superseded():
        return ApprovalState.SUPERSEDED
    if approval.is_expired(now):
        return ApprovalState.EXPIRED
    return ApprovalState.VALID


def execution_summary(run: ExecutionRun | None) -> str:
    """A short, honest phrase for one execution, used by the CLI."""
    if run is None:
        return "none"
    if run.status is ExecutionRunStatus.RUNNING:
        return "running (an unresolved attempt blocks a new one)"
    if run.status is ExecutionRunStatus.UNKNOWN:
        return "unknown (the effect may or may not have happened; do not retry blindly)"
    return run.status.value


__all__ = [
    "ActionOverview",
    "ActionService",
    "ApprovalState",
    "execution_summary",
]
