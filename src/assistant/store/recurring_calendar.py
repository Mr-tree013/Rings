"""SQLite implementation of the recurring-rule repository (ADR-0036 §3)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from uuid import UUID

from assistant.domain.errors import RecurringRuleNotFound
from assistant.domain.recurring_calendar import (
    RecurringCalendarRule,
    RecurringCalendarRuleId,
    RecurringCalendarRuleStatus,
    format_clock,
    parse_clock,
)
from assistant.store.db import Database, transaction
from assistant.store.serialization import from_utc_iso, to_utc_iso

RULE_FIELDS = (
    "id, title, weekday, start_local_time, end_local_time, timezone, starts_on, ends_on, "
    "status, rule_fingerprint, created_at, updated_at, retired_at"
)


class SqliteRecurringCalendarRepository:
    """Weekly recurring rules, backed by the host runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_rule(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        return await asyncio.to_thread(self._add_sync, rule)

    async def update_rule(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        return await asyncio.to_thread(self._update_sync, rule)

    async def get_rule(
        self, rule_id: RecurringCalendarRuleId
    ) -> RecurringCalendarRule | None:
        return await asyncio.to_thread(self._get_sync, rule_id)

    async def find_active_by_fingerprint(
        self, fingerprint: str
    ) -> RecurringCalendarRule | None:
        return await asyncio.to_thread(self._find_sync, fingerprint)

    async def list_rules(
        self, *, status: RecurringCalendarRuleStatus | None = RecurringCalendarRuleStatus.ACTIVE
    ) -> list[RecurringCalendarRule]:
        return await asyncio.to_thread(self._list_sync, status)

    def _add_sync(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                f"INSERT INTO recurring_calendar_rules ({RULE_FIELDS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _values(rule),
            )
        return rule

    def _update_sync(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        with self._database.connect() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE recurring_calendar_rules SET title = ?, weekday = ?, "
                "start_local_time = ?, end_local_time = ?, timezone = ?, starts_on = ?, "
                "ends_on = ?, status = ?, rule_fingerprint = ?, updated_at = ?, retired_at = ? "
                "WHERE id = ?",
                (
                    rule.title,
                    rule.weekday,
                    format_clock(rule.start_time),
                    format_clock(rule.end_time),
                    rule.timezone,
                    rule.starts_on.isoformat(),
                    None if rule.ends_on is None else rule.ends_on.isoformat(),
                    rule.status.value,
                    rule.fingerprint,
                    to_utc_iso(rule.updated_at),
                    None if rule.retired_at is None else to_utc_iso(rule.retired_at),
                    str(rule.id),
                ),
            )
            if cursor.rowcount != 1:
                raise RecurringRuleNotFound(rule.id)
        return rule

    def _get_sync(self, rule_id: RecurringCalendarRuleId) -> RecurringCalendarRule | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {RULE_FIELDS} FROM recurring_calendar_rules WHERE id = ?",
                (str(rule_id),),
            ).fetchone()
        return None if row is None else row_to_rule(row)

    def _find_sync(self, fingerprint: str) -> RecurringCalendarRule | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {RULE_FIELDS} FROM recurring_calendar_rules "
                "WHERE rule_fingerprint = ? AND status = ? ORDER BY created_at LIMIT 1",
                (fingerprint, RecurringCalendarRuleStatus.ACTIVE.value),
            ).fetchone()
        return None if row is None else row_to_rule(row)

    def _list_sync(
        self, status: RecurringCalendarRuleStatus | None
    ) -> list[RecurringCalendarRule]:
        query = (
            f"SELECT {RULE_FIELDS} FROM recurring_calendar_rules ORDER BY created_at, rowid"
        )
        parameters: tuple[object, ...] = ()
        if status is not None:
            query = (
                f"SELECT {RULE_FIELDS} FROM recurring_calendar_rules WHERE status = ? "
                "ORDER BY created_at, rowid"
            )
            parameters = (status.value,)
        with self._database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [row_to_rule(row) for row in rows]


def _values(rule: RecurringCalendarRule) -> tuple[object, ...]:
    return (
        str(rule.id),
        rule.title,
        rule.weekday,
        format_clock(rule.start_time),
        format_clock(rule.end_time),
        rule.timezone,
        rule.starts_on.isoformat(),
        None if rule.ends_on is None else rule.ends_on.isoformat(),
        rule.status.value,
        rule.fingerprint,
        to_utc_iso(rule.created_at),
        to_utc_iso(rule.updated_at),
        None if rule.retired_at is None else to_utc_iso(rule.retired_at),
    )


def row_to_rule(row: sqlite3.Row | tuple[object, ...]) -> RecurringCalendarRule:
    """Rebuild one rule from its row."""
    return RecurringCalendarRule(
        id=UUID(str(row[0])),
        title=str(row[1]),
        weekday=int(str(row[2])),
        start_time=parse_clock(str(row[3])),
        end_time=parse_clock(str(row[4])),
        timezone=str(row[5]),
        starts_on=date.fromisoformat(str(row[6])),
        ends_on=None if row[7] is None else date.fromisoformat(str(row[7])),
        status=RecurringCalendarRuleStatus(str(row[8])),
        created_at=from_utc_iso(str(row[10])),
        updated_at=from_utc_iso(str(row[11])),
        retired_at=None if row[12] is None else from_utc_iso(str(row[12])),
    )


__all__ = ["RULE_FIELDS", "SqliteRecurringCalendarRepository", "row_to_rule"]
