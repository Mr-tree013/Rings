"""Approval: a human decision bound to one exact action fingerprint (ADR-0023).

The chain has two links and a deliberate asymmetry between them:

- a **challenge** is a short-lived, single-use secret. The database stores only its SHA-256; the
  plaintext token is returned exactly once, by the call that created it, and never again — not
  in a log, not in a list, not in an error message. Getting the hash wrong is what protects the
  approval from anyone who can read the database;
- an **approval** is what the challenge becomes when it is redeemed. It names the fingerprint it
  authorised, expires on its own clock, and is consumed by exactly one execution.

Nothing in this module can be reached by a model, an interpreter, an event handler, a mail
analysis or a draft: approval is a human act, and the only code that writes an `ApprovalRecord`
is the approval service the CLI calls.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from assistant.domain.action import FINGERPRINT_PATTERN, ActionRequestId
from assistant.domain.errors import InvalidApproval

ApprovalChallengeId = UUID
ApprovalId = UUID

DEFAULT_APPROVAL_TTL_SECONDS = 600
"""How long a challenge — and the approval it becomes — stays usable (10 minutes)."""


def new_approval_challenge_id() -> ApprovalChallengeId:
    """Generate a fresh challenge identity."""
    return uuid4()


def new_approval_id() -> ApprovalId:
    """Generate a fresh approval identity."""
    return uuid4()


def approval_ttl() -> timedelta:
    """The fixed time-to-live of a challenge and of the approval it produces."""
    return timedelta(seconds=DEFAULT_APPROVAL_TTL_SECONDS)


def hash_approval_token(token: str) -> str:
    """The SHA-256 of a presented token, as 64 lowercase hex characters.

    The hash is the *only* representation of the secret the system keeps. Comparison happens on
    hashes, so the plaintext never has to be recovered, stored or compared anywhere.
    """
    if not token:
        raise InvalidApproval("an approval token must not be blank")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidApproval(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def _require_fingerprint(value: str) -> None:
    if not FINGERPRINT_PATTERN.match(value):
        raise InvalidApproval("an action fingerprint must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class ApprovalChallenge:
    """A single-use, short-lived request for a human decision about one exact action."""

    action_id: ActionRequestId
    action_fingerprint: str
    token_hash: str
    created_at: datetime
    expires_at: datetime
    id: ApprovalChallengeId = field(default_factory=new_approval_challenge_id)
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_fingerprint(self.action_fingerprint)
        _require_fingerprint(self.token_hash)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        _require_optional_aware(self.consumed_at, "consumed_at")
        if self.expires_at <= self.created_at:
            raise InvalidApproval("a challenge must expire after it was created")

    def is_expired(self, at: datetime) -> bool:
        """Whether the challenge can no longer be redeemed."""
        _require_aware(at, "at")
        return at >= self.expires_at

    def is_consumed(self) -> bool:
        """Whether the challenge has already been redeemed."""
        return self.consumed_at is not None

    def matches_token(self, token: str) -> bool:
        """Whether a presented token is this challenge's, compared on hashes."""
        try:
            presented = hash_approval_token(token)
        except InvalidApproval:
            return False
        return presented == self.token_hash


@dataclass(frozen=True, slots=True)
class ApprovalChallengeIssued:
    """A stored challenge plus the one-and-only sight of its plaintext token."""

    challenge: ApprovalChallenge
    token: str

    def __post_init__(self) -> None:
        if not self.token:
            raise InvalidApproval("an issued challenge must carry a token")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One human approval of one exact action fingerprint."""

    action_id: ActionRequestId
    action_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    id: ApprovalId = field(default_factory=new_approval_id)
    consumed_at: datetime | None = None
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_fingerprint(self.action_fingerprint)
        _require_aware(self.approved_at, "approved_at")
        _require_aware(self.expires_at, "expires_at")
        _require_optional_aware(self.consumed_at, "consumed_at")
        _require_optional_aware(self.superseded_at, "superseded_at")
        if self.expires_at <= self.approved_at:
            raise InvalidApproval("an approval must expire after it was given")

    def is_expired(self, at: datetime) -> bool:
        """Whether the approval is past its expiry."""
        _require_aware(at, "at")
        return at >= self.expires_at

    def is_consumed(self) -> bool:
        """Whether an execution has already spent this approval."""
        return self.consumed_at is not None

    def is_superseded(self) -> bool:
        """Whether a later, explicit approval replaced this one.

        A superseded approval is history: it stays in the audit trail, and it can never
        authorise anything again.
        """
        return self.superseded_at is not None

    def is_usable_at(self, at: datetime) -> bool:
        """Whether this approval could authorise an execution right now."""
        return not (
            self.is_consumed() or self.is_superseded() or self.is_expired(at)
        )

    def matches_fingerprint(self, fingerprint: str) -> bool:
        """Whether this approval was given for exactly that action content."""
        return self.action_fingerprint == fingerprint


__all__ = [
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "ApprovalChallenge",
    "ApprovalChallengeId",
    "ApprovalChallengeIssued",
    "ApprovalId",
    "ApprovalRecord",
    "approval_ttl",
    "hash_approval_token",
    "new_approval_challenge_id",
    "new_approval_id",
]
