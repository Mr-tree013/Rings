"""Candidates and confirmed personal facts: learning that a person authorised (ADR-0027).

Two values, and the difference between them is the whole safety story of this phase:

- a **candidate** is something the user proposed, with the correction it came from attached. It
  is never trusted for anything — an unconfirmed candidate is a suggestion, and the domain gives
  it no method that could fill a form, reach a model or send mail;
- a **confirmed fact** is one a person promoted by hand. It carries the same key and value it was
  proposed with (confirmation copies the snapshot; it cannot edit it), a validity window, and the
  provenance chain back to the correction.

Two rules shape the storage, and both are enforced by the database as well as here:

- **at most one current fact per key.** Confirming a new value retires the previous row by setting
  `superseded_at` — retired, never deleted, because "what did I believe in March" is a question a
  personal assistant should be able to answer;
- **expiry is derived.** A fact whose `valid_until` has passed is not active, but it is still the
  current row for its key, so no background job has to run for the rule to hold. There is no
  expiration scheduler in this phase, deliberately.

The key namespace is open by design (this phase does not whitelist keys) with one exception: a key
that names a credential is refused outright. The fact store is not a credential store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.correction import CorrectionId
from assistant.domain.errors import (
    ForbiddenFactKey,
    InvalidConfirmedFact,
    InvalidFactCandidate,
    InvalidFactCandidateTransition,
    InvalidFactKey,
)

FactKey = str
"""A stable namespaced identifier such as `profile.student_id`."""

FactCandidateId = UUID
"""Stable identity of one proposed fact."""

ConfirmedFactId = UUID
"""Stable identity of one confirmed fact."""

FACT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
"""Lowercase, dot-separated namespaces. No key whitelist exists in this phase."""

FORBIDDEN_FACT_KEY_SEGMENTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "private_key",
    }
)
"""Complete path segments that name a secret. Checked on the key, never on the value."""

FACT_VALUE_MAX_LENGTH = 8000
"""Fact values are strings in V1, and this is how long one may be."""


def new_fact_candidate_id() -> FactCandidateId:
    """Generate a fresh candidate identity."""
    return uuid4()


def new_confirmed_fact_id() -> ConfirmedFactId:
    """Generate a fresh confirmed-fact identity."""
    return uuid4()


def forbidden_segment(key: str) -> str | None:
    """Return the credential-like segment of `key`, or `None`.

    A dot-separated segment is refused when the segment itself is a banned word (`api_key`) or when
    one of its underscore- or hyphen-separated words is (`mail.smtp_token`). Matching is exact, so
    `profile.tokenizer` and `profile.secretary_name` are ordinary keys: the project never has to
    guess whether a *value* looks like a password, only whether a key says it is one.
    """
    for segment in key.strip().lower().split("."):
        if not segment:
            continue
        if segment in FORBIDDEN_FACT_KEY_SEGMENTS:
            return segment
        for word in segment.replace("-", "_").split("_"):
            if word in FORBIDDEN_FACT_KEY_SEGMENTS:
                return word
    return None


def validate_fact_key(key: str) -> FactKey:
    """Return the trimmed key, or raise `InvalidFactKey` / `ForbiddenFactKey`.

    The credential check runs first so that a key naming a secret is refused for that reason even
    when it is also malformed — `PROFILE.PASSWORD` is not an invitation to retype it in lowercase.
    """
    if not isinstance(key, str):
        raise InvalidFactKey("a fact key must be text")
    stripped = key.strip()
    banned = forbidden_segment(stripped)
    if banned is not None:
        raise ForbiddenFactKey(stripped, banned)
    if not FACT_KEY_PATTERN.match(stripped):
        raise InvalidFactKey(
            f"fact key must match {FACT_KEY_PATTERN.pattern} (got {key!r})"
        )
    return stripped


def validate_fact_value(value: str) -> str:
    """Return the trimmed value, or raise `InvalidFactCandidate`."""
    if not isinstance(value, str):
        raise InvalidFactCandidate("a fact value must be text in V1")
    stripped = value.strip()
    if not stripped:
        raise InvalidFactCandidate("a fact value must not be blank")
    if len(stripped) > FACT_VALUE_MAX_LENGTH:
        raise InvalidFactCandidate(
            f"a fact value must be at most {FACT_VALUE_MAX_LENGTH} characters"
        )
    return stripped


def _require_aware(value: datetime, field_name: str, error: type[Exception]) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise error(f"{field_name} must be timezone-aware")


def _require_optional_aware(
    value: datetime | None, field_name: str, error: type[Exception]
) -> None:
    if value is not None:
        _require_aware(value, field_name, error)


class FactCandidateStatus(StrEnum):
    """Where a proposed fact stands. Every state but `PENDING` is terminal."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FactState(StrEnum):
    """The locally derived state of a confirmed fact at one instant."""

    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """A proposed fact, with the correction it came from as its provenance."""

    fact_key: FactKey
    value: str
    correction_id: CorrectionId
    created_at: datetime
    id: FactCandidateId = field(default_factory=new_fact_candidate_id)
    status: FactCandidateStatus = FactCandidateStatus.PENDING
    proposed_valid_until: datetime | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_key", validate_fact_key(self.fact_key))
        object.__setattr__(self, "value", validate_fact_value(self.value))
        _require_aware(self.created_at, "created_at", InvalidFactCandidate)
        _require_optional_aware(
            self.proposed_valid_until, "proposed_valid_until", InvalidFactCandidate
        )
        _require_optional_aware(self.resolved_at, "resolved_at", InvalidFactCandidate)
        if (
            self.proposed_valid_until is not None
            and self.proposed_valid_until <= self.created_at
        ):
            raise InvalidFactCandidate(
                "proposed_valid_until must be after the candidate was created"
            )
        resolved = self.status is not FactCandidateStatus.PENDING
        if resolved and self.resolved_at is None:
            raise InvalidFactCandidate("a resolved candidate must record resolved_at")
        if not resolved and self.resolved_at is not None:
            raise InvalidFactCandidate("a pending candidate must not record resolved_at")

    @property
    def is_pending(self) -> bool:
        """Whether this candidate may still be confirmed or rejected."""
        return self.status is FactCandidateStatus.PENDING

    def confirm(self, at: datetime) -> FactCandidate:
        """Move PENDING to CONFIRMED.

        Raises:
            InvalidFactCandidateTransition: this candidate is already resolved.
        """
        _require_aware(at, "at", InvalidFactCandidate)
        if not self.is_pending:
            raise InvalidFactCandidateTransition(
                self.id, self.status, FactCandidateStatus.CONFIRMED
            )
        return replace(
            self, status=FactCandidateStatus.CONFIRMED, resolved_at=at
        )

    def reject(self, at: datetime) -> FactCandidate:
        """Move PENDING to REJECTED.

        Raises:
            InvalidFactCandidateTransition: this candidate is already resolved.
        """
        _require_aware(at, "at", InvalidFactCandidate)
        if not self.is_pending:
            raise InvalidFactCandidateTransition(
                self.id, self.status, FactCandidateStatus.REJECTED
            )
        return replace(self, status=FactCandidateStatus.REJECTED, resolved_at=at)

    def preview(self, limit: int = 60) -> str:
        """A single-line preview of the proposed value for list output."""
        collapsed = " ".join(self.value.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[: limit - 1] + "\u2026"


@dataclass(frozen=True, slots=True)
class ConfirmedFact:
    """A fact a human promoted from one candidate snapshot."""

    candidate_id: FactCandidateId
    fact_key: FactKey
    value: str
    valid_from: datetime
    created_at: datetime
    id: ConfirmedFactId = field(default_factory=new_confirmed_fact_id)
    valid_until: datetime | None = None
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_key", validate_fact_key(self.fact_key))
        object.__setattr__(self, "value", validate_fact_value(self.value))
        _require_aware(self.valid_from, "valid_from", InvalidConfirmedFact)
        _require_aware(self.created_at, "created_at", InvalidConfirmedFact)
        _require_optional_aware(self.valid_until, "valid_until", InvalidConfirmedFact)
        _require_optional_aware(self.superseded_at, "superseded_at", InvalidConfirmedFact)
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise InvalidConfirmedFact("valid_until must be after valid_from")
        if self.superseded_at is not None and self.superseded_at < self.created_at:
            raise InvalidConfirmedFact("superseded_at must not precede created_at")

    @property
    def is_superseded(self) -> bool:
        """Whether a later confirmation for the same key retired this row."""
        return self.superseded_at is not None

    def is_expired(self, at: datetime) -> bool:
        """Whether this fact's validity window has closed."""
        _require_aware(at, "at", InvalidConfirmedFact)
        return self.valid_until is not None and self.valid_until <= at

    def is_active_at(self, at: datetime) -> bool:
        """The rule every future consumer must use: current *and* unexpired."""
        return not self.is_superseded and not self.is_expired(at)

    def state_at(self, at: datetime) -> FactState:
        """The state to display: supersession outranks expiry, because it is history."""
        if self.is_superseded:
            return FactState.SUPERSEDED
        if self.is_expired(at):
            return FactState.EXPIRED
        return FactState.ACTIVE

    def preview(self, limit: int = 60) -> str:
        """A single-line preview of the value for list output."""
        collapsed = " ".join(self.value.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[: limit - 1] + "\u2026"


__all__ = [
    "FACT_KEY_PATTERN",
    "FACT_VALUE_MAX_LENGTH",
    "FORBIDDEN_FACT_KEY_SEGMENTS",
    "ConfirmedFact",
    "ConfirmedFactId",
    "FactCandidate",
    "FactCandidateId",
    "FactCandidateStatus",
    "FactKey",
    "FactState",
    "forbidden_segment",
    "new_confirmed_fact_id",
    "new_fact_candidate_id",
    "validate_fact_key",
    "validate_fact_value",
]
