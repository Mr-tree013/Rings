"""Unified attention: derived, user-facing "this needs you" state (ADR-0042).

Attention is the one place where the product says *"something in your own state needs a human"*
without making the user learn which subsystem noticed it. It is a projection, and the four words
that describe it are all load-bearing:

* **derived.** An item is computed from durable source rows — a task, a deadline, a mail analysis,
  a plan proposal, a candidate, a waiting review, an execution run, an unread notification, an
  observation. Nothing here is a second authority: resolving the source resolves the item.
* **deterministic.** No model decides whether something needs attention; the projector is ordinary
  application logic over existing state. A model may *read* attention and *settle* an item the
  human asked it to settle, and nothing else.
* **bounded.** `ATTENTION_LIMIT` caps the inbox, severity then age orders it, and the vocabulary of
  kinds is closed — there is no `AI_IMPORTANT_THING`, because a kind that exists is a kind with a
  projector rule, a resolution rule and a test behind it.
* **non-executing.** An attention item can be acknowledged or dismissed. It can never approve,
  send, submit or execute anything; "this needs you" is not "this was done for you".

The identity rules are the subtle part. `dedupe_key` is the *stable logical* identity of one
pending situation (`task-overdue:<task_id>`), `source_fingerprint` is a SHA-256 digest of the
materially relevant source state, and `generation` counts how many materially different versions of
that situation have been surfaced. A refresh that finds the same fingerprint changes nothing; a
refresh that finds a different one resolves the old row and opens generation N+1, so a dismissed
reminder cannot silently swallow a new deadline.

Nothing in this module knows about SQLite, a model, a mail server or a browser.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidAttentionItem

AttentionItemId = UUID
"""Stable identity of one attention row."""

ATTENTION_LIMIT = 100
"""How many attention items one inbox may hold. Attention is an inbox, not a feed."""

MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 500
MAX_DEDUPE_KEY_CHARS = 200
MAX_SOURCE_ID_CHARS = 200


def new_attention_item_id() -> AttentionItemId:
    """Generate a fresh attention identity."""
    return uuid4()


class AttentionKind(StrEnum):
    """The closed list of situations this product surfaces.

    Every member has a deterministic projector rule, a source that resolves it, and a test. A new
    member without those three is not a feature, so it is not a member.
    """

    TASK_OVERDUE = "task_overdue"
    TASK_DUE_SOON = "task_due_soon"
    PLAN_BLOCK_PASSED = "plan_block_passed"
    MAIL_REQUIRES_REPLY = "mail_requires_reply"
    PLAN_WAITING = "plan_waiting"
    FACT_WAITING = "fact_waiting"
    RECURRING_WAITING = "recurring_waiting"
    EXTERNAL_REVIEW_WAITING = "external_review_waiting"
    EXTERNAL_EXECUTION_UNKNOWN = "external_execution_unknown"
    WATCHER_OBSERVATION = "watcher_observation"
    NOTIFICATION = "notification"


class AttentionSourceType(StrEnum):
    """Which durable row an item points at. The source stays the authority."""

    TASK = "task"
    DEADLINE = "deadline"
    PLAN_BLOCK = "plan_block"
    MAIL_MESSAGE = "mail_message"
    PLAN_PROPOSAL = "plan_proposal"
    FACT_CANDIDATE = "fact_candidate"
    RECURRING_RULE = "recurring_rule"
    CONVERSATION_OPERATION = "conversation_operation"
    CONVERSATION_REVIEW = "conversation_review"
    EXECUTION_RUN = "execution_run"
    NOTIFICATION = "notification"
    WEB_OBSERVATION = "web_observation"


class AttentionStatus(StrEnum):
    """The lifecycle (§9). `RESOLVED` is terminal; the rest are live."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"

    @property
    def is_live(self) -> bool:
        """Whether this item still occupies its dedupe key."""
        return self is not AttentionStatus.RESOLVED


class AttentionSeverity(StrEnum):
    """How loudly an item asks. Three levels, deliberately, and they sort."""

    INFO = "info"
    NORMAL = "normal"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """Higher is more urgent. Used for ordering, never for authority."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    AttentionSeverity.INFO: 0,
    AttentionSeverity.NORMAL: 1,
    AttentionSeverity.HIGH: 2,
}

LIVE_ATTENTION_STATUSES = (
    AttentionStatus.OPEN,
    AttentionStatus.ACKNOWLEDGED,
    AttentionStatus.DISMISSED,
)
"""The statuses that occupy a dedupe key. Exactly the ones that are not `RESOLVED`."""


def source_fingerprint(parts: dict[str, object]) -> str:
    """The SHA-256 digest of a canonical description of material source state.

    The caller names only the fields a human would call "the same situation". Two refreshes that
    see identical material state hash identically and therefore write nothing.
    """
    canonical = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidAttentionItem(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """One durable "this needs you" row, derived from exactly one source."""

    kind: AttentionKind
    source_type: AttentionSourceType
    source_id: str
    dedupe_key: str
    fingerprint: str
    severity: AttentionSeverity
    title: str
    created_at: datetime
    updated_at: datetime
    id: AttentionItemId = field(default_factory=new_attention_item_id)
    status: AttentionStatus = AttentionStatus.OPEN
    summary: str | None = None
    generation: int = 1
    acknowledged_at: datetime | None = None
    dismissed_at: datetime | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in ((self.kind, "kind"), (self.source_type, "source_type")):
            if not isinstance(value, (AttentionKind, AttentionSourceType)):
                raise InvalidAttentionItem(f"{name} must be a typed value")
        if not isinstance(self.status, AttentionStatus):
            raise InvalidAttentionItem("status must be an AttentionStatus")
        if not isinstance(self.severity, AttentionSeverity):
            raise InvalidAttentionItem("severity must be an AttentionSeverity")
        key = self.dedupe_key.strip()
        if not key or len(key) > MAX_DEDUPE_KEY_CHARS:
            raise InvalidAttentionItem(
                f"a dedupe key must be 1-{MAX_DEDUPE_KEY_CHARS} characters"
            )
        source_id = self.source_id.strip()
        if not source_id or len(source_id) > MAX_SOURCE_ID_CHARS:
            raise InvalidAttentionItem(
                f"a source id must be 1-{MAX_SOURCE_ID_CHARS} characters"
            )
        if len(self.fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.fingerprint
        ):
            raise InvalidAttentionItem("fingerprint must be a lowercase SHA-256 hex digest")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise InvalidAttentionItem("generation must be a positive integer")
        title = " ".join(self.title.split())
        if not title or len(title) > MAX_TITLE_CHARS:
            raise InvalidAttentionItem(
                f"a title must be 1-{MAX_TITLE_CHARS} characters"
            )
        object.__setattr__(self, "title", title)
        if self.summary is not None:
            summary = " ".join(self.summary.split())
            if len(summary) > MAX_SUMMARY_CHARS:
                raise InvalidAttentionItem(
                    f"a summary must be at most {MAX_SUMMARY_CHARS} characters"
                )
            object.__setattr__(self, "summary", summary or None)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise InvalidAttentionItem("updated_at must not precede created_at")
        _require_lifecycle(self)

    @property
    def is_live(self) -> bool:
        """Whether this item still needs the user's inbox slot."""
        return self.status.is_live

    @property
    def is_open(self) -> bool:
        """Whether nobody has acknowledged or dismissed it yet."""
        return self.status is AttentionStatus.OPEN

    @property
    def is_dismissed(self) -> bool:
        """Whether the user asked not to be reminded about this generation."""
        return self.status is AttentionStatus.DISMISSED

    @property
    def settled_at(self) -> datetime | None:
        """When the user settled it, if they did."""
        return self.acknowledged_at or self.dismissed_at

    def acknowledge(self, at: datetime) -> AttentionItem:
        """Mark it seen. A resolved item, or one already acknowledged, is unchanged."""
        _require_aware(at, "at")
        if self.status is not AttentionStatus.OPEN:
            return self
        return replace(
            self, status=AttentionStatus.ACKNOWLEDGED, acknowledged_at=at, updated_at=at
        )

    def dismiss(self, at: datetime) -> AttentionItem:
        """Stop reminding about this generation without touching the source (ADR-0042 §11)."""
        _require_aware(at, "at")
        if self.status is not AttentionStatus.OPEN:
            return self
        return replace(
            self, status=AttentionStatus.DISMISSED, dismissed_at=at, updated_at=at
        )

    def resolve(self, at: datetime) -> AttentionItem:
        """Close it because the source no longer needs anyone. Idempotent."""
        _require_aware(at, "at")
        if self.status is AttentionStatus.RESOLVED:
            return self
        return replace(
            self,
            status=AttentionStatus.RESOLVED,
            acknowledged_at=None,
            dismissed_at=None,
            resolved_at=at,
            updated_at=at,
        )

    def refreshed(
        self,
        *,
        severity: AttentionSeverity,
        title: str,
        summary: str | None,
        at: datetime,
    ) -> AttentionItem:
        """Return the same generation with re-rendered words and no lifecycle change."""
        return replace(
            self, severity=severity, title=title, summary=summary, updated_at=at
        )

    def next_generation(
        self,
        *,
        kind: AttentionKind,
        fingerprint: str,
        severity: AttentionSeverity,
        title: str,
        summary: str | None,
        at: datetime,
    ) -> AttentionItem:
        """Open a new generation of the same situation after the source materially changed.

        The kind may change too — a task that was merely due soon becomes overdue — because the
        *situation* is the identity, and the words describing it are not.
        """
        return AttentionItem(
            kind=kind,
            source_type=self.source_type,
            source_id=self.source_id,
            dedupe_key=self.dedupe_key,
            fingerprint=fingerprint,
            severity=severity,
            title=title,
            summary=summary,
            created_at=at,
            updated_at=at,
            generation=self.generation + 1,
        )

    def to_payload(self) -> dict[str, object]:
        """The bounded representation a browser or a conversation may show.

        Only product language travels: the kind, a human title and summary, a severity, a status and
        timestamps. The source identity is an opaque local id, never a path, a body or a credential.
        """
        return {
            "id": str(self.id),
            "kind": self.kind.value,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "status": self.status.value,
            "severity": self.severity.value,
            "title": self.title,
            "summary": self.summary,
            "generation": self.generation,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _require_lifecycle(item: AttentionItem) -> None:
    """Refuse a row whose status and timestamps disagree (mirrors the DB CHECK)."""
    stamp_for = {
        AttentionStatus.OPEN: None,
        AttentionStatus.ACKNOWLEDGED: item.acknowledged_at,
        AttentionStatus.DISMISSED: item.dismissed_at,
        AttentionStatus.RESOLVED: item.resolved_at,
    }[item.status]
    if item.status is not AttentionStatus.OPEN and stamp_for is None:
        raise InvalidAttentionItem(f"a {item.status.value} item needs its own timestamp")
    if item.status is AttentionStatus.OPEN and any(
        value is not None
        for value in (item.acknowledged_at, item.dismissed_at, item.resolved_at)
    ):
        raise InvalidAttentionItem("an open item must not carry a settlement timestamp")
    for value, name in (
        (item.acknowledged_at, "acknowledged_at"),
        (item.dismissed_at, "dismissed_at"),
        (item.resolved_at, "resolved_at"),
    ):
        if value is not None:
            _require_aware(value, name)
    for value, status, name in (
        (item.acknowledged_at, AttentionStatus.ACKNOWLEDGED, "acknowledged_at"),
        (item.dismissed_at, AttentionStatus.DISMISSED, "dismissed_at"),
        (item.resolved_at, AttentionStatus.RESOLVED, "resolved_at"),
    ):
        if value is not None and item.status is not status:
            raise InvalidAttentionItem(f"only a {status.value} item may carry {name}")


def sort_key(item: AttentionItem) -> tuple[int, datetime, str]:
    """Most urgent first, then oldest first, then a stable tie-break."""
    return (-item.severity.rank, item.created_at, str(item.id))


# --------------------------------------------------------------------------- dedupe keys
#
# One builder per kind, in one place, so identity is a property of the product rather than of
# whichever module happened to build the string. Every key names the durable source, never a
# rendered sentence: rewording a title must not create a second inbox row.


def task_dedupe_key(task_id: object) -> str:
    """`task-overdue:<task_id>` — one pending due/overdue situation per task."""
    return f"task-overdue:{task_id}"


def plan_block_dedupe_key(plan_block_id: object) -> str:
    """`plan-passed:<plan_block_id>` — one passed-but-open plan block per block."""
    return f"plan-passed:{plan_block_id}"


def mail_reply_dedupe_key(message_id: object) -> str:
    """`mail-reply:<message_id>` — one message that still asks for an answer."""
    return f"mail-reply:{message_id}"


def plan_waiting_dedupe_key(proposal_id: object) -> str:
    """`plan-waiting:<proposal_id>` — one pending proposal per proposal."""
    return f"plan-waiting:{proposal_id}"


def fact_waiting_dedupe_key(candidate_id: object) -> str:
    """`fact-waiting:<candidate_id>` — one candidate waiting to be confirmed."""
    return f"fact-waiting:{candidate_id}"


def recurring_waiting_dedupe_key(operation_id: object) -> str:
    """`recurring-waiting:<operation_id>` — one unconfirmed weekly-rule group per turn."""
    return f"recurring-waiting:{operation_id}"


def external_review_dedupe_key(review_id: object) -> str:
    """`external-review:<review_id>` — one prepared external action awaiting a human."""
    return f"external-review:{review_id}"


def execution_unknown_dedupe_key(run_id: object) -> str:
    """`execution-unknown:<run_id>` — one unresolved external outcome."""
    return f"execution-unknown:{run_id}"


def notification_dedupe_key(notification_id: object) -> str:
    """`notification:<notification_id>` — one unread durable notification."""
    return f"notification:{notification_id}"


def watcher_dedupe_key(observation_id: object) -> str:
    """`watcher-observation:<observation_id>` — one actionable observation."""
    return f"watcher-observation:{observation_id}"


__all__ = [
    "ATTENTION_LIMIT",
    "LIVE_ATTENTION_STATUSES",
    "MAX_DEDUPE_KEY_CHARS",
    "MAX_SOURCE_ID_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TITLE_CHARS",
    "AttentionItem",
    "AttentionItemId",
    "AttentionKind",
    "AttentionSeverity",
    "AttentionSourceType",
    "AttentionStatus",
    "execution_unknown_dedupe_key",
    "external_review_dedupe_key",
    "fact_waiting_dedupe_key",
    "mail_reply_dedupe_key",
    "new_attention_item_id",
    "notification_dedupe_key",
    "plan_block_dedupe_key",
    "plan_waiting_dedupe_key",
    "recurring_waiting_dedupe_key",
    "sort_key",
    "source_fingerprint",
    "task_dedupe_key",
    "watcher_dedupe_key",
]
