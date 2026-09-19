"""Calendar application service: occupied time and planned time (ADR-0014).

Busy time is reported as `BusyInterval` values that keep their source kind, so a plan block is
never disguised as a calendar event. Interval arithmetic is half-open (`[start, end)`),
meaning back-to-back intervals do not overlap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from assistant.domain.calendar_event import (
    CalendarEvent,
    CalendarEventId,
    new_calendar_event_id,
)
from assistant.domain.errors import (
    AmbiguousId,
    CalendarEventNotFound,
    InvalidTimeInterval,
    PlanBlockNotFound,
    TaskNotFound,
    TaskNotOpen,
)
from assistant.domain.plan_block import PlanBlock, PlanBlockId, new_plan_block_id
from assistant.domain.task import TaskId
from assistant.domain.time_interval import BusyInterval, BusyIntervalKind
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository

_EARLIEST = datetime(1970, 1, 1, tzinfo=UTC)
_LATEST = datetime(9999, 1, 1, tzinfo=UTC) - timedelta(days=1)
"""Bounds for "all time" lookups, only used by id-prefix resolution."""


@dataclass(frozen=True, slots=True)
class CreateCalendarEvent:
    """Structured input for occupying a stretch of time."""

    title: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CreatePlanBlock:
    """Structured input for planning work on a task."""

    task_id: TaskId
    starts_at: datetime
    ends_at: datetime


class CalendarService:
    """Creates and cancels calendar events and plan blocks, and reports busy time."""

    def __init__(
        self,
        commitments: CommitmentRepository,
        clock: Clock,
        *,
        new_event_id_factory: Callable[[], CalendarEventId] = new_calendar_event_id,
        new_plan_block_id_factory: Callable[[], PlanBlockId] = new_plan_block_id,
    ) -> None:
        self._commitments = commitments
        self._clock = clock
        self._new_event_id = new_event_id_factory
        self._new_plan_block_id = new_plan_block_id_factory

    # ------------------------------------------------------------------- events

    async def create_event(self, command: CreateCalendarEvent) -> CalendarEvent:
        """Store a calendar event."""
        now = self._clock.now()
        event = CalendarEvent(
            id=self._new_event_id(),
            title=command.title.strip(),
            description=command.description,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            created_at=now,
            updated_at=now,
        )
        return await self._commitments.add_calendar_event(event)

    async def cancel_event(self, event_id: CalendarEventId) -> CalendarEvent:
        """Cancel a calendar event."""
        return await self._commitments.cancel_calendar_event(event_id, at=self._clock.now())

    async def list_events(
        self,
        *,
        query_start: datetime,
        query_end: datetime,
        include_cancelled: bool = False,
    ) -> list[CalendarEvent]:
        """List events overlapping the half-open query range."""
        _require_range(query_start, query_end)
        return await self._commitments.list_calendar_events(
            query_start=query_start, query_end=query_end, include_cancelled=include_cancelled
        )

    # --------------------------------------------------------------- plan blocks

    async def create_plan_block(self, command: CreatePlanBlock) -> PlanBlock:
        """Plan time for an OPEN task.

        Raises:
            TaskNotFound: no such task.
            TaskNotOpen: the task is completed or cancelled; terminal tasks are history.
        """
        task = await self._commitments.get_task(command.task_id)
        if task is None:
            raise TaskNotFound(command.task_id)
        if not task.is_open:
            raise TaskNotOpen(
                f"task {command.task_id} is {task.status}; terminal tasks cannot be planned"
            )
        now = self._clock.now()
        block = PlanBlock(
            id=self._new_plan_block_id(),
            task_id=command.task_id,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            created_at=now,
            updated_at=now,
        )
        return await self._commitments.add_plan_block(block)

    async def cancel_plan_block(self, plan_block_id: PlanBlockId) -> PlanBlock:
        """Cancel a plan block by hand."""
        return await self._commitments.cancel_plan_block(plan_block_id, at=self._clock.now())

    async def list_plan_blocks_for_task(
        self, task_id: TaskId, *, include_cancelled: bool = False
    ) -> list[PlanBlock]:
        """List a task's plan blocks."""
        return await self._commitments.list_plan_blocks_for_task(
            task_id, include_cancelled=include_cancelled
        )

    # -------------------------------------------------------------- busy time

    async def get_busy_intervals(
        self, *, query_start: datetime, query_end: datetime
    ) -> list[BusyInterval]:
        """Return active calendar events *and* active plan blocks as busy intervals.

        Deadlines never appear here: a deadline is a point in time, not occupied time.
        """
        _require_range(query_start, query_end)
        events = await self._commitments.list_calendar_events(
            query_start=query_start, query_end=query_end
        )
        blocks = await self._commitments.list_plan_blocks_in_range(
            query_start=query_start, query_end=query_end
        )
        intervals = [
            BusyInterval(
                source_kind=BusyIntervalKind.CALENDAR_EVENT,
                source_id=event.id,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                title=event.title,
            )
            for event in events
        ] + [
            BusyInterval(
                source_kind=BusyIntervalKind.PLAN_BLOCK,
                source_id=block.id,
                starts_at=block.starts_at,
                ends_at=block.ends_at,
                origin=block.origin.value,
                proposal_id=block.proposal_id,
            )
            for block in blocks
        ]
        intervals.sort(key=lambda item: (item.starts_at, item.ends_at, item.source_kind.value))
        return intervals

    # ------------------------------------------------------------ id resolution

    async def resolve_event_id(self, reference: str) -> CalendarEventId:
        """Resolve a full UUID or an unambiguous prefix to a calendar event id."""
        text = reference.strip().lower()
        if not text:
            raise CalendarEventNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        if candidate is not None:
            if await self._commitments.get_calendar_event(candidate) is None:
                raise CalendarEventNotFound(candidate)
            return candidate
        events = await self._commitments.list_calendar_events(
            query_start=_EARLIEST, query_end=_LATEST, include_cancelled=True
        )
        matching = [event.id for event in events if str(event.id).startswith(text)]
        if not matching:
            raise CalendarEventNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    async def resolve_plan_block_id(self, reference: str) -> PlanBlockId:
        """Resolve a full UUID or an unambiguous prefix to a plan block id."""
        text = reference.strip().lower()
        if not text:
            raise PlanBlockNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        if candidate is not None:
            if await self._commitments.get_plan_block(candidate) is None:
                raise PlanBlockNotFound(candidate)
            return candidate
        blocks = await self._commitments.list_plan_blocks_in_range(
            query_start=_EARLIEST, query_end=_LATEST, include_cancelled=True
        )
        matching = [block.id for block in blocks if str(block.id).startswith(text)]
        if not matching:
            raise PlanBlockNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]


def _require_range(query_start: datetime, query_end: datetime) -> None:
    if query_end <= query_start:
        raise InvalidTimeInterval("query_end must be after query_start")


__all__ = ["CalendarService", "CreateCalendarEvent", "CreatePlanBlock"]
