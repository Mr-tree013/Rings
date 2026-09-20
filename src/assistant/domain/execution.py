"""ExecutionRun: the record of one attempt to perform one approved action (ADR-0023).

The status set is chosen for what it means when things go wrong:

- `RUNNING` — an approval has been consumed and the attempt has begun. A run left here is what a
  crash looks like, and it is deliberately *not* resolved automatically;
- `SUCCEEDED` — the executor reported a definite success. Only then does the action become
  `EXECUTED`;
- `FAILED` — the executor reported a definite failure. The action stays `PREPARED`, and trying
  again requires a *new* human approval, because the old one was spent;
- `UNKNOWN` — the attempt ended without a decidable result: the side effect may or may not have
  happened. Nothing may retry it automatically. Ever, until a future executor-specific
  reconciliation says otherwise.

`ExecutionOutcome` is what an executor returns. It deliberately cannot express `RUNNING`: only
the execution service starts a run, and it does so before the executor is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.action import ActionRequestId
from assistant.domain.approval import ApprovalId
from assistant.domain.errors import InvalidExecutionRun

ExecutionRunId = UUID
"""Stable identity of one execution attempt."""

MAX_ERROR_SUMMARY_CHARS = 1000


def new_execution_run_id() -> ExecutionRunId:
    """Generate a fresh execution identity."""
    return uuid4()


class ExecutionRunStatus(StrEnum):
    """How one attempt ended — or that it has not ended yet."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidExecutionRun(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What an executor reports. Never `RUNNING`: starting a run is the service's job."""

    status: ExecutionRunStatus
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if self.status is ExecutionRunStatus.RUNNING:
            raise InvalidExecutionRun(
                "an executor result must be a finished outcome, not RUNNING"
            )
        if self.error_summary is not None:
            summary = self.error_summary.strip()
            if not summary:
                object.__setattr__(self, "error_summary", None)
            elif len(summary) > MAX_ERROR_SUMMARY_CHARS:
                object.__setattr__(
                    self, "error_summary", summary[: MAX_ERROR_SUMMARY_CHARS - 1] + "\u2026"
                )
            else:
                object.__setattr__(self, "error_summary", summary)
        if self.status is not ExecutionRunStatus.SUCCEEDED and not self.error_summary:
            # A failure without a reason is still a failure; say so rather than storing nothing.
            object.__setattr__(
                self,
                "error_summary",
                f"the executor reported {self.status} without a reason",
            )

    @classmethod
    def succeeded(cls) -> ExecutionOutcome:
        """A definite success."""
        return cls(status=ExecutionRunStatus.SUCCEEDED)

    @classmethod
    def failed(cls, summary: str) -> ExecutionOutcome:
        """A definite failure."""
        return cls(status=ExecutionRunStatus.FAILED, error_summary=summary)

    @classmethod
    def unknown(cls, summary: str) -> ExecutionOutcome:
        """An undecidable result: the side effect may or may not have happened."""
        return cls(status=ExecutionRunStatus.UNKNOWN, error_summary=summary)


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    """One attempt, started by consuming one approval."""

    action_id: ActionRequestId
    approval_id: ApprovalId
    started_at: datetime
    id: ExecutionRunId = field(default_factory=new_execution_run_id)
    status: ExecutionRunStatus = ExecutionRunStatus.RUNNING
    finished_at: datetime | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        _require_optional_aware(self.finished_at, "finished_at")
        if self.status is ExecutionRunStatus.RUNNING:
            if self.finished_at is not None:
                raise InvalidExecutionRun("a RUNNING execution must not be finished")
        elif self.finished_at is None:
            raise InvalidExecutionRun(
                f"a {self.status} execution must carry finished_at"
            )
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise InvalidExecutionRun("finished_at must not precede started_at")
        if self.error_summary is not None and not self.error_summary.strip():
            object.__setattr__(self, "error_summary", None)

    @property
    def is_running(self) -> bool:
        """Whether the attempt has not been resolved."""
        return self.status is ExecutionRunStatus.RUNNING

    @property
    def blocks_retry(self) -> bool:
        """Whether this run makes a further execution unsafe without reconciliation.

        A crash (`RUNNING`) and an undecidable result (`UNKNOWN`) both mean the outside world may
        already have changed. Neither may be retried blindly.
        """
        return self.status in (ExecutionRunStatus.RUNNING, ExecutionRunStatus.UNKNOWN)

    def finish(self, outcome: ExecutionOutcome, *, at: datetime) -> ExecutionRun:
        """Return a finished copy of a RUNNING execution."""
        _require_aware(at, "at")
        if not self.is_running:
            raise InvalidExecutionRun(
                f"execution {self.id} already ended as {self.status}"
            )
        return ExecutionRun(
            id=self.id,
            action_id=self.action_id,
            approval_id=self.approval_id,
            status=outcome.status,
            started_at=self.started_at,
            finished_at=at,
            error_summary=outcome.error_summary,
        )


__all__ = [
    "MAX_ERROR_SUMMARY_CHARS",
    "ExecutionOutcome",
    "ExecutionRun",
    "ExecutionRunId",
    "ExecutionRunStatus",
    "new_execution_run_id",
]
