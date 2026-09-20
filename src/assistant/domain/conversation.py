"""Tree conversation: durable history and the record of what was done about it (ADR-0033).

Four entities, and the split between them is the point:

```text
thread ──┬─► message (user / assistant)      what was said
         └─► turn ──► operation              what one user message was turned into,
                                             and which local write it authorised
```

A message is history. An operation is intent plus outcome: it is persisted as `PROPOSED`, becomes
`APPLYING` before a mutating service is called and `APPLIED` only after that service returned.
`UNKNOWN_LOCAL` is what a crash in between leaves behind, and it is never replayed automatically.

Nothing in this module knows about SQLite, the CLI, prompts or application services; the
conversation *plan* the model produces lives in `conversation_plan.py`, which is equally pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.conversation_plan import (
    ConversationOperationArguments,
    ConversationOperationType,
)
from assistant.domain.errors import (
    InvalidConversationMessage,
    InvalidConversationOperation,
    InvalidConversationThread,
)

ConversationThreadId = UUID
ConversationMessageId = UUID
ConversationTurnId = UUID
ConversationOperationId = UUID

MAX_THREAD_TITLE_CHARS = 120
"""A thread title is a label, not a transcript."""

MAX_MESSAGE_CHARS = 4000
"""One conversation message. Pasted documents belong in `pw ingest`, not in a chat turn."""

CONFIRMATION_TTL_MINUTES = 30
"""How long a `CONFIRM_LOCAL` operation waits for the user's "yes" (ADR-0033 §13)."""


def new_thread_id() -> ConversationThreadId:
    """Generate a fresh thread identity."""
    return uuid4()


def new_message_id() -> ConversationMessageId:
    """Generate a fresh message identity."""
    return uuid4()


def new_turn_id() -> ConversationTurnId:
    """Generate a fresh turn identity."""
    return uuid4()


def new_operation_id() -> ConversationOperationId:
    """Generate a fresh operation identity."""
    return uuid4()


class ConversationThreadStatus(StrEnum):
    """Whether a thread is the one the user is in."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ConversationMessageRole(StrEnum):
    """Who said it."""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationTurnStatus(StrEnum):
    """How far the runtime got with one user message."""

    PLANNED = "planned"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ConversationOperationStatus(StrEnum):
    """The durable lifecycle of one proposed operation."""

    PROPOSED = "proposed"
    WAITING_CONFIRMATION = "waiting_confirmation"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN_LOCAL = "unknown_local"


TERMINAL_OPERATION_STATUSES = frozenset(
    {
        ConversationOperationStatus.APPLIED,
        ConversationOperationStatus.REJECTED,
        ConversationOperationStatus.FAILED,
        ConversationOperationStatus.UNKNOWN_LOCAL,
    }
)
"""Statuses a crash fence may treat as finished; `UNKNOWN_LOCAL` is finished *unresolved*."""


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidConversationThread(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """One durable conversation."""

    created_at: datetime
    updated_at: datetime
    id: ConversationThreadId = field(default_factory=new_thread_id)
    title: str | None = None
    status: ConversationThreadStatus = ConversationThreadStatus.ACTIVE
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.title is not None:
            cleaned = self.title.strip()
            if not cleaned or len(cleaned) > MAX_THREAD_TITLE_CHARS:
                raise InvalidConversationThread(
                    f"a thread title must be 1-{MAX_THREAD_TITLE_CHARS} characters"
                )
        if (self.status is ConversationThreadStatus.ARCHIVED) != (self.archived_at is not None):
            raise InvalidConversationThread("archived_at must be set exactly when archived")
        if self.updated_at < self.created_at:
            raise InvalidConversationThread("updated_at cannot precede created_at")


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One user or assistant message inside one thread."""

    thread_id: ConversationThreadId
    role: ConversationMessageRole
    text: str
    created_at: datetime
    id: ConversationMessageId = field(default_factory=new_message_id)

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        cleaned = self.text.strip()
        if not cleaned:
            raise InvalidConversationMessage("a message cannot be blank")
        if len(self.text) > MAX_MESSAGE_CHARS:
            raise InvalidConversationMessage(
                f"a message is at most {MAX_MESSAGE_CHARS} characters; paste documents with "
                "`pw ingest` instead"
            )


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One user message, and what the runtime planned for it."""

    thread_id: ConversationThreadId
    user_message_id: ConversationMessageId
    interpreter_version: str
    context_fingerprint: str
    created_at: datetime
    id: ConversationTurnId = field(default_factory=new_turn_id)
    assistant_message_id: ConversationMessageId | None = None
    status: ConversationTurnStatus = ConversationTurnStatus.PLANNED
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if not self.interpreter_version.strip():
            raise InvalidConversationOperation("interpreter_version must not be blank")
        if len(self.context_fingerprint) != 64:
            raise InvalidConversationOperation("context_fingerprint must be a SHA-256 hex digest")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
        finished = self.status in (
            ConversationTurnStatus.COMPLETED,
            ConversationTurnStatus.FAILED,
            ConversationTurnStatus.INTERRUPTED,
        )
        if finished != (self.completed_at is not None):
            raise InvalidConversationOperation(
                "completed_at is set exactly for completed, failed and interrupted turns"
            )


@dataclass(frozen=True, slots=True)
class ConversationOperation:
    """One typed operation the runtime may perform, with its durable outcome."""

    turn_id: ConversationTurnId
    ordinal: int
    operation_type: ConversationOperationType
    arguments: ConversationOperationArguments
    operation_fingerprint: str
    created_at: datetime
    updated_at: datetime
    id: ConversationOperationId = field(default_factory=new_operation_id)
    status: ConversationOperationStatus = ConversationOperationStatus.PROPOSED
    result_kind: str | None = None
    result_ref: str | None = None
    confirmation_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.ordinal < 0:
            raise InvalidConversationOperation("ordinal cannot be negative")
        if len(self.operation_fingerprint) != 64:
            raise InvalidConversationOperation("operation_fingerprint must be a SHA-256 digest")
        if self.arguments.operation_type is not self.operation_type:
            raise InvalidConversationOperation(
                f"arguments for {self.arguments.operation_type.value} do not match "
                f"{self.operation_type.value}"
            )
        waiting = self.status is ConversationOperationStatus.WAITING_CONFIRMATION
        if waiting != (self.confirmation_expires_at is not None):
            raise InvalidConversationOperation(
                "confirmation_expires_at is set exactly while waiting for a confirmation"
            )
        applied = self.status is ConversationOperationStatus.APPLIED
        if applied and self.result_kind is None:
            raise InvalidConversationOperation("an applied operation records a result_kind")
        if not applied and (self.result_kind is not None or self.result_ref is not None):
            raise InvalidConversationOperation("only an applied operation carries a result")


__all__ = [
    "CONFIRMATION_TTL_MINUTES",
    "MAX_MESSAGE_CHARS",
    "MAX_THREAD_TITLE_CHARS",
    "TERMINAL_OPERATION_STATUSES",
    "ConversationMessage",
    "ConversationMessageId",
    "ConversationMessageRole",
    "ConversationOperation",
    "ConversationOperationId",
    "ConversationOperationStatus",
    "ConversationThread",
    "ConversationThreadId",
    "ConversationThreadStatus",
    "ConversationTurn",
    "ConversationTurnId",
    "ConversationTurnStatus",
    "new_message_id",
    "new_operation_id",
    "new_thread_id",
    "new_turn_id",
]
