"""Durable capacity preferences (ADR-0044)."""

from __future__ import annotations

from typing import Protocol

from assistant.domain.planning_preferences import PlanningPreferences


class PlanningPreferencesRepository(Protocol):
    """Storage for the one set of planning preferences a runtime has."""

    async def get_preferences(self) -> PlanningPreferences | None:
        """Return the stored preferences, or `None` when this host never set any.

        `None` is not an error and not a zero: it means "plan exactly as the configuration says",
        which is what a runtime upgraded from v1.2 keeps doing.
        """
        ...

    async def save_preferences(self, preferences: PlanningPreferences) -> PlanningPreferences:
        """Store the preferences, replacing any previous row.

        One row, one id: a second set of preferences is not something a caller can create by
        accident.
        """
        ...


__all__ = ["PlanningPreferencesRepository"]
