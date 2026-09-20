"""Deterministic mail threading and durable model analysis (ADR-0021).

Two different kinds of reasoning live in the mail pipeline, and they must not be confused:

- **thread membership is decided by code.** `Message-ID`, `In-Reply-To` and `References` are
  evidence a deterministic linker uses; when an id matches more than one stored message the
  result is `AMBIGUOUS` rather than a coin flip, and a message with no matching parent starts its
  own thread.
- **classification is decided by a model, and stored as a candidate.** A `MailAnalysis` records
  what a model said about one message: a category, whether a reply seems needed, a summary, and
  action candidates. A deadline candidate and an event-start candidate are *different* concepts,
  so they are different values, never one merged date field.

An analysis is keyed by message and carries the fingerprint of exactly what was analyzed, so a
retried event can reuse it instead of paying for the same model call twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidMailAnalysis, InvalidMailMessage
from assistant.domain.mail import MailMessageId, validate_account_id

MailThreadId = UUID
"""Stable identity of one mail thread (internal, never shown to a model)."""

MAX_SUMMARY_CHARS = 800
MAX_ACTION_CANDIDATES = 10
MAX_CANDIDATE_TEXT_CHARS = 500
MAX_TIME_TEXT_CHARS = 200


def new_mail_thread_id() -> MailThreadId:
    """Generate a fresh thread identity."""
    return uuid4()


class MailLinkStatus(StrEnum):
    """Why a message ended up where it did in the thread graph."""

    ROOT = "root"
    LINKED = "linked"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class MailCategory(StrEnum):
    """What kind of mail this is, as far as the classifier can tell."""

    ORDINARY_CORRESPONDENCE = "ordinary_correspondence"
    RECEIPT_RESULT = "receipt_result"
    ACTIONABLE_NOTICE = "actionable_notice"
    UNKNOWN = "unknown"


class MailTemporalKind(StrEnum):
    """What kind of time a candidate mentions.

    `DEADLINE` ("finish by") and `EVENT_START` ("happens at") are deliberately separate: a message
    can contain both, and collapsing them would lose the distinction the planner depends on.
    """

    NONE = "none"
    DEADLINE = "deadline"
    EVENT_START = "event_start"
    OTHER = "other"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidMailAnalysis(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MailActionCandidate:
    """A thing the mail seems to ask for, with the raw time text the model saw."""

    text: str
    temporal_kind: MailTemporalKind = MailTemporalKind.NONE
    id: UUID = field(default_factory=uuid4)
    time_text: str | None = None
    interpreted_at: datetime | None = None

    def __post_init__(self) -> None:
        stripped = self.text.strip()
        if not stripped:
            raise InvalidMailAnalysis("an action candidate needs text")
        if len(stripped) > MAX_CANDIDATE_TEXT_CHARS:
            raise InvalidMailAnalysis(
                f"action candidate text must be at most {MAX_CANDIDATE_TEXT_CHARS} characters"
            )
        object.__setattr__(self, "text", stripped)
        if self.time_text is not None:
            collapsed = " ".join(self.time_text.split())
            if not collapsed:
                object.__setattr__(self, "time_text", None)
            elif len(collapsed) > MAX_TIME_TEXT_CHARS:
                raise InvalidMailAnalysis(
                    f"time text must be at most {MAX_TIME_TEXT_CHARS} characters"
                )
            else:
                object.__setattr__(self, "time_text", collapsed)
        if self.interpreted_at is not None:
            _require_aware(self.interpreted_at, "interpreted_at")
            if self.temporal_kind is MailTemporalKind.NONE:
                raise InvalidMailAnalysis(
                    "an interpreted instant needs a temporal kind (deadline, event_start, other)"
                )
        timed_kind = self.temporal_kind in (
            MailTemporalKind.DEADLINE,
            MailTemporalKind.EVENT_START,
        )
        if timed_kind and self.time_text is None and self.interpreted_at is None:
            raise InvalidMailAnalysis(
                f"a {self.temporal_kind} candidate needs a time text or an instant"
            )

    def to_payload(self) -> dict[str, object]:
        """The JSON representation stored in the database."""
        return {
            "id": str(self.id),
            "text": self.text,
            "temporal_kind": self.temporal_kind.value,
            "time_text": self.time_text,
            "interpreted_at": (
                None
                if self.interpreted_at is None
                else self.interpreted_at.astimezone(UTC).isoformat()
            ),
        }

    @classmethod
    def from_payload(cls, payload: object) -> MailActionCandidate:
        """Rebuild a candidate from its stored JSON form."""
        if not isinstance(payload, dict):
            raise InvalidMailAnalysis("a stored action candidate must be a JSON object")
        text = payload.get("text")
        if not isinstance(text, str):
            raise InvalidMailAnalysis("a stored action candidate needs text")
        kind_value = payload.get("temporal_kind", MailTemporalKind.NONE.value)
        try:
            kind = MailTemporalKind(str(kind_value))
        except ValueError as exc:
            raise InvalidMailAnalysis(
                f"stored action candidate has an unknown temporal kind {kind_value!r}"
            ) from exc
        time_text = payload.get("time_text")
        if time_text is not None and not isinstance(time_text, str):
            raise InvalidMailAnalysis("stored time text must be a string or null")
        interpreted = payload.get("interpreted_at")
        interpreted_at: datetime | None = None
        if interpreted is not None:
            if not isinstance(interpreted, str):
                raise InvalidMailAnalysis("stored interpreted instant must be a string or null")
            try:
                interpreted_at = datetime.fromisoformat(interpreted)
            except ValueError as exc:
                raise InvalidMailAnalysis("stored interpreted instant is not ISO 8601") from exc
        candidate_id = payload.get("id")
        candidate = cls(
            text=text,
            temporal_kind=kind,
            id=UUID(str(candidate_id)) if candidate_id else uuid4(),
            time_text=time_text,
            interpreted_at=interpreted_at,
        )
        return candidate


@dataclass(frozen=True, slots=True)
class MailAnalysis:
    """One durable analysis of one message."""

    message_id: MailMessageId
    analyzer_version: int
    input_fingerprint: str
    category: MailCategory
    requires_reply: bool
    summary: str
    created_at: datetime
    updated_at: datetime
    action_candidates: tuple[MailActionCandidate, ...] = ()

    def __post_init__(self) -> None:
        if self.analyzer_version < 1:
            raise InvalidMailAnalysis("analyzer_version must be at least 1")
        if len(self.input_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.input_fingerprint
        ):
            raise InvalidMailAnalysis("input_fingerprint must be 64 lowercase hex characters")
        summary = self.summary.strip()
        if not summary:
            raise InvalidMailAnalysis("an analysis needs a summary")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise InvalidMailAnalysis(
                f"an analysis summary must be at most {MAX_SUMMARY_CHARS} characters"
            )
        object.__setattr__(self, "summary", summary)
        if len(self.action_candidates) > MAX_ACTION_CANDIDATES:
            raise InvalidMailAnalysis(
                f"an analysis may carry at most {MAX_ACTION_CANDIDATES} action candidates"
            )
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")

    @property
    def candidates_with_temporal_kind(self) -> tuple[MailActionCandidate, ...]:
        """The candidates that mention a time."""
        return tuple(
            candidate
            for candidate in self.action_candidates
            if candidate.temporal_kind is not MailTemporalKind.NONE
        )


@dataclass(frozen=True, slots=True)
class MailThread:
    """One deterministic conversation: a set of messages with a common root."""

    account_id: str
    created_at: datetime
    updated_at: datetime
    id: MailThreadId = field(default_factory=new_mail_thread_id)

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class MailThreadMember:
    """Where one message sits in the thread graph, and why."""

    message_id: MailMessageId
    thread_id: MailThreadId
    link_status: MailLinkStatus
    linked_at: datetime
    parent_message_id: MailMessageId | None = None
    link_evidence: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.linked_at, "linked_at")
        if self.link_status is MailLinkStatus.LINKED and self.parent_message_id is None:
            raise InvalidMailAnalysis("a linked member needs a parent message")
        if self.link_status is not MailLinkStatus.LINKED and self.parent_message_id is not None:
            raise InvalidMailAnalysis(
                f"a {self.link_status} member must not carry a parent message"
            )


@dataclass(frozen=True, slots=True)
class MailThreadSummary:
    """A thread with the few numbers a list view shows."""

    thread: MailThread
    message_count: int
    latest_at: datetime
    subject_preview: str | None = None

    def __post_init__(self) -> None:
        if self.message_count < 1:
            raise InvalidMailAnalysis("a thread summary needs at least one message")
        _require_aware(self.latest_at, "latest_at")
        if self.subject_preview is not None and not self.subject_preview.strip():
            object.__setattr__(self, "subject_preview", None)


ANALYZER_VERSION = 1
"""Bump when the analysis prompt or schema changes in a way that changes results."""


def mail_analysis_input_fingerprint(
    *,
    analyzer_version: int,
    schema_version: int,
    message_id: MailMessageId,
    content_fingerprint: str,
    thread_message_ids: tuple[MailMessageId, ...],
    thread_content_fingerprints: tuple[str, ...],
    planning_timezone: str | None,
) -> str:
    """Canonical fingerprint of exactly what an analysis was computed from.

    A retry must be able to tell "the same question" from "a different question", so this covers
    the analyzer version, the schema version, the message itself, the ordered thread context and
    the timezone the model was told to use. Canonical JSON in, SHA-256 out.
    """
    payload = {
        "analyzer_version": analyzer_version,
        "schema_version": schema_version,
        "message_id": str(message_id),
        "content_fingerprint": content_fingerprint,
        "thread_message_ids": [str(value) for value in thread_message_ids],
        "thread_content_fingerprints": list(thread_content_fingerprints),
        "planning_timezone": planning_timezone,
    }
    try:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise InvalidMailAnalysis(f"analysis fingerprint input is not JSON: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_action_candidates(raw: object) -> tuple[MailActionCandidate, ...]:
    """Rebuild the candidate tuple from stored JSON."""
    if not isinstance(raw, list):
        raise InvalidMailAnalysis("stored action candidates must be a JSON array")
    return tuple(MailActionCandidate.from_payload(item) for item in raw)


def validate_thread_account(message_account: str, thread_account: str) -> None:
    """Refuse to place a message in another account's thread.

    Raises:
        InvalidMailMessage: the accounts differ.
    """
    if message_account != thread_account:
        raise InvalidMailMessage(
            f"a message from {message_account!r} cannot join a {thread_account!r} thread"
        )


__all__ = [
    "ANALYZER_VERSION",
    "MAX_ACTION_CANDIDATES",
    "MAX_CANDIDATE_TEXT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TIME_TEXT_CHARS",
    "MailActionCandidate",
    "MailAnalysis",
    "MailCategory",
    "MailLinkStatus",
    "MailTemporalKind",
    "MailThread",
    "MailThreadId",
    "MailThreadMember",
    "MailThreadSummary",
    "mail_analysis_input_fingerprint",
    "new_mail_thread_id",
    "parse_action_candidates",
    "validate_thread_account",
]
