"""Notification: a durable message waiting for the user to read it (ADR-0016).

The notification inbox is the single delivery target in this phase. No desktop popup, no
email and no push adapter exists yet, and none is simulated: the scheduler writes a row, the
CLI reads it, and a future mobile or web client will read the same rows.

Uniqueness is enforced on `dedup_key` by the database, because a notification is created by an
at-least-once job: the same job may be executed again after a crash, and the second execution
must find the original message instead of producing a second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidNotification
from assistant.domain.planning import PlanProposalId
from assistant.domain.task import TaskId

NotificationId = UUID
"""Stable identity of one notification."""


def new_notification_id() -> NotificationId:
    """Generate a fresh notification identity."""
    return uuid4()


class NotificationKind(StrEnum):
    """Why the user is being told something."""

    DEADLINE_REMINDER = "deadline_reminder"
    PLAN_READY = "plan_ready"
    SCHEDULER_WARNING = "scheduler_warning"


class NotificationStatus(StrEnum):
    """Whether the user has seen it."""

    UNREAD = "unread"
    READ = "read"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidNotification(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Notification:
    """One durable message in the inbox."""

    kind: NotificationKind
    title: str
    body: str
    dedup_key: str
    created_at: datetime
    id: NotificationId = field(default_factory=new_notification_id)
    status: NotificationStatus = NotificationStatus.UNREAD
    related_task_id: TaskId | None = None
    related_proposal_id: PlanProposalId | None = None
    read_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise InvalidNotification("a notification needs a non-blank title")
        if not self.body.strip():
            raise InvalidNotification("a notification needs a non-blank body")
        if not self.dedup_key.strip():
            raise InvalidNotification("a notification needs a non-blank dedup key")
        _require_aware(self.created_at, "created_at")
        if self.status is NotificationStatus.UNREAD:
            if self.read_at is not None:
                raise InvalidNotification("an UNREAD notification must not have read_at")
        else:
            if self.read_at is None:
                raise InvalidNotification("a READ notification needs read_at")
            _require_aware(self.read_at, "read_at")

    @property
    def is_unread(self) -> bool:
        """Whether the user still has to look at this."""
        return self.status is NotificationStatus.UNREAD

    def mark_read(self, at: datetime) -> Notification:
        """Mark the notification read. Reading an already-read notification changes nothing."""
        _require_aware(at, "at")
        if self.status is NotificationStatus.READ:
            return self
        return replace(self, status=NotificationStatus.READ, read_at=at)


__all__ = [
    "Notification",
    "NotificationId",
    "NotificationKind",
    "NotificationStatus",
    "new_notification_id",
]
