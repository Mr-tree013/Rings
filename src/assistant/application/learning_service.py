"""Personal learning as explicit, human-confirmed state (ADR-0027).

The service has one job and one boundary. The job: turn something the user wrote into a durable
candidate, and let them promote or discard it. The boundary: **nothing else in the project can do
either of those things.** There is no method here that a model, a mail handler, a scheduler or a
web route would call — no prompt produces a candidate, no background worker confirms one, and no
consumer reads a fact out of this module yet.

That last point is deliberate rather than unfinished: Phase 7A stores, reviews and queries facts.
It does not inject them into a model context, a mail draft or an eHall form, so a confirmed fact
cannot influence anything until a later phase argues for the specific consumer.

A confirmation copies the candidate's snapshot. There is no parameter anywhere in this module
that could substitute a different key or value, because the value a user approves must be the
value that was reviewed — if the content is wrong, the answer is a new candidate, with its own
provenance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from assistant.domain.correction import Correction, CorrectionId, validate_correction_text
from assistant.domain.errors import (
    ConfirmedFactNotFound,
    CorrectionNotFound,
    FactCandidateNotFound,
)
from assistant.domain.fact import (
    ConfirmedFact,
    ConfirmedFactId,
    FactCandidate,
    FactCandidateId,
    FactCandidateStatus,
    FactKey,
    new_confirmed_fact_id,
    validate_fact_key,
    validate_fact_value,
)
from assistant.ports.clock import Clock
from assistant.ports.learning_repository import FactConfirmation, LearningRepository


@dataclass(frozen=True, slots=True)
class FactProposal:
    """A stored correction together with the candidate it justifies."""

    correction: Correction
    candidate: FactCandidate


@dataclass(frozen=True, slots=True)
class CorrectionDetail:
    """One correction and every candidate it has produced."""

    correction: Correction
    candidates: tuple[FactCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    """One candidate with its provenance, and the fact it became if it was confirmed."""

    candidate: FactCandidate
    correction: Correction
    fact: ConfirmedFact | None = None


@dataclass(frozen=True, slots=True)
class FactDetail:
    """One confirmed fact with the candidate and correction it came from."""

    fact: ConfirmedFact
    candidate: FactCandidate
    correction: Correction


@dataclass(frozen=True, slots=True)
class FactOverview:
    """A confirmed fact with the correction that justifies it, for list output."""

    fact: ConfirmedFact
    correction: Correction | None = None


class LearningService:
    """Durable corrections, candidate facts and human-confirmed facts."""

    def __init__(
        self,
        repository: LearningRepository,
        clock: Clock,
        *,
        fact_id_factory: Callable[[], ConfirmedFactId] = new_confirmed_fact_id,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._fact_id_factory = fact_id_factory

    # ------------------------------------------------------------------ corrections

    async def add_correction(self, text: str) -> Correction:
        """Store one explicit user correction.

        A correction does not have to justify a candidate — the user may simply be recording what
        is correct — so this writes the text on its own.

        Raises:
            InvalidCorrection: the text is blank or too long.
        """
        correction = Correction(text=validate_correction_text(text), created_at=self._now())
        return await self._repository.add_correction(correction)

    async def list_corrections(self, *, limit: int | None = 20) -> list[Correction]:
        """List corrections, newest first."""
        return await self._repository.list_corrections(limit=limit)

    async def get_correction(self, reference: CorrectionId | str) -> CorrectionDetail:
        """Return one correction and the candidates it justified.

        Raises:
            CorrectionNotFound: no such correction.
            AmbiguousId: a prefix matched several corrections.
        """
        correction = await self._require_correction(reference)
        candidates = await self._repository.list_candidates(limit=None)
        return CorrectionDetail(
            correction=correction,
            candidates=tuple(
                item for item in candidates if item.correction_id == correction.id
            ),
        )

    # ------------------------------------------------------------------- candidates

    async def propose_fact(
        self,
        key: FactKey,
        value: str,
        correction_text: str,
        valid_until: datetime | None = None,
    ) -> FactProposal:
        """Store a correction and the candidate it justifies, in one transaction.

        Raises:
            InvalidCorrection: the note is blank or too long.
            InvalidFactKey: the key is not a well-formed namespaced identifier.
            ForbiddenFactKey: the key names a credential.
            InvalidFactCandidate: the value is blank or the validity window is impossible.
        """
        now = self._now()
        correction = Correction(
            text=validate_correction_text(correction_text), created_at=now
        )
        candidate = FactCandidate(
            fact_key=validate_fact_key(key),
            value=validate_fact_value(value),
            correction_id=correction.id,
            created_at=now,
            proposed_valid_until=valid_until,
        )
        stored_correction, stored_candidate = (
            await self._repository.create_candidate_with_correction(
                correction=correction, candidate=candidate
            )
        )
        return FactProposal(correction=stored_correction, candidate=stored_candidate)

    async def list_candidates(
        self,
        *,
        statuses: tuple[FactCandidateStatus, ...] | None = None,
        limit: int | None = 20,
    ) -> list[FactCandidate]:
        """List candidates, newest first. All statuses when none are given."""
        return await self._repository.list_candidates(statuses=statuses, limit=limit)

    async def get_candidate(
        self, reference: FactCandidateId | str
    ) -> CandidateDetail:
        """Return one candidate with its source correction and any fact it produced.

        Raises:
            FactCandidateNotFound: no such candidate.
            AmbiguousId: a prefix matched several candidates.
        """
        candidate = await self._require_candidate(reference)
        correction = await self._repository.get_correction(candidate.correction_id)
        if correction is None:  # pragma: no cover - the foreign key forbids it
            raise ConfirmedFactNotFound(candidate.correction_id)
        facts = await self._repository.list_confirmed_facts(limit=None)
        fact = next((item for item in facts if item.candidate_id == candidate.id), None)
        return CandidateDetail(candidate=candidate, correction=correction, fact=fact)

    # ------------------------------------------------------- the human-only boundary

    async def confirm_fact(self, candidate_id: FactCandidateId | str) -> FactConfirmation:
        """Promote one pending candidate to a confirmed fact.

        This is the only path in the project that creates a `ConfirmedFact`. It is reachable from
        one CLI command and from nothing else.

        Raises:
            FactCandidateNotFound: no such candidate.
            InvalidFactCandidateTransition: the candidate is already confirmed or rejected.
            ExpiredFactCandidate: the proposed validity window has already closed.
        """
        resolved = await self._require_candidate_id(candidate_id)
        return await self._repository.confirm_candidate(
            candidate_id=resolved, fact_id=self._fact_id_factory(), now=self._now()
        )

    async def reject_fact(self, candidate_id: FactCandidateId | str) -> FactCandidate:
        """Reject one pending candidate, keeping it as an audit record.

        Raises:
            FactCandidateNotFound: no such candidate.
            InvalidFactCandidateTransition: the candidate is already confirmed or rejected.
        """
        resolved = await self._require_candidate_id(candidate_id)
        return await self._repository.reject_candidate(
            candidate_id=resolved, now=self._now()
        )

    # ------------------------------------------------------------------ facts

    async def list_facts(
        self, *, include_inactive: bool = False, limit: int | None = 20
    ) -> list[ConfirmedFact]:
        """List confirmed facts, newest first.

        By default only facts that are current *and* unexpired are returned: a superseded or
        expired value is history, and history is what `include_inactive` is for.
        """
        if include_inactive:
            return await self._repository.list_confirmed_facts(limit=limit)
        return await self._repository.list_active_facts(now=self._now(), limit=limit)

    async def get_fact(self, reference: ConfirmedFactId | str) -> FactDetail:
        """Return one confirmed fact with its provenance.

        Raises:
            ConfirmedFactNotFound: no such fact.
            AmbiguousId: a prefix matched several facts.
        """
        fact = await self._require_fact(reference)
        candidate = await self._repository.get_candidate(fact.candidate_id)
        if candidate is None:  # pragma: no cover - the foreign key forbids it
            raise FactCandidateNotFound(fact.candidate_id)
        correction = await self._repository.get_correction(candidate.correction_id)
        if correction is None:  # pragma: no cover - the foreign key forbids it
            raise ConfirmedFactNotFound(candidate.correction_id)
        return FactDetail(fact=fact, candidate=candidate, correction=correction)

    async def list_fact_overviews(
        self, *, include_inactive: bool = False, limit: int | None = 20
    ) -> list[FactOverview]:
        """List confirmed facts with their provenance, newest first.

        The join is done here rather than in the caller so that listing facts stays three reads
        whatever the list length, and so nothing outside this service has to know how provenance
        is stored.
        """
        facts = await self.list_facts(include_inactive=include_inactive, limit=limit)
        if not facts:
            return []
        candidates = {
            item.id: item for item in await self._repository.list_candidates(limit=None)
        }
        corrections = {
            item.id: item for item in await self._repository.list_corrections(limit=None)
        }
        overviews: list[FactOverview] = []
        for fact in facts:
            candidate = candidates.get(fact.candidate_id)
            correction = (
                None
                if candidate is None
                else corrections.get(candidate.correction_id)
            )
            overviews.append(FactOverview(fact=fact, correction=correction))
        return overviews

    async def get_active_fact(self, key: FactKey) -> ConfirmedFact | None:
        """The one fact a future consumer may use for this key, or `None`.

        This is the read a later phase would call. It exists now so that the rule — current and
        unexpired, or nothing — has exactly one implementation.
        """
        return await self._repository.get_active_fact_by_key(
            validate_fact_key(key), now=self._now()
        )

    # -------------------------------------------------------------------- helpers

    def _now(self) -> datetime:
        return self._clock.now()

    async def _require_correction(self, reference: CorrectionId | str) -> Correction:
        correction_id = (
            reference
            if not isinstance(reference, str)
            else await self._repository.resolve_correction_id(reference)
        )
        correction = await self._repository.get_correction(correction_id)
        if correction is None:
            raise CorrectionNotFound(correction_id)
        return correction

    async def _require_candidate(self, reference: FactCandidateId | str) -> FactCandidate:
        candidate_id = await self._require_candidate_id(reference)
        candidate = await self._repository.get_candidate(candidate_id)
        if candidate is None:
            raise FactCandidateNotFound(candidate_id)
        return candidate

    async def _require_candidate_id(
        self, reference: FactCandidateId | str
    ) -> FactCandidateId:
        if not isinstance(reference, str):
            return reference
        return await self._repository.resolve_candidate_id(reference)

    async def _require_fact(self, reference: ConfirmedFactId | str) -> ConfirmedFact:
        fact_id = (
            reference
            if not isinstance(reference, str)
            else await self._repository.resolve_confirmed_fact_id(reference)
        )
        fact = await self._repository.get_confirmed_fact(fact_id)
        if fact is None:
            raise ConfirmedFactNotFound(fact_id)
        return fact


__all__ = [
    "CandidateDetail",
    "CorrectionDetail",
    "FactDetail",
    "FactOverview",
    "FactProposal",
    "LearningService",
]
