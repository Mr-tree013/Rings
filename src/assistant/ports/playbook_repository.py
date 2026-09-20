"""PlaybookRepository port: candidates, replay tests and promoted playbooks (ADR-0028).

Two operations are atomic, and they are the two that decide what the audit record ends up saying:

- **`promote_candidate`** runs one `BEGIN IMMEDIATE` that re-checks the candidate is still pending,
  finds the newest recorded dry run that passed *with the current contract version and this exact
  candidate snapshot*, inserts the playbook and records the candidate's single transition. A
  failure rolls all of it back, so a promoted candidate without its playbook cannot exist;
- **`retire_playbook`** is a compare-and-set on the status, so two callers cannot both think they
  retired the same reference.

Nothing here executes anything, and nothing here can write an `action_requests`, `approvals` or
`execution_runs` row: those tables are Phase 6A's, and a playbook only ever *references* them.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Protocol

from assistant.domain.playbook import (
    Playbook,
    PlaybookCandidate,
    PlaybookCandidateId,
    PlaybookCandidateStatus,
    PlaybookId,
    PlaybookReplayTest,
    PlaybookReplayTestId,
    PlaybookStatus,
)


class PlaybookRepository(Protocol):
    """Durable candidates, replay tests and playbooks."""

    async def create_candidate(self, candidate: PlaybookCandidate) -> PlaybookCandidate:
        """Store one candidate for one successful action.

        Raises:
            PlaybookCandidateExists: this action already seeded a candidate.
        """
        ...

    async def get_candidate(
        self, candidate_id: PlaybookCandidateId
    ) -> PlaybookCandidate | None:
        """Return one candidate, or `None`."""
        ...

    async def list_candidates(
        self,
        *,
        statuses: Collection[PlaybookCandidateStatus] | None = None,
        limit: int | None = 20,
    ) -> list[PlaybookCandidate]:
        """List candidates, newest first, optionally filtered by status."""
        ...

    async def resolve_candidate_id(self, reference: str) -> PlaybookCandidateId:
        """Resolve a full UUID or a unique prefix to a candidate id.

        Raises:
            PlaybookCandidateNotFound: nothing matches.
            AmbiguousId: several candidates match.
        """
        ...

    async def add_replay_test(
        self, test: PlaybookReplayTest
    ) -> PlaybookReplayTest:
        """Append one recorded dry run. Tests are never updated or deleted."""
        ...

    async def list_replay_tests(
        self, candidate_id: PlaybookCandidateId
    ) -> list[PlaybookReplayTest]:
        """List one candidate's dry runs, newest first."""
        ...

    async def latest_qualifying_test(
        self,
        *,
        candidate_id: PlaybookCandidateId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
    ) -> PlaybookReplayTest | None:
        """The newest passed dry run that qualifies for promotion, or `None`.

        "Qualifies" is exact: same candidate, same action type, same contract version, same input
        fingerprint, status `PASSED`. Ordered by `tested_at DESC, id DESC`.
        """
        ...

    async def promote_candidate(
        self,
        *,
        candidate_id: PlaybookCandidateId,
        playbook_id: PlaybookId,
        action_type: str,
        contract_version: int,
        input_fingerprint: str,
        now: datetime,
    ) -> Playbook:
        """Promote one pending candidate into a playbook, atomically.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            InvalidPlaybookCandidateTransition: the candidate is already resolved.
            PlaybookCandidateNotTested: no dry run qualifies for promotion.
        """
        ...

    async def reject_candidate(
        self, *, candidate_id: PlaybookCandidateId, now: datetime
    ) -> PlaybookCandidate:
        """Reject one pending candidate, keeping it as an audit record.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            InvalidPlaybookCandidateTransition: the candidate is already resolved.
        """
        ...

    async def get_playbook(self, playbook_id: PlaybookId) -> Playbook | None:
        """Return one playbook, or `None`. Retired playbooks included."""
        ...

    async def list_playbooks(
        self,
        *,
        statuses: Collection[PlaybookStatus] | None = None,
        limit: int | None = None,
    ) -> list[Playbook]:
        """List playbooks, newest first, optionally filtered by status."""
        ...

    async def resolve_playbook_id(self, reference: str) -> PlaybookId:
        """Resolve a full UUID or a unique prefix to a playbook id.

        Raises:
            PlaybookNotFound: nothing matches.
            AmbiguousId: several playbooks match.
        """
        ...

    async def playbook_for_candidate(
        self, candidate_id: PlaybookCandidateId
    ) -> Playbook | None:
        """The playbook a candidate produced, or `None`."""
        ...

    async def get_replay_test(
        self, test_id: PlaybookReplayTestId
    ) -> PlaybookReplayTest | None:
        """Return one recorded dry run, or `None`."""
        ...

    async def retire_playbook(
        self, *, playbook_id: PlaybookId, now: datetime
    ) -> Playbook:
        """Move one active playbook to retired. It is never deleted.

        Raises:
            PlaybookNotFound: no such playbook.
            InvalidPlaybookTransition: the playbook is already retired.
        """
        ...


__all__ = ["PlaybookRepository"]
