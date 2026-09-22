"""SQLite implementation of the planning-preferences repository (ADR-0044).

One row, addressed by a constant id. A singleton is the honest shape here: "which preferences apply"
must not be a question with more than one answer, and an `INSERT OR REPLACE` on that id keeps the
table incapable of holding two.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime

from assistant.domain.planning_preferences import (
    CURRENT_PREFERENCES_ID,
    PlanningPreferences,
)
from assistant.ports.clock import Clock
from assistant.store.db import Database, transaction
from assistant.store.errors import StoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

PREFERENCES_FIELDS = (
    "id, day_start_local, day_end_local, max_daily_minutes, preferred_block_minutes, "
    "max_block_minutes, created_at, updated_at"
)


class PlanningPreferencesStoreError(StoreError):
    """A stored preferences row could not be read or written."""


class SqlitePlanningPreferencesRepository:
    """Planning preferences, backed by the host runtime database."""

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def get_preferences(self) -> PlanningPreferences | None:
        return await asyncio.to_thread(self._get_sync)

    async def save_preferences(self, preferences: PlanningPreferences) -> PlanningPreferences:
        return await asyncio.to_thread(self._save_sync, preferences)

    # ------------------------------------------------------------------ blocking internals

    def _get_sync(self) -> PlanningPreferences | None:
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    f"SELECT {PREFERENCES_FIELDS} FROM planning_preferences WHERE id = ?",
                    (CURRENT_PREFERENCES_ID,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise PlanningPreferencesStoreError(
                f"could not read planning preferences: {exc}"
            ) from exc
        return None if row is None else row_to_preferences(row)

    def _save_sync(self, preferences: PlanningPreferences) -> PlanningPreferences:
        # The store owns the timestamps, because the Clock lives here: a caller that had to supply
        # them would be a second place where "now" is decided.
        now = self._clock.now()
        stamped = replace(
            preferences,
            created_at=self._get_sync_created_at() or now,
            updated_at=now,
        )
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    "INSERT INTO planning_preferences "
                    "(id, day_start_local, day_end_local, max_daily_minutes, "
                    "preferred_block_minutes, max_block_minutes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "day_start_local = excluded.day_start_local, "
                    "day_end_local = excluded.day_end_local, "
                    "max_daily_minutes = excluded.max_daily_minutes, "
                    "preferred_block_minutes = excluded.preferred_block_minutes, "
                    "max_block_minutes = excluded.max_block_minutes, "
                    "updated_at = excluded.updated_at",
                    _parameters(stamped),
                )
        except sqlite3.IntegrityError as exc:
            raise PlanningPreferencesStoreError(
                f"could not store planning preferences: {exc}"
            ) from exc
        return stamped

    def _get_sync_created_at(self) -> datetime | None:
        """The original creation moment, so an update never rewrites history."""
        existing = self._get_sync()
        return None if existing is None else existing.created_at


def _parameters(preferences: PlanningPreferences) -> tuple[object, ...]:
    # The store stamps both timestamps before it gets here, so neither can be missing in practice;
    # raising rather than substituting a clock read keeps that a visible invariant instead of a
    # silent one.
    created = preferences.created_at or preferences.updated_at
    updated = preferences.updated_at or preferences.created_at
    if created is None or updated is None:  # pragma: no cover - _save_sync always stamps both
        raise PlanningPreferencesStoreError("planning preferences need a stored timestamp")
    return (
        CURRENT_PREFERENCES_ID,
        preferences.day_start_local,
        preferences.day_end_local,
        preferences.max_daily_minutes,
        preferences.preferred_block_minutes,
        preferences.max_block_minutes,
        to_utc_iso(created),
        to_utc_iso(updated),
    )


def row_to_preferences(row: sqlite3.Row | tuple[object, ...]) -> PlanningPreferences:
    """Rebuild one preferences row."""
    return PlanningPreferences(
        id=str(row[0]),
        day_start_local=int(str(row[1])),
        day_end_local=int(str(row[2])),
        max_daily_minutes=int(str(row[3])),
        preferred_block_minutes=int(str(row[4])),
        max_block_minutes=int(str(row[5])),
        created_at=from_utc_iso(str(row[6])),
        updated_at=from_utc_iso(str(row[7])),
    )


__all__ = [
    "PREFERENCES_FIELDS",
    "PlanningPreferencesStoreError",
    "SqlitePlanningPreferencesRepository",
    "row_to_preferences",
]
