"""Accepted browser input, queued durably before anything runs (Phase 11A, ADR-0041).

```text
browser POST ──► conversation_requests(QUEUED) ──► claim ──► PROCESSING
                                                      │
                                  existing ConversationService turn
                                                      │
                                        COMPLETED / FAILED / CANCELLED / INTERRUPTED
```

A request is *accepted intent*, not history: the message the user eventually sees lives in
`conversation_messages`, and this row exists so that intent survives a page reload, a POST retry
and a process restart. Two vocabularies live here, and both are deliberately closed:

* `ConversationRequestStatus` is the durable lifecycle;
* `ConversationProgressStage` is the *safe UI* vocabulary — coarse application phases that the
  application layer chooses deterministically. There is no value for a reasoning step, a prompt or
  a provider payload, because those are never reportable (ADR-0041 §17-§18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.conversation import MAX_MESSAGE_CHARS
from assistant.domain.errors import InvalidConversationRequest

ConversationRequestId = UUID

MIN_CLIENT_REQUEST_ID_CHARS = 8
MAX_CLIENT_REQUEST_ID_CHARS = 128
"""A browser-generated id, long enough to be a UUID and short enough to be a column."""


def new_request_id() -> ConversationRequestId:
    """Generate a fresh request identity."""
    return uuid4()


class ConversationRequestStatus(StrEnum):
    """The durable lifecycle of one accepted message."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_REQUEST_STATUSES = frozenset(
    {
        ConversationRequestStatus.COMPLETED,
        ConversationRequestStatus.FAILED,
        ConversationRequestStatus.CANCELLED,
        ConversationRequestStatus.INTERRUPTED,
    }
)
"""Statuses a request never leaves. `PROCESSING` is deliberately not one of them."""


class ConversationProgressStage(StrEnum):
    """Coarse, product-level phases a browser may display. Nothing finer is reportable."""

    QUEUED = "queued"
    UNDERSTANDING = "understanding"
    READING_LOCAL_STATE = "reading_local_state"
    PLANNING = "planning"
    QUERYING_KNOWLEDGE = "querying_knowledge"
    PREPARING_MAIL = "preparing_mail"
    UPDATING_LOCAL_STATE = "updating_local_state"
    WAITING_CONFIRMATION = "waiting_confirmation"
    EXTERNAL_EXECUTION = "external_execution"
    FINALIZING = "finalizing"


CANCELLABLE_STAGES = frozenset(
    {
        ConversationProgressStage.QUEUED,
        ConversationProgressStage.UNDERSTANDING,
    }
)
"""Where Stop is a promise rather than a hope.

`QUEUED` is always cancellable. `UNDERSTANDING` is the window in which the only thing that has
happened is one model call whose answer the runtime has not acted on: abandoning it cannot leave
a local mutation behind, so cancelling there is safe *and* honest. Everything after that —
planning, reading, preparing, writing, executing — is refused (`CANNOT_CANCEL_SAFELY`) rather than
claimed, because a partially applied plan is not something a browser button can undo (ADR-0041
§28-§33).
"""


@dataclass(frozen=True, slots=True)
class ConversationRequest:
    """One durably accepted message, and how far the runtime got with it."""

    thread_id: UUID
    client_request_id: str
    created_at: datetime
    id: ConversationRequestId = field(default_factory=new_request_id)
    input_text: str | None = None
    status: ConversationRequestStatus = ConversationRequestStatus.QUEUED
    stage: ConversationProgressStage | None = None
    turn_id: UUID | None = None
    error_code: str | None = None
    cancel_requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.created_at, "created_at"),
            (self.cancel_requested_at, "cancel_requested_at"),
            (self.started_at, "started_at"),
            (self.finished_at, "finished_at"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise InvalidConversationRequest(f"{name} must be timezone-aware")
        cleaned = self.client_request_id.strip()
        if not MIN_CLIENT_REQUEST_ID_CHARS <= len(cleaned) <= MAX_CLIENT_REQUEST_ID_CHARS:
            raise InvalidConversationRequest(
                "client_request_id must be "
                f"{MIN_CLIENT_REQUEST_ID_CHARS}-{MAX_CLIENT_REQUEST_ID_CHARS} characters"
            )
        if self.input_text is not None:
            if not self.input_text.strip():
                raise InvalidConversationRequest("a request's input cannot be blank")
            if len(self.input_text) > MAX_MESSAGE_CHARS:
                raise InvalidConversationRequest(
                    f"a request's input is at most {MAX_MESSAGE_CHARS} characters"
                )
        if self.status is ConversationRequestStatus.QUEUED:
            if self.started_at is not None:
                raise InvalidConversationRequest("a queued request has not started")
            if self.finished_at is not None:
                raise InvalidConversationRequest("a queued request has not finished")
        if (self.status in TERMINAL_REQUEST_STATUSES) != (self.finished_at is not None):
            raise InvalidConversationRequest(
                "finished_at is set exactly for the terminal statuses"
            )
        if (
            self.finished_at is not None
            and self.started_at is not None
            and self.finished_at < self.started_at
        ):
            raise InvalidConversationRequest("finished_at cannot precede started_at")

    @property
    def is_terminal(self) -> bool:
        """Whether this request will never be processed again."""
        return self.status in TERMINAL_REQUEST_STATUSES

    @property
    def can_cancel(self) -> bool:
        """Whether Stop is safe *right now*, derived from durable state and nothing else."""
        if self.status is ConversationRequestStatus.QUEUED:
            return True
        if self.status is not ConversationRequestStatus.PROCESSING:
            return False
        return self.stage in CANCELLABLE_STAGES

    def with_stage(self, stage: ConversationProgressStage) -> ConversationRequest:
        """Return the same request at another coarse stage."""
        return _replace(self, stage=stage)

    def with_cancel_requested(self, at: datetime) -> ConversationRequest:
        """Return the same request with the user's stop recorded."""
        return _replace(self, cancel_requested_at=at)


def _replace(request: ConversationRequest, **changes: object) -> ConversationRequest:
    values: dict[str, object] = {
        "id": request.id,
        "thread_id": request.thread_id,
        "client_request_id": request.client_request_id,
        "input_text": request.input_text,
        "status": request.status,
        "stage": request.stage,
        "turn_id": request.turn_id,
        "error_code": request.error_code,
        "cancel_requested_at": request.cancel_requested_at,
        "created_at": request.created_at,
        "started_at": request.started_at,
        "finished_at": request.finished_at,
    }
    values.update(changes)
    return ConversationRequest(**values)  # type: ignore[arg-type]


__all__ = [
    "CANCELLABLE_STAGES",
    "MAX_CLIENT_REQUEST_ID_CHARS",
    "MIN_CLIENT_REQUEST_ID_CHARS",
    "TERMINAL_REQUEST_STATUSES",
    "ConversationProgressStage",
    "ConversationRequest",
    "ConversationRequestId",
    "ConversationRequestStatus",
    "new_request_id",
]
