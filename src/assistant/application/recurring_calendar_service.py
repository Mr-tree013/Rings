"""Weekly recurring commitments as authoritative calendar state (ADR-0036 §4-§5).

Creating the same weekly commitment twice is idempotent: the rule's canonical fingerprint is what
identifies it, so saying "每周一十点到十二点有课" three times leaves one rule, not three. Editing
or retiring a rule never rewrites the past: occurrences are derived, so a historic plan or work
session keeps the meaning it had.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistant.domain.errors import (
    InvalidTimeInterval,
    RecurringRuleNotFound,
)
from assistant.domain.recurring_calendar import (
    RecurringCalendarOccurrence,
    RecurringCalendarRule,
    RecurringCalendarRuleId,
    RecurringCalendarRuleStatus,
    expand_rules,
    parse_clock,
)
from assistant.ports.clock import Clock
from assistant.ports.recurring_calendar_repository import RecurringCalendarRepository


class RecurringCalendarService:
    """Create, inspect, edit, retire and expand weekly commitments."""

    def __init__(
        self,
        repository: RecurringCalendarRepository,
        clock: Clock,
        *,
        default_timezone: str | None,
    ) -> None:
        self._rules = repository
        self._clock = clock
        self._default_timezone = default_timezone

    @property
    def default_timezone(self) -> str | None:
        """The planning timezone a bare weekday/time is interpreted in."""
        return self._default_timezone

    async def create_weekly(
        self,
        *,
        title: str,
        weekday: int,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        timezone: str | None = None,
        starts_on: date | None = None,
        ends_on: date | None = None,
    ) -> RecurringCalendarRule:
        """Create one weekly rule, or return the existing one with the same meaning.

        Raises:
            InvalidTimeInterval: the fields do not describe a weekly same-day commitment.
        """
        rule, _ = await self.ensure_weekly(
            title=title,
            weekday=weekday,
            start=start,
            end=end,
            timezone=timezone,
            starts_on=starts_on,
            ends_on=ends_on,
        )
        return rule

    async def ensure_weekly(
        self,
        *,
        title: str,
        weekday: int,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        timezone: str | None = None,
        starts_on: date | None = None,
        ends_on: date | None = None,
    ) -> tuple[RecurringCalendarRule, bool]:
        """Create one weekly rule, or return the existing one and say which happened.

        The flag is what lets a caller tell the user "这个固定安排已经存在" instead of reporting a
        creation that did not happen.

        Raises:
            InvalidTimeInterval: the fields do not describe a weekly same-day commitment.
        """
        zone = timezone or self._default_timezone
        if zone is None:
            raise InvalidTimeInterval(
                "a weekly commitment needs a planning timezone; set [planning].timezone"
            )
        now = self._clock.now()
        start_time = _clock_value(start)
        end_time = _clock_value(end)
        first_day = starts_on or now.astimezone(_zone(zone)).date()
        candidate = RecurringCalendarRule(
            title=title,
            weekday=weekday,
            start_time=start_time,
            end_time=end_time,
            timezone=zone,
            starts_on=first_day,
            ends_on=ends_on,
            created_at=now,
            updated_at=now,
        )
        existing = await self._rules.find_active_by_fingerprint(candidate.fingerprint)
        if existing is not None:
            # Idempotent: the same weekly commitment is the same rule.
            return existing, False
        return await self._rules.add_rule(candidate), True

    async def list_active(self) -> list[RecurringCalendarRule]:
        """Every live rule, oldest first."""
        return await self._rules.list_rules(status=RecurringCalendarRuleStatus.ACTIVE)

    async def list_rules(
        self, *, include_retired: bool = False
    ) -> list[RecurringCalendarRule]:
        """Every live rule, oldest first — retired ones only when they were asked for."""
        return await self._rules.list_rules(
            status=None if include_retired else RecurringCalendarRuleStatus.ACTIVE
        )

    async def resolve_rule_id(self, reference: str) -> RecurringCalendarRuleId:
        """Resolve a full id or a unique prefix.

        Raises:
            RecurringRuleNotFound: nothing matches.
            InvalidTimeInterval: the prefix is ambiguous.
        """
        cleaned = reference.strip().lower()
        rules = await self._rules.list_rules(status=None)
        matches = [rule for rule in rules if str(rule.id).startswith(cleaned)]
        if not matches:
            raise RecurringRuleNotFound(reference)
        if len(matches) > 1:
            raise InvalidTimeInterval(f"{reference} matches several weekly rules")
        return matches[0].id

    async def require_rule(self, reference: str) -> RecurringCalendarRule:
        """Return one rule by id or unique prefix."""
        rule = await self._rules.get_rule(await self.resolve_rule_id(reference))
        if rule is None:  # pragma: no cover - resolution just found it
            raise RecurringRuleNotFound(reference)
        return rule

    async def edit(
        self,
        reference: str,
        *,
        title: str | None = None,
        weekday: int | None = None,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
    ) -> RecurringCalendarRule:
        """Change one rule's meaning, keeping its identity and its history.

        Raises:
            RecurringRuleNotFound: no such rule.
            InvalidTimeInterval: the change would break a rule invariant.
        """
        current = await self.require_rule(reference)
        now = self._clock.now()
        updated = RecurringCalendarRule(
            id=current.id,
            title=current.title if title is None else title,
            weekday=current.weekday if weekday is None else weekday,
            start_time=current.start_time if start is None else _clock_value(start),
            end_time=current.end_time if end is None else _clock_value(end),
            timezone=current.timezone,
            starts_on=current.starts_on,
            ends_on=current.ends_on,
            status=current.status,
            retired_at=current.retired_at,
            created_at=current.created_at,
            updated_at=now,
        )
        conflict = await self._rules.find_active_by_fingerprint(updated.fingerprint)
        if conflict is not None and conflict.id != updated.id:
            raise InvalidTimeInterval(f"已经有一条同样的固定安排了 (id {str(conflict.id)[:8]})")
        return await self._rules.update_rule(updated)

    async def retire(self, reference: str) -> RecurringCalendarRule:
        """Retire a rule: it stops producing occurrences, and history is untouched."""
        current = await self.require_rule(reference)
        if current.status is RecurringCalendarRuleStatus.RETIRED:
            return current
        return await self._rules.update_rule(current.retired(self._clock.now()))

    async def expand_range(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[RecurringCalendarOccurrence, ...]:
        """Derive every occurrence of every live rule inside one range."""
        rules = tuple(await self.list_active())
        return expand_rules(rules, window_start=window_start, window_end=window_end)

    async def occurrences_on(self, day: date, *, timezone: str | None = None) -> tuple[
        RecurringCalendarOccurrence, ...
    ]:
        """Every occurrence on one local calendar day, in the rule's own timezone."""
        zone = _zone(timezone or self._default_timezone or "UTC")
        start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        return await self.expand_range(window_start=start, window_end=start + timedelta(days=1))


def _clock_value(value: str | datetime | None) -> time:
    """Accept `HH:MM` or a datetime's time, so both CLI and conversation callers work."""
    if value is None:
        raise InvalidTimeInterval("a weekly commitment needs a start and an end time")
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    return parse_clock(value)


def _zone(name: str) -> ZoneInfo:
    """The IANA zone a bare weekday/time is interpreted in."""
    return ZoneInfo(name)


__all__ = ["RecurringCalendarService"]
