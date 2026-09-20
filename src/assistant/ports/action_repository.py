"""ActionRepository port: actions, approvals and executions as one authority (ADR-0023).

These five tables are one port rather than five because the two decisions that matter are atomic
across all of them:

- **redeeming a challenge** verifies the challenge, re-checks the action's fingerprint, supersedes
  an expired approval and inserts the new one — in one transaction, so a crash cannot leave a
  token spent without an approval;
- **beginning an execution** re-checks the fingerprint, finds the exact approval, consumes it and
  creates the `RUNNING` run — in one transaction, so two callers cannot both spend the same
  approval.

An application service that stitched two commits together would produce the half-states this port
exists to prevent. Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from assistant.domain.action import (
    ActionRequest,
    ActionRequestId,
    ActionRequestStatus,
)
from assistant.domain.approval import (
    ApprovalChallenge,
    ApprovalChallengeId,
    ApprovalId,
    ApprovalRecord,
)
from assistant.domain.case import CaseId
from assistant.domain.execution import ExecutionOutcome, ExecutionRun, ExecutionRunId


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """The approval a redeemed challenge produced, and how many old ones it retired."""

    approval: ApprovalRecord
    superseded: int = 0


@dataclass(frozen=True, slots=True)
class StartedExecution:
    """The run that was started, plus the approval it consumed."""

    run: ExecutionRun
    approval: ApprovalRecord


class ActionRepository(Protocol):
    """Durable actions, approval challenges, approvals and execution runs."""

    # ------------------------------------------------------------------ actions

    async def add_action(self, action: ActionRequest) -> ActionRequest:
        """Store a new prepared action. Its payload is never writable again."""
        ...

    async def get_action(self, action_id: ActionRequestId) -> ActionRequest | None:
        """Return one action, or `None`."""
        ...

    async def list_actions(
        self, *, case_id: CaseId | None = None, limit: int | None = 20
    ) -> list[ActionRequest]:
        """List actions, newest first."""
        ...

    async def resolve_action_id(self, reference: str) -> ActionRequestId:
        """Resolve a full UUID or a unique prefix to an action id.

        Raises:
            ActionRequestNotFound: nothing matches.
            AmbiguousId: several actions match.
        """
        ...

    async def count_actions(
        self, *, statuses: Collection[ActionRequestStatus] | None = None
    ) -> int:
        """How many actions exist in the given statuses (all of them when omitted)."""
        ...

    async def cancel_action(self, action: ActionRequest) -> ActionRequest:
        """Move one PREPARED action to CANCELLED, if it still is PREPARED.

        Raises:
            ActionRequestNotFound: no such action.
            ActionNotExecutable: the action is already terminal.
        """
        ...

    # ---------------------------------------------------------------- approvals

    async def add_challenge(self, challenge: ApprovalChallenge) -> ApprovalChallenge:
        """Store one challenge. Only its token hash is written."""
        ...

    async def get_challenge(
        self, challenge_id: ApprovalChallengeId
    ) -> ApprovalChallenge | None:
        """Return one challenge, or `None`."""
        ...

    async def latest_challenge(
        self, action_id: ActionRequestId
    ) -> ApprovalChallenge | None:
        """Return the most recently created challenge for an action, or `None`."""
        ...

    async def redeem_challenge(
        self,
        *,
        challenge_id: ApprovalChallengeId,
        token_hash: str,
        action_id: ActionRequestId,
        action_fingerprint: str,
        approval_id: ApprovalId,
        now: datetime,
    ) -> ApprovalGrant:
        """Spend one challenge and record the approval it authorises, atomically.

        Verifies the token hash, the expiry, the single use, the action's status and the exact
        fingerprint; supersedes any expired outstanding approval; refuses when a live approval
        already exists.

        Raises:
            ApprovalChallengeNotFound: no such challenge.
            InvalidApprovalToken: the hash does not match.
            ApprovalChallengeExpired: past its expiry.
            ApprovalChallengeConsumed: already redeemed.
            ActionRequestNotFound: the action is gone.
            ActionFingerprintMismatch: the action no longer hashes to the approved fingerprint.
            ApprovalAlreadyOutstanding: a live approval already exists for this action.
        """
        ...

    async def list_approvals(self, action_id: ActionRequestId) -> list[ApprovalRecord]:
        """List every approval ever recorded for an action, oldest first."""
        ...

    async def latest_approval(self, action_id: ActionRequestId) -> ApprovalRecord | None:
        """Return the most recently approved record for an action, or `None`."""
        ...

    # --------------------------------------------------------------- executions

    async def begin_execution(
        self,
        *,
        action_id: ActionRequestId,
        action_fingerprint: str,
        run_id: ExecutionRunId,
        now: datetime,
    ) -> StartedExecution:
        """Consume the exact approval and start a run, atomically.

        Raises:
            ActionRequestNotFound: no such action.
            ActionNotExecutable: the action is not PREPARED.
            ActionFingerprintMismatch: the payload no longer matches its fingerprint.
            ActionExecutionUnresolved: an earlier attempt has no decidable outcome.
            ApprovalUnavailable: no usable approval exists (including when another caller
                consumed the only one first).
        """
        ...

    async def finish_execution(
        self,
        *,
        run_id: ExecutionRunId,
        outcome: ExecutionOutcome,
        at: datetime,
    ) -> ExecutionRun:
        """Finish a RUNNING run, marking the action EXECUTED on success, atomically.

        Raises:
            ExecutionRunNotFound: no such run.
            InvalidExecutionRun: the run already ended.
        """
        ...

    async def get_execution(self, run_id: ExecutionRunId) -> ExecutionRun | None:
        """Return one execution run, or `None`."""
        ...

    async def list_executions(self, action_id: ActionRequestId) -> list[ExecutionRun]:
        """List every execution attempt for an action, oldest first."""
        ...

    async def latest_execution(self, action_id: ActionRequestId) -> ExecutionRun | None:
        """Return the most recent execution attempt for an action, or `None`."""
        ...

    async def count_executions(self, action_id: ActionRequestId) -> int:
        """How many attempts have been started for one action."""
        ...


__all__ = ["ActionRepository", "ApprovalGrant", "StartedExecution"]
