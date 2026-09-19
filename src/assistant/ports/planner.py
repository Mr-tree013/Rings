"""Planner port: pure planning logic behind a stable interface (ADR-0015).

The planner is synchronous on purpose: scheduling is pure CPU work over an immutable request,
so it needs no I/O, no clock and no event loop. A future solver can replace the greedy
implementation without touching services or storage.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.planning import PlanningRequest, PlanResult


class Planner(Protocol):
    """Turns a planning request into proposed blocks and issues."""

    def plan(self, request: PlanningRequest) -> PlanResult:
        """Return a deterministic plan for `request`."""
        ...


__all__ = ["Planner"]

