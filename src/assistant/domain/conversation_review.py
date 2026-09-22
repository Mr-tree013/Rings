"""A conversational review of one exact external action (ADR-0034, ADR-0045).

A review is a pointer, not a copy: it remembers which conversation operation prepared which
immutable `ActionRequest`, what that action's fingerprint was, and how the human settled it. The
preview the user reads is always re-rendered from the action's own payload, so the thing being
approved and the thing that would be sent can never drift apart.

The reviewed capabilities are a *closed pair* of action types — `mail.send` and
`ehall.submit-certificate` — and each has its own confirmation vocabulary:

* **sending** needs "确认发送" (and its documented synonyms); **submitting** needs "确认提交". A
  generic "可以" confirms a local plan and must never move an external effect, and one phrase can
  never settle two capabilities, because a confirmation is matched against the review's own
  vocabulary.
* **cancelling** is explicit too, and cheaper to say than nothing being said. The cancellation
  vocabulary may overlap between capabilities; the confirmation vocabulary may not.

Nothing here knows about `ApprovalService`, SMTP, a model or SQLite; the deterministic controller
that acts on a review lives in the application layer, and the approval itself is still created by
the existing service at the moment the user confirms.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidConversationOperation

ConversationExternalReviewId = UUID

EXTERNAL_REVIEW_TTL_MINUTES = 30
"""How long a reviewed send waits for the human's explicit confirmation."""

MAIL_SEND_ACTION_TYPE = "mail.send"
"""Sending one exact letter (Phase 10B)."""

EHALL_CERTIFICATE_ACTION_TYPE = "ehall.submit-certificate"
"""Submitting one exact certificate application (Phase 11E)."""

ALLOWED_EXTERNAL_ACTION_TYPES = frozenset(
    {MAIL_SEND_ACTION_TYPE, EHALL_CERTIFICATE_ACTION_TYPE}
)
"""The only external capabilities with a conversational review design.

A closed set, deliberately. Widening it is a reviewed change that has to move the table CHECK in
`migrations/0023_conversation_review_expansion.sql`, the confirmation vocabulary, the preview
dispatch, the card kind and the integrity audit together (ADR-0045 §25).
"""

CONFIRM_SEND_PHRASES = frozenset(
    {"确认发送", "发送", "发吧", "确认发出", "send", "confirm send"}
)
"""The entire accepted send vocabulary. Generic acknowledgements are absent on purpose."""

CANCEL_SEND_PHRASES = frozenset({"取消", "不要发", "不发送", "cancel"})
"""The entire accepted withdrawal vocabulary."""

CONFIRM_EHALL_CERTIFICATE_PHRASES = frozenset(
    {"确认提交", "提交", "确认申请", "提交申请", "submit", "confirm submit"}
)
"""The entire accepted submission vocabulary. It is disjoint from the send vocabulary."""

CANCEL_EHALL_CERTIFICATE_PHRASES = frozenset(
    {"取消", "不要提交", "不提交", "别提交", "cancel"}
)
"""The entire accepted withdrawal vocabulary for a prepared certificate."""

CONFIRM_PHRASES_BY_ACTION_TYPE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        MAIL_SEND_ACTION_TYPE: CONFIRM_SEND_PHRASES,
        EHALL_CERTIFICATE_ACTION_TYPE: CONFIRM_EHALL_CERTIFICATE_PHRASES,
    }
)
"""One confirmation vocabulary per reviewed capability. A capability absent here cannot settle."""

CANCEL_PHRASES_BY_ACTION_TYPE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        MAIL_SEND_ACTION_TYPE: CANCEL_SEND_PHRASES,
        EHALL_CERTIFICATE_ACTION_TYPE: CANCEL_EHALL_CERTIFICATE_PHRASES,
    }
)
"""One withdrawal vocabulary per reviewed capability."""


def new_external_review_id() -> ConversationExternalReviewId:
    """Generate a fresh review identity."""
    return uuid4()


class ConversationExternalReviewStatus(StrEnum):
    """How one reviewed external action ended."""

    WAITING = "waiting"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    STALE = "stale"
    APPROVED = "approved"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


TERMINAL_REVIEW_STATUSES = frozenset(
    {
        ConversationExternalReviewStatus.CANCELLED,
        ConversationExternalReviewStatus.EXPIRED,
        ConversationExternalReviewStatus.STALE,
        ConversationExternalReviewStatus.SUCCEEDED,
        ConversationExternalReviewStatus.FAILED,
        ConversationExternalReviewStatus.UNKNOWN,
    }
)
"""Statuses a review never leaves; `APPROVED` is deliberately not one of them."""

RESOLVED_REVIEW_STATUSES = frozenset(
    {
        *TERMINAL_REVIEW_STATUSES,
        ConversationExternalReviewStatus.APPROVED,
    }
)


@dataclass(frozen=True, slots=True)
class ConversationExternalReview:
    """One reviewed `mail.send` action, bound to the conversation turn that prepared it."""

    conversation_operation_id: UUID
    thread_id: UUID
    action_request_id: UUID
    action_type: str
    action_fingerprint: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    id: ConversationExternalReviewId = field(default_factory=new_external_review_id)
    status: ConversationExternalReviewStatus = ConversationExternalReviewStatus.WAITING
    execution_run_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.action_type not in ALLOWED_EXTERNAL_ACTION_TYPES:
            raise InvalidConversationOperation(
                f"{self.action_type!r} has no conversational review design in this phase"
            )
        if len(self.action_fingerprint) != 64:
            raise InvalidConversationOperation("action_fingerprint must be a SHA-256 digest")
        for value, name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
            (self.expires_at, "expires_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidConversationOperation(f"{name} must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise InvalidConversationOperation("a review expires after it is created")
        requires_execution = self.status in (
            ConversationExternalReviewStatus.SUCCEEDED,
            ConversationExternalReviewStatus.UNKNOWN,
        )
        if requires_execution and self.execution_run_id is None:
            raise InvalidConversationOperation(
                "a succeeded or unknown review names the execution run that decided it"
            )
        never_executed = self.status in (
            ConversationExternalReviewStatus.WAITING,
            ConversationExternalReviewStatus.CANCELLED,
            ConversationExternalReviewStatus.EXPIRED,
            ConversationExternalReviewStatus.STALE,
            ConversationExternalReviewStatus.APPROVED,
        )
        if never_executed and self.execution_run_id is not None:
            raise InvalidConversationOperation(
                "only an execution attempt may record an execution_run_id"
            )

    @property
    def is_waiting(self) -> bool:
        """Whether this review is still the one thing waiting for a human."""
        return self.status is ConversationExternalReviewStatus.WAITING

    def is_expired(self, now: datetime) -> bool:
        """Whether the review's window has closed."""
        return self.expires_at <= now

    def with_status(
        self,
        status: ConversationExternalReviewStatus,
        *,
        at: datetime,
        execution_run_id: UUID | None = None,
    ) -> ConversationExternalReview:
        """Return the same review in another state."""
        return ConversationExternalReview(
            id=self.id,
            conversation_operation_id=self.conversation_operation_id,
            thread_id=self.thread_id,
            action_request_id=self.action_request_id,
            action_type=self.action_type,
            action_fingerprint=self.action_fingerprint,
            status=status,
            expires_at=self.expires_at,
            execution_run_id=execution_run_id,
            created_at=self.created_at,
            updated_at=at,
        )


def matches_send_confirmation(text: str) -> bool:
    """Whether the raw human message is an explicit send confirmation."""
    return matches_confirmation(MAIL_SEND_ACTION_TYPE, text)


def matches_send_cancellation(text: str) -> bool:
    """Whether the raw human message withdraws the pending send."""
    return matches_cancellation(MAIL_SEND_ACTION_TYPE, text)


def matches_confirmation(action_type: str, text: str) -> bool:
    """Whether this raw human message confirms a review of exactly this action type.

    An action type with no reviewed confirmation vocabulary answers `False` rather than raising:
    the caller is asking "did the human settle *this* review", and for a capability that has no
    review the honest answer is no.
    """
    phrases = CONFIRM_PHRASES_BY_ACTION_TYPE.get(action_type)
    return phrases is not None and _normalise(text) in phrases


def matches_cancellation(action_type: str, text: str) -> bool:
    """Whether this raw human message withdraws a review of exactly this action type."""
    phrases = CANCEL_PHRASES_BY_ACTION_TYPE.get(action_type)
    return phrases is not None and _normalise(text) in phrases


def confirmation_phrases(action_type: str) -> frozenset[str]:
    """The closed confirmation vocabulary of one reviewed capability.

    Raises:
        InvalidConversationOperation: this action type has no conversational review.
    """
    phrases = CONFIRM_PHRASES_BY_ACTION_TYPE.get(action_type)
    if phrases is None:
        raise InvalidConversationOperation(
            f"{action_type!r} has no conversational review in this phase"
        )
    return phrases


def _normalise(text: str) -> str:
    return text.strip().lower().rstrip("。.!！~")


__all__ = [
    "ALLOWED_EXTERNAL_ACTION_TYPES",
    "CANCEL_EHALL_CERTIFICATE_PHRASES",
    "CANCEL_PHRASES_BY_ACTION_TYPE",
    "CANCEL_SEND_PHRASES",
    "CONFIRM_EHALL_CERTIFICATE_PHRASES",
    "CONFIRM_PHRASES_BY_ACTION_TYPE",
    "CONFIRM_SEND_PHRASES",
    "EHALL_CERTIFICATE_ACTION_TYPE",
    "EXTERNAL_REVIEW_TTL_MINUTES",
    "MAIL_SEND_ACTION_TYPE",
    "RESOLVED_REVIEW_STATUSES",
    "TERMINAL_REVIEW_STATUSES",
    "ConversationExternalReview",
    "ConversationExternalReviewId",
    "ConversationExternalReviewStatus",
    "confirmation_phrases",
    "matches_cancellation",
    "matches_confirmation",
    "matches_send_cancellation",
    "matches_send_confirmation",
    "new_external_review_id",
]
