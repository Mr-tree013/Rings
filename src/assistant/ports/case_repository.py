"""CaseRepository port: the durable container a multi-step action lives in (ADR-0023).

`updated_at` is the optimistic concurrency token, exactly as it is for tasks: a terminal
transition presents the value the caller read, and a mismatch raises `StaleCaseUpdate` rather
than silently completing a case somebody else already moved.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Protocol

from assistant.domain.case import Case, CaseId, CaseStatus


class CaseRepository(Protocol):
    """Durable cases and their lifecycle."""

    async def add_case(self, case: Case) -> Case:
        """Store a new case."""
        ...

    async def get_case(self, case_id: CaseId) -> Case | None:
        """Return one case, or `None`."""
        ...

    async def list_cases(
        self, *, statuses: Collection[CaseStatus] | None = None, limit: int | None = 20
    ) -> list[Case]:
        """List cases, newest update first."""
        ...

    async def update_case(self, case: Case, *, expected_updated_at: datetime) -> Case:
        """Replace a case if it is still at `expected_updated_at`.

        Raises:
            CaseNotFound: no such case.
            StaleCaseUpdate: someone else moved the case first.
        """
        ...

    async def resolve_case_id(self, reference: str) -> CaseId:
        """Resolve a full UUID or a unique prefix to a case id.

        Raises:
            CaseNotFound: nothing matches.
            AmbiguousId: several cases match.
        """
        ...

    async def count_cases(self, *, statuses: Collection[CaseStatus] | None = None) -> int:
        """How many cases exist in the given statuses (all of them when omitted)."""
        ...


__all__ = ["CaseRepository"]
