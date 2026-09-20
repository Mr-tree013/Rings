"""Cases and actions as user-facing work (ADR-0023).

This service is small on purpose: it creates a container, moves it through its one-way lifecycle,
prepares an action inside it and lists what it holds. It cannot approve anything and cannot
execute anything — those are separate services with separate callers, which is what keeps "the
assistant prepared this" from drifting into "the assistant did this".

`prepare_action` exists for tests and for the typed factories a later phase will add (a mail
sender, an eHall submitter). There is deliberately no generic CLI command that turns arbitrary
user input into an `ActionRequest`.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from assistant.domain.action import (
    ActionRequest,
    ActionRequestId,
    ActionType,
)
from assistant.domain.case import Case, CaseId, CaseStatus, validate_case_title
from assistant.domain.errors import ActionRequestNotFound, CaseNotFound
from assistant.ports.action_repository import ActionRepository
from assistant.ports.case_repository import CaseRepository
from assistant.ports.clock import Clock


@dataclass(frozen=True, slots=True)
class CaseDetail:
    """A case together with the actions it contains."""

    case: Case
    actions: tuple[ActionRequest, ...] = ()


class CaseService:
    """Durable cases and the actions prepared inside them."""

    def __init__(
        self, cases: CaseRepository, actions: ActionRepository, clock: Clock
    ) -> None:
        self._cases = cases
        self._actions = actions
        self._clock = clock

    async def create_case(self, title: str) -> Case:
        """Open a new case with a validated title."""
        now = self._clock.now()
        return await self._cases.add_case(
            Case(title=validate_case_title(title), created_at=now, updated_at=now)
        )

    async def get_case(self, reference: CaseId | str) -> CaseDetail:
        """Return one case and its actions, oldest action first.

        Raises:
            CaseNotFound: no such case.
            AmbiguousId: a prefix matched several cases.
        """
        case = await self._require_case(reference)
        actions = await self._actions.list_actions(case_id=case.id, limit=None)
        return CaseDetail(
            case=case,
            actions=tuple(
                sorted(actions, key=lambda action: (action.created_at, str(action.id)))
            ),
        )

    async def list_cases(
        self, *, statuses: tuple[CaseStatus, ...] | None = None, limit: int | None = 20
    ) -> list[Case]:
        """List cases, newest update first."""
        return await self._cases.list_cases(statuses=statuses, limit=limit)

    async def complete_case(self, reference: CaseId | str) -> Case:
        """Move an OPEN case to COMPLETED.

        Raises:
            CaseNotFound: no such case.
            InvalidCaseTransition: the case is already terminal.
            StaleCaseUpdate: someone else moved the case first.
        """
        case = await self._require_case(reference)
        return await self._cases.update_case(
            case.complete(self._clock.now()), expected_updated_at=case.updated_at
        )

    async def cancel_case(self, reference: CaseId | str) -> Case:
        """Move an OPEN case to CANCELLED.

        Raises:
            CaseNotFound: no such case.
            InvalidCaseTransition: the case is already terminal.
            StaleCaseUpdate: someone else moved the case first.
        """
        case = await self._require_case(reference)
        return await self._cases.update_case(
            case.cancel(self._clock.now()), expected_updated_at=case.updated_at
        )

    async def resolve_case_id(self, reference: str) -> CaseId:
        """Resolve a full UUID or a unique prefix to a case id."""
        return await self._cases.resolve_case_id(reference)

    async def prepare_action(
        self,
        case_id: CaseId,
        action_type: ActionType | str,
        payload: object,
    ) -> ActionRequest:
        """Prepare one exact side effect inside an existing case.

        The payload is canonicalised and fingerprinted immediately; there is no edit path
        afterwards, so changed content means a new action and a new approval.

        Raises:
            CaseNotFound: the case does not exist.
            InvalidActionPayload: the payload is not JSON data.
            InvalidActionRequest: the type is not a namespaced identifier.
        """
        case = await self._cases.get_case(case_id)
        if case is None:
            raise CaseNotFound(case_id)
        return await self._actions.add_action(
            ActionRequest.prepare(
                case_id=case.id,
                action_type=action_type,
                payload=payload,
                at=self._clock.now(),
            )
        )

    async def cancel_action(self, reference: ActionRequestId | str) -> ActionRequest:
        """Move one PREPARED action to CANCELLED.

        Raises:
            ActionRequestNotFound: no such action.
            ActionNotExecutable: the action is already terminal.
        """
        action = await self.require_action(reference)
        return await self._actions.cancel_action(action.cancel(self._clock.now()))

    async def require_action(self, reference: ActionRequestId | str) -> ActionRequest:
        """Load one action by id or unique prefix.

        Raises:
            ActionRequestNotFound: no such action.
            AmbiguousId: a prefix matched several actions.
        """
        action = await self.find_action(reference)
        if action is None:  # pragma: no cover - resolution just found it
            raise ActionRequestNotFound(reference)
        return action

    async def find_action(self, reference: ActionRequestId | str) -> ActionRequest | None:
        """Load one action, or `None` when it does not exist."""
        action_id = (
            reference
            if isinstance(reference, UUID)
            else await self._actions.resolve_action_id(reference)
        )
        return await self._actions.get_action(action_id)

    async def _require_case(self, reference: CaseId | str) -> Case:
        case_id = (
            reference
            if isinstance(reference, UUID)
            else await self._cases.resolve_case_id(reference)
        )
        case = await self._cases.get_case(case_id)
        if case is None:
            raise CaseNotFound(reference)
        return case


__all__ = ["CaseDetail", "CaseService"]
