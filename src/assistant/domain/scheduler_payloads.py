"""Typed payloads for the fixed scheduled-job kinds (ADR-0016).

A payload names authoritative rows and the facts the job was created from; it never copies
task content, and it is never code. When a job runs, its handler re-reads the rows and decides
whether the intent is still current — a reminder for a deadline that has since moved is
completed as obsolete rather than delivered late.

Everything here is deterministic and pure: given the same inputs it produces the same payload
bytes, the same dedup keys and therefore the same database state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from assistant.domain.deadline import DeadlineId
from assistant.domain.errors import InvalidScheduledJobPayload
from assistant.domain.scheduled_job import (
    ScheduledJob,
    ScheduledJobId,
    ScheduledJobKind,
    canonical_payload_json,
)
from assistant.domain.task import TaskId

DEADLINE_REMINDER_DEDUP_PREFIX = "deadline-reminder"
ROLLING_REPLAN_DEDUP_KEY = "rolling-replan:current-week"
"""One active rolling-replan request per planning scope: mutations coalesce into it."""

NOTIFICATION_DEDUP_PREFIX = "notification"


class RollingReplanWindowKind(StrEnum):
    """Which planning window a rolling replan asks for."""

    CURRENT_WEEK = "current_week"


def _instant_hash(moment: datetime) -> str:
    """A short, stable digest of one instant, so dedup keys stay readable."""
    canonical = moment.astimezone(UTC).isoformat()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def deadline_reminder_dedup_key(
    deadline_id: DeadlineId, *, due_at: datetime, offset_minutes: int
) -> str:
    """Identity of one reminder: deadline, its due instant, and the offset.

    The due instant is part of the key on purpose. Moving a deadline produces a new identity
    for its reminders, so a job that is already being processed can be cancelled and replaced
    instead of silently delivering the old reminder (ADR-0016).
    """
    return (
        f"{DEADLINE_REMINDER_DEDUP_PREFIX}:{deadline_id}:"
        f"{_instant_hash(due_at)}:{offset_minutes}"
    )


def deadline_reminder_notification_key(job_id: ScheduledJobId) -> str:
    """One job produces at most one notification, even across a crash and retry."""
    return f"{NOTIFICATION_DEDUP_PREFIX}:deadline:{job_id}"


def plan_ready_notification_key(job_id: ScheduledJobId) -> str:
    """One rolling-replan job produces at most one PLAN_READY notification."""
    return f"{NOTIFICATION_DEDUP_PREFIX}:plan-ready:{job_id}"


def scheduler_warning_notification_key(job_id: ScheduledJobId) -> str:
    """One job produces at most one SCHEDULER_WARNING notification."""
    return f"{NOTIFICATION_DEDUP_PREFIX}:scheduler-warning:{job_id}"


@dataclass(frozen=True, slots=True)
class DeadlineReminderPayload:
    """What a deadline reminder needs: which deadline, from which due instant, at what offset."""

    task_id: TaskId
    deadline_id: DeadlineId
    deadline_due_at: datetime
    reminder_offset_minutes: int

    def __post_init__(self) -> None:
        if self.deadline_due_at.tzinfo is None or self.deadline_due_at.utcoffset() is None:
            raise InvalidScheduledJobPayload("deadline_due_at must be timezone-aware")
        if self.reminder_offset_minutes < 0:
            raise InvalidScheduledJobPayload("reminder_offset_minutes must not be negative")

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON object stored on the job."""
        return {
            "task_id": str(self.task_id),
            "deadline_id": str(self.deadline_id),
            "deadline_due_at": self.deadline_due_at.astimezone(UTC).isoformat(),
            "reminder_offset_minutes": self.reminder_offset_minutes,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> DeadlineReminderPayload:
        """Parse and validate a stored payload.

        Raises:
            InvalidScheduledJobPayload: a required field is missing or malformed.
        """
        return cls(
            task_id=_uuid_field(payload, "task_id"),
            deadline_id=_uuid_field(payload, "deadline_id"),
            deadline_due_at=_instant_field(payload, "deadline_due_at"),
            reminder_offset_minutes=_int_field(payload, "reminder_offset_minutes"),
        )


@dataclass(frozen=True, slots=True)
class RollingReplanPayload:
    """What a rolling replan needs: the timezone it plans in, and which window."""

    timezone: str
    window_kind: RollingReplanWindowKind = RollingReplanWindowKind.CURRENT_WEEK

    def __post_init__(self) -> None:
        if not self.timezone.strip():
            raise InvalidScheduledJobPayload("timezone must not be blank")

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON object stored on the job."""
        return {"timezone": self.timezone, "window_kind": self.window_kind.value}

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> RollingReplanPayload:
        """Parse and validate a stored payload."""
        window_kind = payload.get("window_kind")
        if not isinstance(window_kind, str):
            raise InvalidScheduledJobPayload("window_kind must be a string")
        try:
            kind = RollingReplanWindowKind(window_kind)
        except ValueError as exc:
            raise InvalidScheduledJobPayload(
                f"unknown rolling replan window_kind {window_kind!r}"
            ) from exc
        return cls(timezone=_str_field(payload, "timezone"), window_kind=kind)


def deadline_reminder_jobs(
    *,
    task_id: TaskId,
    deadline_id: DeadlineId,
    deadline_due_at: datetime,
    offsets_minutes: Sequence[int],
    now: datetime,
) -> tuple[ScheduledJob, ...]:
    """Build the reminder jobs one deadline should have after a mutation.

    A reminder whose time has already passed is due *now* rather than dropped: creating a task
    that is due in twenty minutes must still remind the user (ADR-0016).
    """
    jobs: list[ScheduledJob] = []
    for offset in offsets_minutes:
        payload = DeadlineReminderPayload(
            task_id=task_id,
            deadline_id=deadline_id,
            deadline_due_at=deadline_due_at,
            reminder_offset_minutes=offset,
        )
        due_at = deadline_due_at - timedelta(minutes=offset)
        jobs.append(
            ScheduledJob(
                kind=ScheduledJobKind.DEADLINE_REMINDER,
                due_at=max(due_at, now),
                dedup_key=deadline_reminder_dedup_key(
                    deadline_id, due_at=deadline_due_at, offset_minutes=offset
                ),
                payload_json=canonical_payload_json(payload.to_payload()),
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(jobs)


def parse_deadline_reminder_payload(payload_json: str) -> DeadlineReminderPayload:
    """Parse a DEADLINE_REMINDER payload from its stored canonical JSON."""
    return DeadlineReminderPayload.from_payload(_decode(payload_json))


def parse_rolling_replan_payload(payload_json: str) -> RollingReplanPayload:
    """Parse a ROLLING_REPLAN payload from its stored canonical JSON."""
    return RollingReplanPayload.from_payload(_decode(payload_json))


def _decode(payload_json: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload_json)
    except ValueError as exc:
        raise InvalidScheduledJobPayload(f"job payload is not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise InvalidScheduledJobPayload("a job payload must be a JSON object")
    return decoded


def _str_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidScheduledJobPayload(f"{key} must be a non-blank string")
    return value


def _int_field(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidScheduledJobPayload(f"{key} must be an integer")
    return value


def _uuid_field(payload: dict[str, object], key: str) -> UUID:
    value = _str_field(payload, key)
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidScheduledJobPayload(f"{key} must be a UUID") from exc


def _instant_field(payload: dict[str, object], key: str) -> datetime:
    value = _str_field(payload, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidScheduledJobPayload(f"{key} must be an ISO 8601 instant") from exc
    if parsed.tzinfo is None:
        raise InvalidScheduledJobPayload(f"{key} must be a timezone-aware instant")
    return parsed


__all__ = [
    "DEADLINE_REMINDER_DEDUP_PREFIX",
    "NOTIFICATION_DEDUP_PREFIX",
    "ROLLING_REPLAN_DEDUP_KEY",
    "DeadlineReminderPayload",
    "RollingReplanPayload",
    "RollingReplanWindowKind",
    "deadline_reminder_dedup_key",
    "deadline_reminder_jobs",
    "deadline_reminder_notification_key",
    "parse_deadline_reminder_payload",
    "parse_rolling_replan_payload",
    "plan_ready_notification_key",
    "scheduler_warning_notification_key",
]
