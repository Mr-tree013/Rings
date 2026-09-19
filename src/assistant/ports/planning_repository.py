"""PlanningRepository port: proposals, consistent snapshots and atomic apply (ADR-0015).

This port exists for two reasons that the commitment port must not be stretched to cover:

- a planning snapshot has to come from **one** database transaction, otherwise a proposal
  could mix task state from revision N with calendar state from revision N+1;
- applying a proposal is a compound write — cancel planner blocks, insert new ones, mark the
  proposal applied, bump the revision — that must be atomic, and the stale branch must commit
  its "stale" marking instead of rolling it back.

Nothing here is generic CRUD: it speaks proposals, snapshots and apply outcomes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from assistant.domain.planning import (
    PlanningIssue,
    PlanningSnapshot,
    PlanningWindow,
    PlanProposal,
    PlanProposalDetail,
    PlanProposalId,
    PlanProposalSummary,
    ProposedPlanBlock,
)


class ApplyOutcome(StrEnum):
    """What applying a proposal did (or refused to do)."""

    APPLIED = "applied"
    STALE = "stale"
    NOT_PENDING = "not_pending"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The outcome of an apply attempt, with its effects."""

    outcome: ApplyOutcome
    proposal: PlanProposal
    created_blocks: int = 0
    replaced_blocks: int = 0


class PlanningRepository(Protocol):
    """Durable planning state and the one atomic planning write."""

    async def load_snapshot(self, window: PlanningWindow) -> PlanningSnapshot:
        """Read all planner-relevant state for `window` in one transaction.

        Includes open tasks, their deadlines and recorded work, active calendar events and
        active plan blocks (manual and planner) that intersect the window, plus the current
        commitment revision.
        """
        ...

    async def create_proposal(
        self,
        proposal: PlanProposal,
        *,
        blocks: Sequence[ProposedPlanBlock],
        issues: Sequence[PlanningIssue],
    ) -> PlanProposal:
        """Persist a proposal, its blocks and its issues in one transaction.

        Also supersedes any pending proposal for the same window, and refuses to store a
        proposal whose `input_revision` is no longer current (`PlanningSnapshotChanged`).
        """
        ...

    async def get_proposal(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        """Return one proposal, or `None`."""
        ...

    async def get_proposal_detail(self, proposal_id: PlanProposalId) -> PlanProposalDetail | None:
        """Return a proposal with the blocks and issues it was created with."""
        ...

    async def list_proposals(self, *, limit: int | None = 20) -> list[PlanProposal]:
        """List proposals, newest first."""
        ...

    async def list_proposal_summaries(
        self, *, limit: int | None = 20
    ) -> list[PlanProposalSummary]:
        """List proposals with their block and issue counts, newest first."""
        ...

    async def mark_stale(self, proposal_id: PlanProposalId) -> PlanProposal | None:
        """Mark a pending proposal stale (no revision change, no plan-block change)."""
        ...

    async def apply_proposal(
        self, proposal_id: PlanProposalId, *, applied_at: datetime
    ) -> ApplyResult:
        """Atomically turn a pending proposal into real planner blocks.

        Raises:
            PlanProposalNotFound: no such proposal.
        """
        ...

    async def resolve_proposal_id(self, reference: str) -> PlanProposalId:
        """Resolve a full UUID or a unique prefix to a proposal id.

        Raises:
            PlanProposalNotFound: nothing matches.
            AmbiguousId: several proposals match; the CLI never guesses.
        """
        ...


__all__ = ["ApplyOutcome", "ApplyResult", "PlanningRepository"]
