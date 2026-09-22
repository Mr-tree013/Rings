"""Deterministic review and settlement of one exact external action (ADR-0034 §5, §19-20; ADR-0045).

This component is the only place in the conversation feature where an external effect can happen,
and it is deliberately the least clever code in the phase:

```text
review (pointer) ──► ActionRequest (immutable) ──► challenge ──► Approval ──► ExecutionRun
        │                    │                          │
   fingerprint          re-hashed here            plaintext token never
   comparison            before anything          leaves this call
```

It has no `ModelPort`, no interpreter and no prompt: it cannot be talked into anything. What
reaches it is a sentence the *human* typed, already matched against a fixed vocabulary by the
conversation service, plus a durable row that names exactly one action and one fingerprint.

Everything it does on the way to an effect is a re-verification: the action still exists, is still
one of the two reviewed capabilities, still hashes to the fingerprint the review recorded, and is
still exactly what the user was shown. That last check is *per capability* and it is closed —
`mail.send` re-reads its draft version, `ehall.submit-certificate` re-parses the approved payload —
so a third capability cannot reach the approval boundary by naming itself here. If any check fails
the review goes `STALE` and nothing is approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.mail_send_status import MailSendStatusService
from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.conversation_review import (
    ALLOWED_EXTERNAL_ACTION_TYPES,
    EHALL_CERTIFICATE_ACTION_TYPE,
    EXTERNAL_REVIEW_TTL_MINUTES,
    MAIL_SEND_ACTION_TYPE,
    ConversationExternalReview,
    ConversationExternalReviewStatus,
)
from assistant.domain.ehall import EHallCertificatePayload
from assistant.domain.errors import ActionExecutionUnknown, DomainError, InvalidEHallForm
from assistant.domain.execution import ExecutionRunStatus
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock
from assistant.ports.conversation_review_repository import ConversationReviewRepository


@dataclass(frozen=True, slots=True)
class PreparedReview:
    """One action frozen for human review, with the exact payload behind it."""

    review: ConversationExternalReview
    action: ActionRequest
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ConfirmationOutcome:
    """What one confirmation phrase actually did."""

    review: ConversationExternalReview
    settled: bool
    execution_status: str | None = None
    failure_reason: str | None = None

    @property
    def status(self) -> ConversationExternalReviewStatus:
        """The review's state after the attempt."""
        return self.review.status


class ConversationExternalReviewService:
    """Opens, shows, settles and withdraws conversational reviews of external actions."""

    def __init__(
        self,
        *,
        reviews: ConversationReviewRepository,
        actions: ActionRepository,
        approvals: ApprovalService,
        execution: ActionExecutionService,
        sends: MailSendStatusService,
        clock: Clock,
        ttl: timedelta | None = None,
    ) -> None:
        self._reviews = reviews
        self._actions = actions
        self._approvals = approvals
        self._execution = execution
        self._sends = sends
        self._clock = clock
        self._ttl = ttl or timedelta(minutes=EXTERNAL_REVIEW_TTL_MINUTES)

    # ------------------------------------------------------------------------ opening

    async def open(
        self, *, thread_id: UUID, operation_id: UUID, action_id: ActionRequestId
    ) -> PreparedReview:
        """Freeze one prepared action for review. No approval, no execution, no SMTP.

        Raises:
            ActionRequestNotFound: no such action.
            ConversationCapabilityUnavailable: the action is not a reviewed capability.
            ActionFingerprintMismatch: the stored payload does not hash to its fingerprint.
        """
        action = await self._actions.get_action(action_id)
        if action is None:
            from assistant.domain.errors import ActionRequestNotFound

            raise ActionRequestNotFound(action_id)
        if action.action_type.value not in ALLOWED_EXTERNAL_ACTION_TYPES:
            from assistant.domain.errors import ConversationCapabilityUnavailable

            raise ConversationCapabilityUnavailable(
                f"{action.action_type.value} has no conversational review in this phase"
            )
        if not action.fingerprint_matches():
            from assistant.domain.errors import ActionFingerprintMismatch

            raise ActionFingerprintMismatch(action.id)
        now = self._clock.now()
        for previous in await self._reviews.waiting_for_thread(thread_id):
            # One live review per thread: a new review supersedes the old one, which the user has
            # effectively moved on from by preparing something else.
            await self._reviews.update_review(
                previous.with_status(ConversationExternalReviewStatus.STALE, at=now)
            )
        review = ConversationExternalReview(
            conversation_operation_id=operation_id,
            thread_id=thread_id,
            action_request_id=action.id,
            action_type=action.action_type.value,
            action_fingerprint=action.fingerprint,
            expires_at=now + self._ttl,
            created_at=now,
            updated_at=now,
        )
        stored = await self._reviews.add_review(review)
        return PreparedReview(
            review=stored, action=action, payload=_payload_of(action)
        )

    # ------------------------------------------------------------------------- reading

    async def waiting(self, thread_id: UUID) -> list[ConversationExternalReview]:
        """Live reviews of one conversation thread, oldest first."""
        return await self._reviews.waiting_for_thread(thread_id)

    async def get(self, review_id: UUID) -> ConversationExternalReview | None:
        """One review by identity, or `None` — how a card names its exact target."""
        return await self._reviews.get_review(review_id)

    async def payload_of(self, review: ConversationExternalReview) -> dict[str, object] | None:
        """The exact payload of the reviewed action, or `None` when it is gone."""
        try:
            action = await self._actions.get_action(review.action_request_id)
        except Exception:
            # A store that refuses to read a corrupt row has no payload to show.
            return None
        return None if action is None else _payload_of(action)

    async def latest_action_id(self) -> ActionRequestId | None:
        """The most recently prepared send, for "check whether that one went out"."""
        statuses = await self._sends.list_statuses(limit=1)
        return None if not statuses else statuses[0].action.id

    # ------------------------------------------------------------------------ settling

    async def confirm(self, review: ConversationExternalReview) -> ConfirmationOutcome:
        """Settle one review: re-verify, approve, execute — or refuse and explain."""
        now = self._clock.now()
        if review.is_expired(now):
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.EXPIRED, now
                ),
                settled=False,
                failure_reason="expired",
            )
        try:
            action = await self._actions.get_action(review.action_request_id)
        except Exception:
            # A store that refuses to read a corrupt or tampered row must make the conversation
            # refuse the send, not crash it. Nothing has been approved at this point.
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason="the stored action could not be read",
            )
        if action is None:
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason="the prepared action is gone",
            )
        if action.action_type.value not in ALLOWED_EXTERNAL_ACTION_TYPES:
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason="the action is no longer a reviewed capability",
            )
        if action.fingerprint != review.action_fingerprint or not action.fingerprint_matches():
            # Corruption or tampering: the bytes the user read are not the bytes on disk.
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason="the action payload changed since it was reviewed",
            )
        stale_reason = await self._stale_reason(action)
        if stale_reason is not None:
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason=stale_reason,
            )
        if not action.is_prepared:
            return ConfirmationOutcome(
                review=await self._transition(
                    review, ConversationExternalReviewStatus.STALE, now
                ),
                settled=False,
                failure_reason=f"the action is {action.status.value}",
            )
        issued = await self._approvals.create_challenge(action.id)
        # The plaintext token exists only here, for this one call. It is never persisted, never
        # returned to a caller and never shown to a model.
        await self._approvals.approve(action.id, issued.token)
        approved = await self._transition(
            review, ConversationExternalReviewStatus.APPROVED, self._clock.now()
        )
        try:
            result = await self._execution.execute(action.id)
        except ActionExecutionUnknown:
            run = await self._latest_run(action.id)
            return ConfirmationOutcome(
                review=await self._transition(
                    approved,
                    ConversationExternalReviewStatus.UNKNOWN,
                    self._clock.now(),
                    execution_run_id=None if run is None else run,
                ),
                settled=False,
                execution_status=ExecutionRunStatus.UNKNOWN.value,
                failure_reason="unknown",
            )
        except DomainError as exc:
            return ConfirmationOutcome(
                review=await self._transition(
                    approved, ConversationExternalReviewStatus.FAILED, self._clock.now()
                ),
                settled=False,
                failure_reason=str(exc),
            )
        settled = _status_for(result.status)
        if settled is not ConversationExternalReviewStatus.SUCCEEDED:
            return ConfirmationOutcome(
                review=await self._transition(
                    approved, settled, self._clock.now(), execution_run_id=result.run.id
                ),
                settled=False,
                execution_status=result.status.value,
                failure_reason=result.run.error_summary,
            )
        return ConfirmationOutcome(
            review=await self._transition(
                approved, settled, self._clock.now(), execution_run_id=result.run.id
            ),
            settled=True,
            execution_status=result.status.value,
        )

    async def cancel(self, review: ConversationExternalReview) -> ConversationExternalReview:
        """Withdraw a review without touching an approval, an execution or SMTP."""
        return await self._transition(
            review, ConversationExternalReviewStatus.CANCELLED, self._clock.now()
        )

    async def supersede(self, review: ConversationExternalReview) -> ConversationExternalReview:
        """Mark one review stale because the thing it reviewed has moved on."""
        return await self._transition(
            review, ConversationExternalReviewStatus.STALE, self._clock.now()
        )

    async def expire_due(self, thread_id: UUID) -> tuple[ConversationExternalReview, ...]:
        """Close every waiting review of a thread whose window has passed."""
        now = self._clock.now()
        closed: list[ConversationExternalReview] = []
        for review in await self._reviews.waiting_for_thread(thread_id):
            if review.is_expired(now):
                closed.append(
                    await self._transition(
                        review, ConversationExternalReviewStatus.EXPIRED, now
                    )
                )
        return tuple(closed)

    # ------------------------------------------------------------------------ internals

    async def _transition(
        self,
        review: ConversationExternalReview,
        status: ConversationExternalReviewStatus,
        at: datetime,
        *,
        execution_run_id: UUID | None = None,
    ) -> ConversationExternalReview:
        updated = review.with_status(status, at=at, execution_run_id=execution_run_id)
        return await self._reviews.update_review(updated)

    async def _stale_reason(self, action: ActionRequest) -> str | None:
        """Whether one reviewed action still means what the human was shown.

        A closed per-capability check, in the same spirit as the card dispatch: a capability with no
        branch here cannot be settled, because it never reaches the approval below.

        `mail.send` has a second durable copy of the letter behind it — the draft — so a draft that
        moved makes the review stale. A certificate action has no second copy: the frozen payload
        *is* the errand, and the live page is re-verified by the gateway after the approval has been
        consumed (a changed page is `EHallPageChanged`, and nothing is typed). What is checked here
        is that the stored payload is still a usable certificate errand at all, so a corrupt row
        cannot spend a human decision.
        """
        if action.action_type.value == MAIL_SEND_ACTION_TYPE:
            try:
                status = await self._sends.status(action.id)
            except Exception:
                return "the stored send could not be read"
            if status.draft_version_changed:
                return "the draft changed after it was reviewed"
            return None
        if action.action_type.value == EHALL_CERTIFICATE_ACTION_TYPE:
            try:
                EHallCertificatePayload.from_payload(action.payload)
            except InvalidEHallForm:
                return "the stored certificate payload is not usable"
            return None
        return "the action is no longer a reviewed capability"

    async def _latest_run(self, action_id: ActionRequestId) -> UUID | None:
        """The most recent execution attempt for an action, whatever its capability.

        Read from the action store rather than from the mail send status: an ambiguous certificate
        submission is exactly as real as an ambiguous letter, and the run that recorded it is the
        same kind of row.
        """
        run = await self._actions.latest_execution(action_id)
        return None if run is None else run.id


def _payload_of(action: ActionRequest) -> dict[str, object]:
    payload = action.payload
    return dict(payload) if isinstance(payload, dict) else {}


def _status_for(
    status: ExecutionRunStatus,
) -> ConversationExternalReviewStatus:
    if status is ExecutionRunStatus.SUCCEEDED:
        return ConversationExternalReviewStatus.SUCCEEDED
    if status is ExecutionRunStatus.FAILED:
        return ConversationExternalReviewStatus.FAILED
    return ConversationExternalReviewStatus.UNKNOWN


__all__ = [
    "ConfirmationOutcome",
    "ConversationExternalReviewService",
    "PreparedReview",
]
