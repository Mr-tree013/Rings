"""Weekly recurring civil-time commitments (ADR-0036).

```text
RecurringCalendarRule        "every Monday 10:00-12:00, Asia/Shanghai, from 2026-09-21"
        │  expand(range)
        ▼
RecurringCalendarOccurrence  concrete instants, derived on demand
```

The rule is the durable fact; occurrences are a *view*. Nothing here materializes a calendar
event, and nothing here reads the host clock or the host timezone: the rule carries its own IANA
zone, and expansion is pure arithmetic over `zoneinfo`.

v1.1 is deliberately one weekday per rule, weekly, same-day, non-overnight. "Monday and Wednesday"
is two rules; "every other week" is not expressible, and pretending otherwise would silently create
classes that do not exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from assistant.domain.errors import InvalidTimeInterval

RecurringCalendarRuleId = UUID

MAX_RULE_TITLE_CHARS = 200
MAX_EXPANDED_OCCURRENCES = 500
"""A bounded expansion: a range is a question, not a request to plan a decade."""


def new_recurring_rule_id() -> RecurringCalendarRuleId:
    """Generate a fresh rule identity."""
    return uuid4()


class RecurringCalendarRuleStatus(StrEnum):
    """Whether the weekly commitment is still in force."""

    ACTIVE = "active"
    RETIRED = "retired"


def parse_clock(value: str) -> time:
    """Parse `HH:MM` into a local time.

    Raises:
        InvalidTimeInterval: the text is not a clock time.
    """
    try:
        hour, minute = value.split(":")
        return time(hour=int(hour), minute=int(minute))
    except (ValueError, AttributeError) as exc:
        raise InvalidTimeInterval(f"{value!r} is not a HH:MM local time") from exc


def format_clock(value: time) -> str:
    """Render a local time as `HH:MM`."""
    return f"{value.hour:02d}:{value.minute:02d}"


@dataclass(frozen=True, slots=True)
class RecurringCalendarOccurrence:
    """One derived occurrence, as aware instants."""

    rule_id: RecurringCalendarRuleId
    title: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise InvalidTimeInterval("occurrences must be timezone-aware")
        if self.ends_at <= self.starts_at:
            raise InvalidTimeInterval("an occurrence must end after it starts")


@dataclass(frozen=True, slots=True)
class RecurringCalendarRule:
    """One weekly civil-time commitment."""

    title: str
    weekday: int
    start_time: time
    end_time: time
    timezone: str
    starts_on: date
    created_at: datetime
    updated_at: datetime
    id: RecurringCalendarRuleId = field(default_factory=new_recurring_rule_id)
    ends_on: date | None = None
    status: RecurringCalendarRuleStatus = RecurringCalendarRuleStatus.ACTIVE
    retired_at: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.weekday <= 7:
            raise InvalidTimeInterval("weekday is ISO 1 (Monday) to 7 (Sunday)")
        if self.end_time <= self.start_time:
            raise InvalidTimeInterval(
                "a weekly rule must end after it starts on the same day (no overnight rules)"
            )
        if not self.timezone.strip():
            raise InvalidTimeInterval("a weekly rule needs an IANA timezone")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise InvalidTimeInterval(f"unknown timezone {self.timezone!r}") from exc
        if not self.title.strip() or len(self.title) > MAX_RULE_TITLE_CHARS:
            raise InvalidTimeInterval(
                f"a rule title is 1 to {MAX_RULE_TITLE_CHARS} characters"
            )
        if self.ends_on is not None and self.ends_on < self.starts_on:
            raise InvalidTimeInterval("ends_on cannot precede starts_on")
        if (self.status is RecurringCalendarRuleStatus.RETIRED) != (self.retired_at is not None):
            raise InvalidTimeInterval("retired_at is set exactly when the rule is retired")

    @property
    def fingerprint(self) -> str:
        """A canonical SHA-256 over the meaning of the rule (never its identity or status)."""
        document = {
            "title": self.title.strip(),
            "weekday": self.weekday,
            "start": format_clock(self.start_time),
            "end": format_clock(self.end_time),
            "timezone": self.timezone,
            "starts_on": self.starts_on.isoformat(),
            "ends_on": None if self.ends_on is None else self.ends_on.isoformat(),
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def retired(self, at: datetime) -> RecurringCalendarRule:
        """Return the same rule, retired."""
        return RecurringCalendarRule(
            id=self.id,
            title=self.title,
            weekday=self.weekday,
            start_time=self.start_time,
            end_time=self.end_time,
            timezone=self.timezone,
            starts_on=self.starts_on,
            ends_on=self.ends_on,
            status=RecurringCalendarRuleStatus.RETIRED,
            retired_at=at,
            created_at=self.created_at,
            updated_at=at,
        )

    def expand(
        self, *, window_start: datetime, window_end: datetime
    ) -> tuple[RecurringCalendarOccurrence, ...]:
        """Derive every occurrence overlapping `[window_start, window_end)`.

        Pure: the same rule and the same range always produce the same instants, whatever the host
        clock or the host timezone happens to be.
        """
        if window_end <= window_start:
            raise InvalidTimeInterval("an expansion window must end after it starts")
        zone = ZoneInfo(self.timezone)
        first_local = window_start.astimezone(zone).date()
        last_local = window_end.astimezone(zone).date()
        occurrences: list[RecurringCalendarOccurrence] = []
        day = max(self.starts_on, first_local - timedelta(days=7))
        while day <= last_local:
            if day.isoweekday() == self.weekday and day >= self.starts_on:
                if self.ends_on is not None and day > self.ends_on:
                    break
                starts_at = datetime.combine(day, self.start_time, tzinfo=zone)
                ends_at = datetime.combine(day, self.end_time, tzinfo=zone)
                if starts_at < window_end and ends_at > window_start:
                    occurrences.append(
                        RecurringCalendarOccurrence(
                            rule_id=self.id,
                            title=self.title,
                            starts_at=starts_at,
                            ends_at=ends_at,
                        )
                    )
            day += timedelta(days=1)
            if len(occurrences) > MAX_EXPANDED_OCCURRENCES:
                raise InvalidTimeInterval(
                    "that range expands to too many occurrences; ask for a shorter window"
                )
        return tuple(occurrences)


def expand_rules(
    rules: tuple[RecurringCalendarRule, ...],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[RecurringCalendarOccurrence, ...]:
    """Every ACTIVE rule's occurrences in one range, ordered by start."""
    occurrences = [
        occurrence
        for rule in rules
        if rule.status is RecurringCalendarRuleStatus.ACTIVE
        for occurrence in rule.expand(window_start=window_start, window_end=window_end)
    ]
    occurrences.sort(key=lambda occurrence: (occurrence.starts_at, str(occurrence.rule_id)))
    return tuple(occurrences)


__all__ = [
    "MAX_EXPANDED_OCCURRENCES",
    "MAX_RULE_TITLE_CHARS",
    "RecurringCalendarOccurrence",
    "RecurringCalendarRule",
    "RecurringCalendarRuleId",
    "RecurringCalendarRuleStatus",
    "expand_rules",
    "format_clock",
    "new_recurring_rule_id",
    "parse_clock",
]
