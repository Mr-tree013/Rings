"""WorkSession: the record of work that actually happened (ADR-0014).

Only work sessions answer "how long did this really take?". Durations are kept as exact
seconds (`duration_seconds`) and never rounded at rest: converting to minutes is a reporting
decision, and the database stores no redundant duration column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidWorkSession
from assistant.domain.task import TaskId

WorkSessionId = UUID
"""Stable identity of a work session."""


def new_work_session_id() -> WorkSessionId:
    """Generate a fresh work session identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidWorkSession(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WorkSession:
    """One stretch of time actually spent on a task."""

    task_id: TaskId
    started_at: datetime
    ended_at: datetime
    created_at: datetime
    id: WorkSessionId = field(default_factory=new_work_session_id)

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.ended_at, "ended_at")
        _require_aware(self.created_at, "created_at")
        if self.ended_at <= self.started_at:
            raise InvalidWorkSession("ended_at must be after started_at")

    @property
    def duration_seconds(self) -> int:
        """Exact elapsed seconds; no rounding, no minute conversion."""
        return int((self.ended_at - self.started_at).total_seconds())


__all__ = ["WorkSession", "WorkSessionId", "new_work_session_id"]

