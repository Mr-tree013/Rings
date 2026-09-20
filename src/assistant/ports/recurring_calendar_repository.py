"""Durable weekly recurring rules (ADR-0036 §3)."""

from __future__ import annotations

from typing import Protocol

from assistant.domain.recurring_calendar import (
    RecurringCalendarRule,
    RecurringCalendarRuleId,
    RecurringCalendarRuleStatus,
)


class RecurringCalendarRepository(Protocol):
    """Storage for authoritative weekly commitments."""

    async def add_rule(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        """Store a new rule.

        Raises:
            StoreError: a live rule with the same fingerprint already exists.
        """

    async def update_rule(self, rule: RecurringCalendarRule) -> RecurringCalendarRule:
        """Store the current state of an existing rule."""

    async def get_rule(
        self, rule_id: RecurringCalendarRuleId
    ) -> RecurringCalendarRule | None:
        """Return one rule, or `None`."""

    async def find_active_by_fingerprint(
        self, fingerprint: str
    ) -> RecurringCalendarRule | None:
        """The live rule with this exact meaning, if there is one."""

    async def list_rules(
        self, *, status: RecurringCalendarRuleStatus | None = RecurringCalendarRuleStatus.ACTIVE
    ) -> list[RecurringCalendarRule]:
        """Rules with one status, oldest first (or every rule when `status` is `None`)."""
