"""Derived delivery state for prepared mail sends (ADR-0024).

```text
DRAFT            prepared, nothing approved, nothing attempted
APPROVED         a live, unconsumed approval exists
SENDING          a run is RUNNING
SENT             a run SUCCEEDED (or the action is EXECUTED)
SENDING_UNKNOWN  a run is UNKNOWN, or a crash left one RUNNING and we are looking at it later
FAILED           the latest attempt was definitely refused
```

Nothing here is stored twice. The state is *derived* from the Phase 6A records — the action, its
approvals and its execution runs — so there is exactly one authority for "was it approved" and
"did it run", and this module cannot drift from it.

`FAILED` is a display word, not an instruction: a failed attempt has already consumed its approval,
and trying again needs a fresh human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from assistant.application.action_service import ApprovalState, approval_state
from assistant.domain.action import ActionRequest, ActionRequestId, ActionRequestStatus
from assistant.domain.approval import ApprovalRecord
from assistant.domain.errors import ActionRequestNotFound, MailSendNotReconcilable
from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
from assistant.domain.mail_draft import MailDraft
from assistant.domain.mail_send import (
    MailSendKind,
    MailSendLink,
    MailSendPayload,
    MailSendReconciliation,
)
from assistant.domain.new_mail_draft import NewMailDraft
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock
from assistant.ports.mail_draft_repository import MailDraftRepository
from assistant.ports.mail_send_repository import MailSendRepository
from assistant.ports.new_mail_draft_repository import NewMailDraftRepository


class MailDeliveryState(StrEnum):
    """Where one prepared send stands, in the words a user needs."""

    DRAFT = "draft"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    SENDING_UNKNOWN = "sending_unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MailSendStatus:
    """One prepared send: its exact content, its state and its history."""

    action: ActionRequest
    payload: MailSendPayload
    link: MailSendLink
    draft: MailDraft | None
    state: MailDeliveryState
    approval_state: ApprovalState
    new_draft: NewMailDraft | None = None
    approval: ApprovalRecord | None = None
    execution: ExecutionRun | None = None
    reconciliations: tuple[MailSendReconciliation, ...] = ()

    @property
    def draft_version_changed(self) -> bool:
        """Whether the draft has moved on since this action was prepared.

        The action still sends exactly what it snapshotted; this only tells the user that the
        draft they are looking at is no longer the text behind the pending approval.
        """
        if self.current_draft_version is None:
            return False
        return self.current_draft_version != self.link.draft_version

    @property
    def current_draft_version(self) -> int | None:
        """The draft's version right now, or `None` when the draft is gone."""
        if self.link.kind is MailSendKind.NEW:
            return None if self.new_draft is None else self.new_draft.version
        return None if self.draft is None else self.draft.version


def derive_delivery_state(
    *,
    action: ActionRequest,
    approval: ApprovalRecord | None,
    run: ExecutionRun | None,
    now: datetime,
) -> MailDeliveryState:
    """Derive the delivery word from the records that actually exist."""
    if action.status is ActionRequestStatus.EXECUTED:
        return MailDeliveryState.SENT
    if run is not None:
        if run.status is ExecutionRunStatus.SUCCEEDED:
            return MailDeliveryState.SENT
        if run.status is ExecutionRunStatus.RUNNING:
            # A run still running may be this very call, or a crash from an earlier process. Both
            # read the same to a user: the outcome is not known yet, and a retry is not allowed.
            return MailDeliveryState.SENDING_UNKNOWN
        if run.status is ExecutionRunStatus.UNKNOWN:
            return MailDeliveryState.SENDING_UNKNOWN
        if run.status is ExecutionRunStatus.FAILED:
            return MailDeliveryState.FAILED
    if approval_state(approval, now) is ApprovalState.VALID:
        return MailDeliveryState.APPROVED
    return MailDeliveryState.DRAFT


class MailSendStatusService:
    """Read-only views of prepared sends. It cannot send, approve or reconcile."""

    def __init__(
        self,
        actions: ActionRepository,
        send: MailSendRepository,
        drafts: MailDraftRepository,
        clock: Clock,
        *,
        new_drafts: NewMailDraftRepository | None = None,
    ) -> None:
        self._actions = actions
        self._send = send
        self._drafts = drafts
        self._clock = clock
        self._new_drafts = new_drafts

    async def status(self, action_id: ActionRequestId | str) -> MailSendStatus:
        """Return one prepared send with its delivery state.

        Raises:
            ActionRequestNotFound: no such action.
            MailSendNotReconcilable: the action is not a mail send.
        """
        action = await self._require_send_action(action_id)
        link = await self._send.get_link(action.id)
        if link is None:
            raise MailSendNotReconcilable(action.id, "it has no send link")
        return await self._describe(action, link)

    async def list_statuses(self, *, limit: int | None = 20) -> list[MailSendStatus]:
        """List prepared sends, newest first."""
        links = await self._send.list_links(limit=limit)
        statuses: list[MailSendStatus] = []
        for link in links:
            action = await self._actions.get_action(link.action_id)
            if action is None:  # pragma: no cover - the link's FK guarantees the action
                continue
            statuses.append(await self._describe(action, link))
        return statuses

    async def require_send_action(
        self, action_id: ActionRequestId | str
    ) -> ActionRequest:
        """Load one action and require that it is a `mail.send`."""
        return await self._require_send_action(action_id)

    async def _require_send_action(
        self, action_id: ActionRequestId | str
    ) -> ActionRequest:
        resolved = (
            action_id
            if not isinstance(action_id, str)
            else await self._actions.resolve_action_id(action_id)
        )
        action = await self._actions.get_action(resolved)
        if action is None:
            raise ActionRequestNotFound(action_id)
        if action.action_type.value != "mail.send":
            raise MailSendNotReconcilable(
                action.id, f"its action type is {action.action_type.value}"
            )
        return action

    async def _describe(
        self, action: ActionRequest, link: MailSendLink
    ) -> MailSendStatus:
        payload = MailSendPayload.from_payload(action.payload)
        approval = await self._actions.latest_approval(action.id)
        run = await self._actions.latest_execution(action.id)
        draft = None if link.draft_id is None else await self._drafts.get_draft(link.draft_id)
        new_draft = None
        if link.new_draft_id is not None and self._new_drafts is not None:
            new_draft = await self._new_drafts.get_draft(link.new_draft_id)
        now = self._clock.now()
        return MailSendStatus(
            action=action,
            payload=payload,
            link=link,
            draft=draft,
            new_draft=new_draft,
            state=derive_delivery_state(
                action=action, approval=approval, run=run, now=now
            ),
            approval_state=approval_state(approval, now),
            approval=approval,
            execution=run,
            reconciliations=tuple(
                await self._send.list_reconciliations(action.id)
            ),
        )


__all__ = [
    "MailDeliveryState",
    "MailSendStatus",
    "MailSendStatusService",
    "derive_delivery_state",
]
