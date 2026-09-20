"""LearningRepository port: corrections, candidates and confirmed facts (ADR-0027).

Two operations carry the safety of this phase, and both run inside one `BEGIN IMMEDIATE`
transaction:

- **`create_candidate_with_correction`** — the user's text and the proposal it justifies are
  written together, so a candidate can never exist without the sentence that produced it (and a
  correction can never be committed with nothing to show for it);
- **`confirm_candidate`** — the previous current fact for the key is retired, the new fact is
  inserted and the candidate's single status transition is recorded in the same transaction. A
  failure rolls all three back, which is what makes "history is retired, never deleted" and "at
  most one current fact per key" true *simultaneously* rather than on average.

Nothing here exposes a connection, a transaction or SQL, and nothing here can be reached by a
model, a worker or a schedule: the only caller is the learning service behind an explicit
command.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from assistant.domain.correction import Correction, CorrectionId
from assistant.domain.fact import (
    ConfirmedFact,
    ConfirmedFactId,
    FactCandidate,
    FactCandidateId,
    FactCandidateStatus,
    FactKey,
)


@dataclass(frozen=True, slots=True)
class FactConfirmation:
    """One confirmation: the fact it created plus whatever it retired for that key."""

    fact: ConfirmedFact
    superseded: ConfirmedFact | None = None


class LearningRepository(Protocol):
    """Durable corrections, candidate facts and confirmed personal facts."""

    async def add_correction(self, correction: Correction) -> Correction:
        """Store one correction on its own, with no candidate attached."""
        ...

    async def create_candidate_with_correction(
        self, *, correction: Correction, candidate: FactCandidate
    ) -> tuple[Correction, FactCandidate]:
        """Store a user correction and the candidate it justifies, atomically.

        Raises:
            CommitmentStoreError: the candidate does not reference the correction, or the write
                failed. Neither row is committed in that case.
        """
        ...

    async def get_correction(self, correction_id: CorrectionId) -> Correction | None:
        """Return one correction, or `None`."""
        ...

    async def list_corrections(self, *, limit: int | None = 20) -> list[Correction]:
        """List corrections, newest first."""
        ...

    async def resolve_correction_id(self, reference: str) -> CorrectionId:
        """Resolve a full UUID or a unique prefix to a correction id.

        Raises:
            CorrectionNotFound: nothing matches.
            AmbiguousId: several corrections match.
        """
        ...

    async def get_candidate(self, candidate_id: FactCandidateId) -> FactCandidate | None:
        """Return one candidate, or `None`."""
        ...

    async def list_candidates(
        self,
        *,
        statuses: Collection[FactCandidateStatus] | None = None,
        limit: int | None = 20,
    ) -> list[FactCandidate]:
        """List candidates, newest first, optionally filtered by status."""
        ...

    async def resolve_candidate_id(self, reference: str) -> FactCandidateId:
        """Resolve a full UUID or a unique prefix to a candidate id.

        Raises:
            FactCandidateNotFound: nothing matches.
            AmbiguousId: several candidates match.
        """
        ...

    async def confirm_candidate(
        self,
        *,
        candidate_id: FactCandidateId,
        fact_id: ConfirmedFactId,
        now: datetime,
    ) -> FactConfirmation:
        """Promote one pending candidate, retiring the key's previous current fact.

        The candidate's own snapshot supplies the key, the value and the proposed expiry; there is
        no parameter that could substitute a different value.

        Raises:
            FactCandidateNotFound: no such candidate.
            InvalidFactCandidateTransition: the candidate is already resolved.
            ExpiredFactCandidate: the proposed validity window has already closed.
        """
        ...

    async def reject_candidate(
        self, *, candidate_id: FactCandidateId, now: datetime
    ) -> FactCandidate:
        """Reject one pending candidate, keeping it as an audit record.

        Raises:
            FactCandidateNotFound: no such candidate.
            InvalidFactCandidateTransition: the candidate is already resolved.
        """
        ...

    async def get_confirmed_fact(self, fact_id: ConfirmedFactId) -> ConfirmedFact | None:
        """Return one confirmed fact, or `None`. Superseded and expired rows included."""
        ...

    async def list_confirmed_facts(self, *, limit: int | None = None) -> list[ConfirmedFact]:
        """List every confirmed fact, newest first, including retired history."""
        ...

    async def resolve_confirmed_fact_id(self, reference: str) -> ConfirmedFactId:
        """Resolve a full UUID or a unique prefix to a confirmed fact id.

        Raises:
            ConfirmedFactNotFound: nothing matches.
            AmbiguousId: several facts match.
        """
        ...

    async def get_active_fact_by_key(
        self, fact_key: FactKey, *, now: datetime
    ) -> ConfirmedFact | None:
        """The one fact that may be used for this key at `now`: current and unexpired."""
        ...

    async def list_active_facts(
        self, *, now: datetime, limit: int | None = None
    ) -> list[ConfirmedFact]:
        """Every fact that is current and unexpired at `now`, newest first."""
        ...


__all__ = ["FactConfirmation", "LearningRepository"]
