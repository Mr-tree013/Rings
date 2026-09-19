"""Deterministic greedy weekly planner (ADR-0015).

Pure by construction: no clock, no repository, no SQLite, no randomness, no CLI. Given the
same `PlanningRequest` it produces byte-identical blocks and issues, which is what makes a
proposal reviewable and reproducible.

Algorithm: order the tasks, subtract busy time from availability, then first-fit each task's
remaining effort into the earliest free slots — soft-deadline space first, then the deadline
buffer, and finally report what could not be placed.
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
                            else PlanningIssueCode.WINDOW_CAPACITY_EXHAUSTED
                        ),
                        task_id=task.task_id,
                        message=(
                            "not enough time before the deadline to finish this task"
                            if has_deadline
                            else "the planning window has insufficient capacity for this task"
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


def _fill_slots(
    free_slots: list[Interval],
    *,
    blocks: list[ProposedPlanBlock],
    task_id: TaskId,
    needed: int,
    limit: datetime | None,
    min_block: int,
    max_block: int,
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
                break
            block_minutes = min(needed, max_block, available)
            if block_minutes < min_block and block_minutes < needed:
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

