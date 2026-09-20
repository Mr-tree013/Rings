"""Case: the container a multi-step piece of work lives in (ADR-0023).

A case is deliberately thin: a title and a lifecycle. What makes it useful is what it *holds* —
one or more `ActionRequest`s, each of which is an exact prepared side effect that needs its own
human approval. The case never carries authority of its own: approving one action inside a case
authorises that action and nothing else.

The lifecycle is one-way. `OPEN` may become `COMPLETED` or `CANCELLED`, and there is no reopen:
finishing something is a statement about the world, and a phase that could take it back would
make "completed" meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidCase, InvalidCaseTransition

CaseId = UUID
"""Stable identity of a case."""

CASE_TITLE_MAX_LENGTH = 500


class CaseStatus(StrEnum):
    """Lifecycle of a case. Terminal states are never left in this phase."""

    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def new_case_id() -> CaseId:
    """Generate a fresh case identity."""
    return uuid4()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCase(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def validate_case_title(title: str) -> str:
    """Return the stripped title, or raise `InvalidCase`."""
    stripped = title.strip()
    if not stripped:
        raise InvalidCase("case title must not be blank")
    if len(stripped) > CASE_TITLE_MAX_LENGTH:
        raise InvalidCase(f"case title must be at most {CASE_TITLE_MAX_LENGTH} characters")
    return stripped


@dataclass(frozen=True, slots=True)
class Case:
    """A container that is open until it is completed or cancelled."""

    title: str
    created_at: datetime
    updated_at: datetime
    id: CaseId = field(default_factory=new_case_id)
    status: CaseStatus = CaseStatus.OPEN
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", validate_case_title(self.title))
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        _require_optional_aware(self.completed_at, "completed_at")
        _require_optional_aware(self.cancelled_at, "cancelled_at")
        if self.updated_at < self.created_at:
            raise InvalidCase("updated_at must not precede created_at")
        if self.status is CaseStatus.OPEN:
            if self.completed_at is not None or self.cancelled_at is not None:
                raise InvalidCase("an OPEN case must not carry completed_at or cancelled_at")
        elif self.status is CaseStatus.COMPLETED:
            if self.completed_at is None:
                raise InvalidCase("a COMPLETED case must carry completed_at")
            if self.cancelled_at is not None:
                raise InvalidCase("a COMPLETED case must not carry cancelled_at")
        else:
            if self.cancelled_at is None:
                raise InvalidCase("a CANCELLED case must carry cancelled_at")
            if self.completed_at is not None:
                raise InvalidCase("a CANCELLED case must not carry completed_at")

    @property
    def is_open(self) -> bool:
        """Whether the case can still be changed."""
        return self.status is CaseStatus.OPEN

    def complete(self, at: datetime) -> Case:
        """Move an OPEN case to COMPLETED."""
        _require_aware(at, "at")
        if self.status is not CaseStatus.OPEN:
            raise InvalidCaseTransition(self.status, CaseStatus.COMPLETED)
        return replace(self, status=CaseStatus.COMPLETED, completed_at=at, updated_at=at)

    def cancel(self, at: datetime) -> Case:
        """Move an OPEN case to CANCELLED."""
        _require_aware(at, "at")
        if self.status is not CaseStatus.OPEN:
            raise InvalidCaseTransition(self.status, CaseStatus.CANCELLED)
        return replace(self, status=CaseStatus.CANCELLED, cancelled_at=at, updated_at=at)


__all__ = [
    "CASE_TITLE_MAX_LENGTH",
    "Case",
    "CaseId",
    "CaseStatus",
    "new_case_id",
    "validate_case_title",
]
