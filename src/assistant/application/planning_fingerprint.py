"""Canonical fingerprint of everything that can change a plan (ADR-0015).

The fingerprint is audit evidence and a reproducibility check; the *transactional* fence
against races is the commitment revision. Task titles and descriptions are deliberately
excluded: they cannot change any scheduled instant.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from assistant.domain.config import PlanningConfig
from assistant.domain.plan_block import PlanBlock
from assistant.domain.planning import PlanningSnapshot, PlanningWindow
from assistant.domain.task import Task


def planning_fingerprint(
    *, window: PlanningWindow, config: PlanningConfig, snapshot: PlanningSnapshot
) -> str:
    """Return the 64-character lowercase hex fingerprint of the planning input."""
    payload = {
        "window": {
            "starts_at": _instant(window.starts_at),
            "ends_at": _instant(window.ends_at),
            "timezone": window.timezone,
        },
        "config": {
            "timezone": config.timezone,
            "min_block_minutes": config.min_block_minutes,
            "max_block_minutes": config.max_block_minutes,
            "deadline_buffer_minutes": config.deadline_buffer_minutes,
            "availability": [
                {
                    "days": [day.value for day in rule.days],
                    "start_minute": rule.start_minute,
                    "end_minute": rule.end_minute,
                }
                for rule in config.availability
            ],
        },
        "revision": snapshot.revision,
        "tasks": [_task_entry(task) for task in _sorted_tasks(snapshot.open_tasks)],
        "deadlines": [
            {"task_id": _id(task_id), "due_at": _instant(deadline.due_at)}
            for task_id, deadline in sorted(
                snapshot.deadlines.items(), key=lambda item: str(item[0])
            )
        ],
        "actual_work_seconds": [
            {"task_id": _id(task_id), "seconds": seconds}
            for task_id, seconds in sorted(
                snapshot.actual_work_seconds.items(), key=lambda item: str(item[0])
            )
        ],
        "calendar_events": [
            {
                "id": _id(event.id),
                "starts_at": _instant(event.starts_at),
                "ends_at": _instant(event.ends_at),
                "updated_at": _instant(event.updated_at),
            }
            for event in sorted(
                snapshot.active_calendar_events, key=lambda item: (item.starts_at, str(item.id))
            )
        ],
        "manual_plan_blocks": [
            _block_entry(block)
            for block in sorted(
                snapshot.active_manual_plan_blocks,
                key=lambda item: (item.starts_at, str(item.id)),
            )
        ],
        "planner_plan_blocks": [
            _block_entry(block)
            for block in sorted(
                snapshot.active_planner_plan_blocks,
                key=lambda item: (item.starts_at, str(item.id)),
            )
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sorted_tasks(tasks: tuple[Task, ...]) -> list[Task]:
    return sorted(tasks, key=lambda task: (task.created_at, str(task.id)))


def _task_entry(task: Task) -> dict[str, object]:
    return {
        "id": _id(task.id),
        "priority": task.priority.value,
        "estimated_minutes": task.estimated_minutes,
        "updated_at": _instant(task.updated_at),
    }


def _block_entry(block: PlanBlock) -> dict[str, object]:
    return {
        "id": _id(block.id),
        "task_id": _id(block.task_id),
        "starts_at": _instant(block.starts_at),
        "ends_at": _instant(block.ends_at),
        "updated_at": _instant(block.updated_at),
        "proposal_id": None if block.proposal_id is None else _id(block.proposal_id),
    }


def _instant(value: datetime) -> str:
    return value.isoformat()


def _id(value: UUID) -> str:
    return str(value)


__all__ = ["planning_fingerprint"]
