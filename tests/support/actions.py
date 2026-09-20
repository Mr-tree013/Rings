"""Helpers for the Phase 6A tests: real stores, a scripted executor, a scripted token factory.

The executor is the only place an external side effect could happen, so the test double records
every call and can be scripted to succeed, fail, come back unknown, raise or hang. The token
factory is injected for the same reason: a deterministic token makes the "the plaintext is never
stored" regression checkable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.execution import ExecutionOutcome, ExecutionRunStatus
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.db import Database
from tests.support.fakes import FakeClock

SECRET_TOKEN = "SECRET-APPROVAL-TOKEN"
"""A recognisable plaintext that must never reach the database, a log or an error message."""

DEFAULT_ACTION_TYPE = ActionType("mail.send")


class FixedTokenFactory:
    """A token factory that hands out a known sequence."""

    def __init__(self, *tokens: str) -> None:
        self._tokens = list(tokens) or [SECRET_TOKEN]
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if not self._tokens:
            return f"{SECRET_TOKEN}-{self.calls}"
        return self._tokens.pop(0)


@dataclass
class FakeActionExecutor:
    """A scripted `ActionExecutor` that records what it was asked to do."""

    action_type: ActionType = DEFAULT_ACTION_TYPE
    outcome: ExecutionOutcome = field(default_factory=ExecutionOutcome.succeeded)
    error: BaseException | None = None
    block: bool = False
    calls: list[ActionRequest] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    def script_success(self) -> FakeActionExecutor:
        """Make the next call report a definite success."""
        self.outcome = ExecutionOutcome.succeeded()
        self.error = None
        return self

    def script_failure(self, summary: str = "the remote refused") -> FakeActionExecutor:
        """Make the next call report a definite failure."""
        self.outcome = ExecutionOutcome.failed(summary)
        self.error = None
        return self

    def script_unknown(self, summary: str = "no response") -> FakeActionExecutor:
        """Make the next call report an undecidable result."""
        self.outcome = ExecutionOutcome.unknown(summary)
        self.error = None
        return self

    def script_raise(self, error: BaseException | None = None) -> FakeActionExecutor:
        """Make the next call raise instead of reporting anything."""
        self.error = error if error is not None else RuntimeError("executor exploded")
        return self

    def script_cancel(self) -> FakeActionExecutor:
        """Make the next call be cancelled mid-flight."""
        self.error = asyncio.CancelledError()
        return self

    def script_block(self) -> FakeActionExecutor:
        """Make the next call wait until the test releases it."""
        self.block = True
        return self

    @property
    def call_count(self) -> int:
        """How many times an execution actually reached the executor."""
        return len(self.calls)

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        self.calls.append(action)
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self.outcome


@dataclass
class ActionStores:
    """The stores a Phase 6A test needs, over a real database."""

    database: Database
    clock: FakeClock
    cases: SqliteCaseRepository = field(init=False)
    actions: SqliteActionRepository = field(init=False)

    def __post_init__(self) -> None:
        self.cases = SqliteCaseRepository(self.database)
        self.actions = SqliteActionRepository(self.database)


def run_status_names() -> tuple[str, ...]:
    """Every run status name, for exhaustive parametrisation."""
    return tuple(status.value for status in ExecutionRunStatus)


__all__ = [
    "DEFAULT_ACTION_TYPE",
    "SECRET_TOKEN",
    "ActionStores",
    "FakeActionExecutor",
    "FixedTokenFactory",
    "run_status_names",
]
