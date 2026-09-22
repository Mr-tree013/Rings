"""Deterministic greedy weekly planner (ADR-0015).

Pure by construction: no clock, no repository, no SQLite, no randomness, no CLI. Given the
same `PlanningRequest` it produces byte-identical blocks and issues, which is what makes a
proposal reviewable and reproducible.

Algorithm: order the tasks, subtract busy time from availability, then first-fit each task's
remaining effort into the earliest free slots — soft-deadline space first, then the deadline
buffer, and finally report what could not be placed.

Three capacity rules from ADR-0044 sit on top of that, and all three are *hard*:

* a local day has a minute budget (`daily_capacity`), so "at most six hours" is enforced by the
  planner rather than hoped for;
* a sitting never exceeds `max_block_minutes`;
* a sitting normally *is* `preferred_block_minutes`, so a five-hour task becomes a handful of
  ordinary afternoons rather than one impossible evening.

When the budget runs out the remainder is reported, never silently dropped: an honest "this does not
fit" is a thing a user can act on, and a plan that quietly forgot two hours is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from assistant.domain.planning import (
    PlanningIssue,
    PlanningIssueCode,
    PlanningRequest,
    PlanningTask,
    PlanResult,
    ProposedPlanBlock,
)
from assistant.domain.planning_intervals import Interval, subtract_intervals
from assistant.domain.task import TaskId, TaskPriority

_PRIORITY_RANK = {
    TaskPriority.HIGH: 0,
    TaskPriority.NORMAL: 1,
    TaskPriority.LOW: 2,
}


class GreedyPlanner:
    """First-fit scheduler: earliest free slot wins, blocks never cross a gap."""

    def plan(self, request: PlanningRequest) -> PlanResult:
        """Return proposed blocks and issues for one planning request."""
        free_slots: list[Interval] = subtract_intervals(
            request.availability, request.busy_intervals
        )
        budget = _DayBudget(request)
        preferred = request.preferred_block_minutes or request.max_block_minutes
        tasks = _ordered_tasks(request.tasks)
        blocks: list[ProposedPlanBlock] = []
        issues: list[PlanningIssue] = []
        if not free_slots and any(task.remaining_minutes > 0 for task in tasks):
            issues.append(
                PlanningIssue(
                    code=PlanningIssueCode.NO_AVAILABILITY,
                    message=(
                        "the planning window has no free availability after busy time; "
                        "nothing can be scheduled"
                    ),
                )
            )
        for task in tasks:
            if task.remaining_minutes <= 0:
                continue
            if task.deadline is not None and task.deadline <= request.window.starts_at:
                issues.append(
                    PlanningIssue(
                        code=PlanningIssueCode.DEADLINE_ALREADY_PASSED,
                        task_id=task.task_id,
                        message="the deadline has already passed; the task was not planned",
                        required_minutes=task.remaining_minutes,
                        scheduled_minutes=0,
                    )
                )
                continue
            needed = task.remaining_minutes
            required = task.remaining_minutes
            soft_limit = _soft_limit(task, request)
            scheduled, needed = _fill_slots(
                free_slots,
                blocks=blocks,
                task_id=task.task_id,
                needed=needed,
                limit=soft_limit,
                min_block=request.min_block_minutes,
                max_block=request.max_block_minutes,
                preferred=preferred,
                budget=budget,
            )
            buffer_minutes = 0
            if needed > 0 and task.deadline is not None:
                buffer_minutes, needed = _fill_slots(
                    free_slots,
                    blocks=blocks,
                    task_id=task.task_id,
                    needed=needed,
                    limit=task.deadline,
                    min_block=request.min_block_minutes,
                    max_block=request.max_block_minutes,
                    preferred=preferred,
                    budget=budget,
                )
                scheduled += buffer_minutes
            if buffer_minutes > 0:
                issues.append(
                    PlanningIssue(
                        code=PlanningIssueCode.BUFFER_VIOLATED,
                        task_id=task.task_id,
                        message=(
                            "the deadline buffer was not enough; time between the buffer and "
                            "the real deadline was used"
                        ),
                        required_minutes=required,
                        scheduled_minutes=scheduled,
                    )
                )
            if needed > 0:
                has_deadline = task.deadline is not None
                issues.append(
                    PlanningIssue(
                        code=(
                            PlanningIssueCode.INSUFFICIENT_CAPACITY
                            if has_deadline
                            else (
                                PlanningIssueCode.DAILY_CAPACITY_REACHED
                                if budget.exhausted
                                else PlanningIssueCode.WINDOW_CAPACITY_EXHAUSTED
                            )
                        ),
                        task_id=task.task_id,
                        message=(
                            "not enough time before the deadline to finish this task"
                            if has_deadline
                            else (
                                "the daily planning limit was reached before this task fitted; "
                                "the remainder is not scheduled"
                                if budget.exhausted
                                else "the planning window has insufficient capacity for this task"
                            )
                        ),
                        required_minutes=required,
                        scheduled_minutes=scheduled,
                    )
                )
        return PlanResult(blocks=tuple(blocks), issues=tuple(issues))


def _ordered_tasks(tasks: tuple[PlanningTask, ...]) -> list[PlanningTask]:
    """Deadline tasks first (by deadline), then the rest, both by priority and creation."""
    with_deadline = [task for task in tasks if task.deadline is not None]
    without_deadline = [task for task in tasks if task.deadline is None]
    with_deadline.sort(
        key=lambda task: (
            task.deadline,
            _PRIORITY_RANK[task.priority],
            task.created_at,
            str(task.task_id),
        )
    )
    without_deadline.sort(
        key=lambda task: (
            _PRIORITY_RANK[task.priority],
            task.created_at,
            str(task.task_id),
        )
    )
    return with_deadline + without_deadline


def _soft_limit(task: PlanningTask, request: PlanningRequest) -> datetime | None:
    """The preferred finish time: the deadline minus its buffer, when that is meaningful."""
    if task.deadline is None:
        return None
    if request.deadline_buffer_minutes <= 0:
        return task.deadline
    soft = task.deadline - timedelta(minutes=request.deadline_buffer_minutes)
    if soft <= request.window.starts_at:
        return request.window.starts_at
    return soft


class _DayBudget:
    """How much of each local day's planning budget is still unspent (ADR-0044 §18).

    A day with no window in the request has no cap: that is what keeps a host with no stored
    preferences planning exactly as it did before the preference existed.
    """

    def __init__(self, request: PlanningRequest) -> None:
        self._windows = tuple(request.daily_capacity)
        self._spent = [0] * len(self._windows)
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        """Whether a daily limit was the reason something could not be placed."""
        return self._exhausted

    def remaining(self, start: datetime, end: datetime) -> int:
        """Minutes still available inside the day that covers this slot, or a huge number."""
        index = self._index_of(start)
        if index is None:
            return _minutes(end - start)
        window = self._windows[index]
        return max(0, window.capacity_minutes - self._spent[index])

    def spend(self, start: datetime, minutes: int) -> None:
        """Record planned time against the day that contains `start`."""
        index = self._index_of(start)
        if index is not None:
            self._spent[index] += minutes

    def note_exhausted(self) -> None:
        """Remember that a limit, not the clock, is what stopped the plan."""
        self._exhausted = True

    def _index_of(self, moment: datetime) -> int | None:
        for index, window in enumerate(self._windows):
            if window.starts_at <= moment < window.ends_at:
                return index
        return None


def _minutes(span: object) -> int:
    """Whole minutes in a timedelta."""
    if not isinstance(span, timedelta):  # pragma: no cover - the caller always passes a difference
        return 0
    return int(span.total_seconds() // 60)


def _fill_slots(
    free_slots: list[Interval],
    *,
    blocks: list[ProposedPlanBlock],
    task_id: TaskId,
    needed: int,
    limit: datetime | None,
    min_block: int,
    max_block: int,
    preferred: int,
    budget: _DayBudget,
) -> tuple[int, int]:
    """First-fit `needed` minutes into `free_slots`, capped by `limit`.

    Returns `(scheduled_minutes, needed_minutes_left)` and consumes the used prefixes, so the
    next task sees an accurate view of remaining availability.
    """
    scheduled = 0
    index = 0
    while index < len(free_slots) and needed > 0:
        start, end = free_slots[index]
        slot_end = end if limit is None else min(end, limit)
        if slot_end <= start:
            index += 1
            continue
        cursor = start
        while needed > 0:
            available = int((slot_end - cursor).total_seconds() // 60)
            if available <= 0:
                # The slot itself is used up. That is the calendar, not the user's daily limit.
                break
            budget_left = budget.remaining(cursor, slot_end)
            if budget_left <= 0:
                # There is room in the calendar and none in the day: this is the daily limit.
                budget.note_exhausted()
                break
            room = min(available, budget_left)
            # The preferred length is the target; the maximum is the ceiling; the remainder of the
            # task is the floor only when it is itself a legitimate sitting.
            block_minutes = min(needed, max_block, room, preferred)
            if block_minutes < min_block and block_minutes < needed:
                # A fragment that would not finish the task is not worth a calendar entry — but one
                # that *does* finish it is always worth placing, however short.
                #
                # Only a daily limit is reported as a daily limit: a slot that is merely too short
                # is availability, and blaming the user's own setting for the calendar would send
                # them to change the wrong thing.
                if budget_left < available:
                    budget.note_exhausted()
                break
            block_end = cursor + timedelta(minutes=block_minutes)
            blocks.append(
                ProposedPlanBlock(
                    task_id=task_id,
                    starts_at=cursor,
                    ends_at=block_end,
                    ordinal=len(blocks),
                )
            )
            budget.spend(cursor, block_minutes)
            scheduled += block_minutes
            needed -= block_minutes
            cursor = block_end
        if cursor > start:
            free_slots[index] = (cursor, end)
        if free_slots[index][1] <= free_slots[index][0]:
            free_slots.pop(index)
        else:
            index += 1
    return scheduled, needed


__all__ = ["GreedyPlanner"]
