"""Facts a conversation may propose, and the confirmation it may never perform (ADR-0038).

```text
fact.propose ──► Correction + FactCandidate(PENDING)      ← the durable review
                       │
        「确认记住」 ────┴──► LearningService.confirm_fact ← bypasses ModelPort
```

This module owns no table and no second fact model: it is a thin, deterministic orchestration over
the Phase 7A `LearningService`. What it adds is the conversational policy that service should not
have — that a new proposal for a key resolves an earlier *pending* proposal for the same key (a
correction must not leave two live candidates), and that pending candidates are read back in a
bounded, presentation-ready shape.

Nothing here is reachable by a model for confirmation. The model may name `fact.propose`; the final
`confirm` call is made by the runtime from the human's own turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant.application.learning_service import FactOverview, LearningService
from assistant.domain.correction import CorrectionId
from assistant.domain.fact import (
    ConfirmedFact,
    FactCandidate,
    FactCandidateId,
    FactCandidateStatus,
    FactKey,
)
from assistant.ports.learning_repository import FactConfirmation

MAX_PENDING_FACT_REVIEWS = 12
"""How many pending candidates one confirmation attempt or one listing may consider."""

FACT_CONFIRM_PHRASES = frozenset({"确认记住", "记住", "确认保存", "确认记录"})
"""The entire accepted vocabulary for confirming one long-term fact (ADR-0038 §7).

Deliberately its own list: a generic "可以" confirms a local plan, and it must never also mean
"remember this about me forever". Matching is on the whole message, so "记住我的办公室在仙林" is a
*proposal*, not a confirmation.
"""

FACT_CANCEL_PHRASES = frozenset({"不要记", "别记", "不记", "取消", "cancel"})
"""The entire accepted vocabulary for refusing one pending fact review (ADR-0038 §8)."""

FACT_CONFIRMATION_INTENT_CONFIRM = "confirm"
FACT_CONFIRMATION_INTENT_CANCEL = "cancel"


def fact_confirmation_intent(text: str) -> tuple[str, str | None] | None:
    """`(intent, key_or_None)` when the message is exactly a fact phrase, else `None`.

    An optional trailing key lets a person answer the "which one?" question without a model:
    `确认记住 profile.office`. Anything longer than a phrase plus a key is an ordinary message and
    is interpreted normally, which is what keeps a sentence like "记住我的办公室在仙林" a proposal.
    """
    cleaned = text.strip().rstrip("。.!！")
    if not cleaned:
        return None
    folded = cleaned.lower()
    if folded in FACT_CONFIRM_PHRASES:
        return (FACT_CONFIRMATION_INTENT_CONFIRM, None)
    if folded in FACT_CANCEL_PHRASES:
        return (FACT_CONFIRMATION_INTENT_CANCEL, None)
    head, _, tail = cleaned.partition(" ")
    key = tail.strip()
    if key and head.lower() in FACT_CONFIRM_PHRASES:
        return (FACT_CONFIRMATION_INTENT_CONFIRM, key)
    if key and head.lower() in FACT_CANCEL_PHRASES:
        return (FACT_CONFIRMATION_INTENT_CANCEL, key)
    return None


@dataclass(frozen=True, slots=True)
class PendingFactReview:
    """One candidate waiting for a human, with the words that justified it."""

    candidate: FactCandidate
    correction_text: str

    @property
    def fact_key(self) -> FactKey:
        """The key this proposal is about."""
        return self.candidate.fact_key


@dataclass(frozen=True, slots=True)
class FactProposalResult:
    """The candidate a turn just proposed, and the sentence it came from."""

    candidate: FactCandidate
    correction_id: CorrectionId
    correction_text: str
    superseded_pending: tuple[FactCandidate, ...] = ()


class ConversationalFactService:
    """Propose facts from a conversation, list them, and confirm exactly one at a time."""

    def __init__(self, learning: LearningService) -> None:
        self._learning = learning

    async def propose(
        self, *, key: FactKey, value: str, correction_text: str
    ) -> FactProposalResult:
        """Store a correction and the candidate it justifies; never confirms anything.

        A proposal for a key that already has a *pending* candidate resolves that one as rejected
        first, so the user is never asked to confirm a value they have already corrected. Nothing
        that was ever confirmed is touched here.

        Raises:
            InvalidCorrection: the sentence is blank or too long.
            InvalidFactKey: the key is malformed.
            ForbiddenFactKey: the key names a credential.
            InvalidFactCandidate: the value or the validity window is unusable.
        """
        superseded = await self._reject_pending_for_key(key)
        proposal = await self._learning.propose_fact(key, value, correction_text)
        return FactProposalResult(
            candidate=proposal.candidate,
            correction_id=proposal.correction.id,
            correction_text=proposal.correction.text,
            superseded_pending=superseded,
        )

    async def pending(self, *, limit: int = MAX_PENDING_FACT_REVIEWS) -> list[PendingFactReview]:
        """Candidates waiting for a human, newest first."""
        candidates = await self._learning.list_candidates(
            statuses=(FactCandidateStatus.PENDING,), limit=limit
        )
        reviews: list[PendingFactReview] = []
        for candidate in candidates:
            detail = await self._learning.get_candidate(candidate.id)
            reviews.append(
                PendingFactReview(
                    candidate=candidate, correction_text=detail.correction.text
                )
            )
        return reviews

    async def confirm(self, candidate_id: FactCandidateId | str) -> FactConfirmation:
        """Promote one pending candidate. This is the only confirmation path in a conversation.

        Raises:
            FactCandidateNotFound: no such candidate.
            InvalidFactCandidateTransition: it was already confirmed or rejected.
            ExpiredFactCandidate: its validity window has already closed.
        """
        return await self._learning.confirm_fact(candidate_id)

    async def reject(self, candidate_id: FactCandidateId | str) -> FactCandidate:
        """Resolve one pending candidate as rejected, keeping it as an audit record."""
        return await self._learning.reject_fact(candidate_id)

    async def list_facts(
        self, *, include_inactive: bool = False, limit: int | None = 20
    ) -> list[ConfirmedFact]:
        """Confirmed facts, newest first; superseded and expired values only when asked for."""
        return await self._learning.list_facts(
            include_inactive=include_inactive, limit=limit
        )

    async def fact_overviews(
        self, *, limit: int = 20
    ) -> list[FactOverview]:
        """Current confirmed facts with the corrections that justify them."""
        return await self._learning.list_fact_overviews(limit=limit)

    async def show(self, key: FactKey) -> ConfirmedFact | None:
        """The one current confirmed fact for a key, or `None`.

        Raises:
            InvalidFactKey: the key is malformed.
            ForbiddenFactKey: the key names a credential.
        """
        return await self._learning.get_active_fact(key)

    async def _reject_pending_for_key(self, key: FactKey) -> tuple[FactCandidate, ...]:
        """Resolve every pending candidate for one key as rejected, keeping the audit trail."""
        superseded: list[FactCandidate] = []
        for candidate in await self._learning.list_candidates(
            statuses=(FactCandidateStatus.PENDING,), limit=None
        ):
            if candidate.fact_key == key.strip():
                superseded.append(await self._learning.reject_fact(candidate.id))
        return tuple(superseded)


__all__ = [
    "FACT_CANCEL_PHRASES",
    "FACT_CONFIRMATION_INTENT_CANCEL",
    "FACT_CONFIRMATION_INTENT_CONFIRM",
    "FACT_CONFIRM_PHRASES",
    "MAX_PENDING_FACT_REVIEWS",
    "ConversationalFactService",
    "FactProposalResult",
    "PendingFactReview",
    "fact_confirmation_intent",
]
