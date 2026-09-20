"""Reviewing what worked, without ever doing it again (ADR-0028).

```text
pw playbook candidate add ACTION --name ... --note ...
        │  read the action and the SUCCEEDED run it came from, re-hash the payload
        ▼
   PlaybookCandidate (pending)          ── no approval, no execution, no side effect
        │
pw playbook candidate test CANDIDATE
        │  hand the exact approved payload to a pure validator for its action type
        ▼
   PlaybookReplayTest (passed / failed)  ── never ActionExecutor, never a socket
        │
pw playbook candidate promote CANDIDATE
        │  require a PASS at the current contract version for this exact snapshot
        ▼
      Playbook (active)                 ── a reference blueprint, not a capability
```

Three boundaries shape this service, and each of them is a decision rather than an omission:

- **it never creates a candidate by itself.** A successful execution is history; turning history
  into a lesson is a person's choice, made by running a command.
- **it cannot execute anything.** The service has no executor, no executor registry and no
  approval service: testing calls a *pure* validator, and promoting writes one row.
- **a pass is narrow.** It says the current code still parses the historical payload — not that the
  credentials work, that the page is unchanged, or that repeating the action is wise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from assistant.application.playbook_replay.registry import PlaybookReplayRegistry
from assistant.domain.action import (
    ActionRequest,
    ActionRequestId,
    ActionRequestStatus,
    ActionType,
)
from assistant.domain.errors import (
    ActionRequestNotFound,
    InvalidPlaybookCandidateTransition,
    PlaybookCandidateNotFound,
    PlaybookNotFound,
    PlaybookSourceIntegrityError,
    PlaybookSourceNotEligible,
    PlaybookSourceUnsupported,
)
from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
from assistant.domain.playbook import (
    Playbook,
    PlaybookCandidate,
    PlaybookCandidateId,
    PlaybookCandidateStatus,
    PlaybookId,
    PlaybookReplayTest,
    PlaybookStatus,
    new_playbook_id,
    new_playbook_replay_test_id,
    replay_input_fingerprint,
)
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock
from assistant.ports.playbook_replay import PlaybookReplayValidator
from assistant.ports.playbook_repository import PlaybookRepository

LOGGER = logging.getLogger("assistant.playbooks")


@dataclass(frozen=True, slots=True)
class CandidateDetail:
    """One candidate with the dry runs it has accumulated and the playbook it produced."""

    candidate: PlaybookCandidate
    tests: tuple[PlaybookReplayTest, ...] = ()
    playbook: Playbook | None = None


@dataclass(frozen=True, slots=True)
class PlaybookDetail:
    """One playbook with the candidate and the qualifying dry run behind it."""

    playbook: Playbook
    candidate: PlaybookCandidate
    promotion_test: PlaybookReplayTest | None = None


class PlaybookService:
    """Human-created candidates, side-effect-free dry runs and human promotion."""

    def __init__(
        self,
        playbooks: PlaybookRepository,
        actions: ActionRepository,
        clock: Clock,
        *,
        replay: PlaybookReplayRegistry | None = None,
    ) -> None:
        self._playbooks = playbooks
        self._actions = actions
        self._clock = clock
        self._replay = replay if replay is not None else PlaybookReplayRegistry()

    # ------------------------------------------------------------------ candidates

    async def create_candidate(
        self, action: ActionRequestId | str, *, name: str, note: str
    ) -> PlaybookCandidate:
        """Name one successful action as a candidate for review.

        The source is checked properly rather than trusted: an `EXECUTED` action whose payload
        still re-hashes, plus a `SUCCEEDED` run that belongs to it, and a validator for its type.

        Raises:
            PlaybookSourceNotEligible: the action is not a definitive success.
            PlaybookSourceIntegrityError: the stored payload no longer matches its fingerprint.
            PlaybookSourceUnsupported: no replay validator exists for this action type.
            PlaybookCandidateExists: this action already seeded a candidate.
        """
        action_id = await self._resolve_action(action)
        stored = await self._require_action(action_id)
        run = self._require_success(stored, await self._success_runs(action_id))
        self._require_supported(stored.action_type)
        now = self._clock.now()
        return await self._playbooks.create_candidate(
            PlaybookCandidate(
                name=name,
                note=note,
                source_action_id=stored.id,
                source_execution_run_id=run.id,
                source_action_type=stored.action_type,
                source_action_fingerprint=stored.fingerprint,
                created_at=now,
            )
        )

    async def list_candidates(
        self,
        *,
        statuses: tuple[PlaybookCandidateStatus, ...] | None = None,
        limit: int | None = 20,
    ) -> list[PlaybookCandidate]:
        """List candidates, newest first. All statuses when none are given."""
        return await self._playbooks.list_candidates(statuses=statuses, limit=limit)

    async def get_candidate(
        self, reference: PlaybookCandidateId | str
    ) -> CandidateDetail:
        """Return one candidate, its dry runs and any playbook it produced.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            AmbiguousId: a prefix matched several candidates.
        """
        candidate = await self._require_candidate(reference)
        return CandidateDetail(
            candidate=candidate,
            tests=tuple(await self._playbooks.list_replay_tests(candidate.id)),
            playbook=await self._playbooks.playbook_for_candidate(candidate.id),
        )

    # ----------------------------------------------------------------- dry runs

    async def test_candidate(
        self, reference: PlaybookCandidateId | str
    ) -> PlaybookReplayTest:
        """Dry-run one candidate's exact historical payload. No side effect is attempted.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            InvalidPlaybookCandidateTransition: the candidate is already resolved.
            PlaybookReplayUnsupported: no validator exists for its action type.
            PlaybookSourceIntegrityError: the source no longer matches the candidate snapshot.
        """
        candidate = await self._require_candidate(reference)
        if not candidate.is_pending:
            raise InvalidPlaybookCandidateTransition(
                candidate.id, candidate.status, "tested"
            )
        action, _ = await self._revalidate_source(candidate)
        validator = self._require_validator(candidate.source_action_type)
        result = validator.validate(action)
        test = PlaybookReplayTest(
            id=new_playbook_replay_test_id(),
            candidate_id=candidate.id,
            action_type=validator.action_type,
            contract_version=validator.contract_version,
            input_fingerprint=self._input_fingerprint(candidate, validator),
            status=result.status,
            issue_codes=result.issue_codes,
            tested_at=self._clock.now(),
        )
        stored = await self._playbooks.add_replay_test(test)
        LOGGER.info(
            "playbook replay test %s recorded for candidate %s (%s)",
            stored.id,
            candidate.id,
            stored.status.value,
        )
        return stored

    # ------------------------------------------------------------------ promotion

    async def promote_candidate(
        self, reference: PlaybookCandidateId | str
    ) -> Playbook:
        """Promote a candidate that has passed a dry run under the current contract.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            InvalidPlaybookCandidateTransition: the candidate is already resolved.
            PlaybookCandidateNotTested: no qualifying dry run exists.
            PlaybookSourceIntegrityError: the source no longer matches the snapshot.
        """
        candidate = await self._require_candidate(reference)
        await self._revalidate_source(candidate)
        validator = self._require_validator(candidate.source_action_type)
        return await self._playbooks.promote_candidate(
            candidate_id=candidate.id,
            playbook_id=new_playbook_id(),
            action_type=validator.action_type.value,
            contract_version=validator.contract_version,
            input_fingerprint=self._input_fingerprint(candidate, validator),
            now=self._clock.now(),
        )

    async def reject_candidate(
        self, reference: PlaybookCandidateId | str
    ) -> PlaybookCandidate:
        """Reject one candidate, keeping it as an audit record.

        Raises:
            PlaybookCandidateNotFound: no such candidate.
            InvalidPlaybookCandidateTransition: the candidate is already resolved.
        """
        candidate = await self._require_candidate(reference)
        return await self._playbooks.reject_candidate(
            candidate_id=candidate.id, now=self._clock.now()
        )

    # ------------------------------------------------------------------ playbooks

    async def list_playbooks(
        self,
        *,
        statuses: tuple[PlaybookStatus, ...] | None = None,
        limit: int | None = 20,
    ) -> list[Playbook]:
        """List playbooks, newest first. All statuses when none are given."""
        return await self._playbooks.list_playbooks(statuses=statuses, limit=limit)

    async def get_playbook(self, reference: PlaybookId | str) -> PlaybookDetail:
        """Return one playbook with its candidate and the dry run that qualified it.

        Raises:
            PlaybookNotFound: no such playbook.
            AmbiguousId: a prefix matched several playbooks.
        """
        playbook = await self._require_playbook(reference)
        candidate = await self._playbooks.get_candidate(playbook.candidate_id)
        if candidate is None:  # pragma: no cover - the foreign key forbids it
            raise PlaybookCandidateNotFound(playbook.candidate_id)
        test = await self._playbooks.get_replay_test(playbook.promoted_from_test_id)
        return PlaybookDetail(
            playbook=playbook, candidate=candidate, promotion_test=test
        )

    async def retire_playbook(self, reference: PlaybookId | str) -> Playbook:
        """Retire one active playbook. It is kept, not deleted.

        Raises:
            PlaybookNotFound: no such playbook.
            InvalidPlaybookTransition: the playbook is already retired.
        """
        playbook = await self._require_playbook(reference)
        return await self._playbooks.retire_playbook(
            playbook_id=playbook.id, now=self._clock.now()
        )

    # --------------------------------------------------------------------- helpers

    def _input_fingerprint(
        self, candidate: PlaybookCandidate, validator: PlaybookReplayValidator
    ) -> str:
        return replay_input_fingerprint(
            candidate_id=candidate.id,
            source_action_id=candidate.source_action_id,
            source_action_fingerprint=candidate.source_action_fingerprint,
            source_action_type=candidate.source_action_type,
            validator_action_type=validator.action_type,
            contract_version=validator.contract_version,
        )

    def _require_supported(self, action_type: ActionType) -> None:
        """Refuse an action type that could never be tested, *before* it becomes a candidate.

        Raises:
            PlaybookSourceUnsupported: no validator is registered for this type.
        """
        if not self._replay.supports(action_type):
            raise PlaybookSourceUnsupported(action_type)

    def _require_validator(self, action_type: ActionType) -> PlaybookReplayValidator:
        """The validator for one action type, for a candidate that already exists.

        Raises:
            PlaybookReplayUnsupported: the registered capability set no longer covers this type.
        """
        return self._replay.validator_for(action_type)

    async def _revalidate_source(
        self, candidate: PlaybookCandidate
    ) -> tuple[ActionRequest, ExecutionRun]:
        """Re-read the source and require it to still be the snapshot the candidate recorded."""
        action = await self._require_action(candidate.source_action_id)
        if action.fingerprint != candidate.source_action_fingerprint:
            raise PlaybookSourceIntegrityError(
                action.id, "the action fingerprint is not the one the candidate recorded"
            )
        runs = await self._actions.list_executions(action.id)
        run = next(
            (item for item in runs if item.id == candidate.source_execution_run_id), None
        )
        if run is None:
            raise PlaybookSourceIntegrityError(
                action.id, "the recorded execution run does not exist for this action"
            )
        try:
            self._require_success(action, [run])
        except PlaybookSourceNotEligible as exc:
            raise PlaybookSourceIntegrityError(action.id, exc.reason) from exc
        return action, run

    async def _require_action(self, action_id: ActionRequestId) -> ActionRequest:
        action = await self._actions.get_action(action_id)
        if action is None:
            raise ActionRequestNotFound(action_id)
        if not action.fingerprint_matches():
            raise PlaybookSourceIntegrityError(
                action.id, "the stored payload no longer matches its fingerprint"
            )
        return action

    async def _success_runs(self, action_id: ActionRequestId) -> list[ExecutionRun]:
        runs = await self._actions.list_executions(action_id)
        return [item for item in runs if item.status is ExecutionRunStatus.SUCCEEDED]

    def _require_success(
        self, action: ActionRequest, runs: list[ExecutionRun]
    ) -> ExecutionRun:
        """Pick the definitive successful run for this action, or explain why none qualifies."""
        if action.status is not ActionRequestStatus.EXECUTED:
            raise PlaybookSourceNotEligible(
                action.id, f"the action is {action.status.value}, not executed"
            )
        eligible = [
            run
            for run in runs
            if run.action_id == action.id
            and run.status is ExecutionRunStatus.SUCCEEDED
            and run.finished_at is not None
        ]
        if not eligible:
            raise PlaybookSourceNotEligible(
                action.id, "no execution run of this action succeeded definitively"
            )
        return sorted(eligible, key=lambda item: (item.started_at, str(item.id)))[-1]

    async def _resolve_action(self, reference: ActionRequestId | str) -> ActionRequestId:
        if not isinstance(reference, str):
            return reference
        return await self._actions.resolve_action_id(reference)

    async def _require_candidate(
        self, reference: PlaybookCandidateId | str
    ) -> PlaybookCandidate:
        candidate_id = (
            reference
            if not isinstance(reference, str)
            else await self._playbooks.resolve_candidate_id(reference)
        )
        candidate = await self._playbooks.get_candidate(candidate_id)
        if candidate is None:
            raise PlaybookCandidateNotFound(candidate_id)
        return candidate

    async def _require_playbook(self, reference: PlaybookId | str) -> Playbook:
        playbook_id = (
            reference
            if not isinstance(reference, str)
            else await self._playbooks.resolve_playbook_id(reference)
        )
        playbook = await self._playbooks.get_playbook(playbook_id)
        if playbook is None:
            raise PlaybookNotFound(playbook_id)
        return playbook


__all__ = [
    "CandidateDetail",
    "PlaybookDetail",
    "PlaybookService",
]
