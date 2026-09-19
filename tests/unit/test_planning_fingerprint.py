"""Canonical planning fingerprint: stable, order-independent, scheduling-relevant only."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from assistant.application.planning_fingerprint import planning_fingerprint
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.config import PlanningConfig, Weekday, WeeklyAvailabilityRule
from assistant.domain.deadline import Deadline
from assistant.domain.plan_block import PlanBlock, PlanBlockOrigin
from assistant.domain.planning import PlanningSnapshot, PlanningWindow
from assistant.domain.task import Task, TaskPriority

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
WINDOW = PlanningWindow(
    starts_at=NOW, ends_at=NOW + timedelta(days=7), timezone="Asia/Shanghai"
)
CONFIG = PlanningConfig(
    timezone="Asia/Shanghai",
    availability=(
        WeeklyAvailabilityRule(
            days=(Weekday.MON, Weekday.TUE),
            start_minute=9 * 60,
            end_minute=17 * 60,
        ),
    ),
)


def _task(
    task_id: UUID,
    *,
    title: str = "Write SE lab report",
    description: str | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    estimate: int | None = 300,
    updated_at: datetime = NOW,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        created_at=NOW,
        updated_at=updated_at,
        priority=priority,
        estimated_minutes=estimate,
    )


def _deadline(task_id: UUID, *, hours: int = 120) -> Deadline:
    return Deadline(
        task_id=task_id,
        due_at=NOW + timedelta(hours=hours),
        created_at=NOW,
        updated_at=NOW,
    )


def _event(title: str, *, start_hours: int, end_hours: int) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        starts_at=NOW + timedelta(hours=start_hours),
        ends_at=NOW + timedelta(hours=end_hours),
        created_at=NOW,
        updated_at=NOW,
    )


def _block(
    task_id: UUID,
    *,
    start_hours: int,
    end_hours: int,
    origin: PlanBlockOrigin = PlanBlockOrigin.MANUAL,
    proposal_id: UUID | None = None,
) -> PlanBlock:
    return PlanBlock(
        task_id=task_id,
        starts_at=NOW + timedelta(hours=start_hours),
        ends_at=NOW + timedelta(hours=end_hours),
        created_at=NOW,
        updated_at=NOW,
        origin=origin,
        proposal_id=proposal_id,
    )


def _snapshot(
    *,
    revision: int = 3,
    tasks: tuple[Task, ...] = (),
    deadlines: dict[UUID, Deadline] | None = None,
    actual_work: dict[UUID, int] | None = None,
    events: tuple[CalendarEvent, ...] = (),
    manual: tuple[PlanBlock, ...] = (),
    planner: tuple[PlanBlock, ...] = (),
) -> PlanningSnapshot:
    return PlanningSnapshot(
        window=WINDOW,
        revision=revision,
        open_tasks=tasks,
        deadlines=deadlines if deadlines is not None else {},
        actual_work_seconds=actual_work if actual_work is not None else {},
        active_calendar_events=events,
        active_manual_plan_blocks=manual,
        active_planner_plan_blocks=planner,
    )


def _fingerprint(snapshot: PlanningSnapshot) -> str:
    return planning_fingerprint(window=WINDOW, config=CONFIG, snapshot=snapshot)


def test_fingerprint_is_64_lowercase_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", _fingerprint(_snapshot())) is not None


def test_fingerprint_ignores_collection_order_but_not_content() -> None:
    first_id, second_id = uuid4(), uuid4()
    first_task = _task(first_id, title="a")
    second_task = _task(second_id, title="b")
    first_event = _event("x", start_hours=1, end_hours=2)
    second_event = _event("y", start_hours=3, end_hours=4)
    manual = _block(first_id, start_hours=5, end_hours=6)

    ordered = _fingerprint(
        _snapshot(
            tasks=(first_task, second_task),
            deadlines={first_id: _deadline(first_id), second_id: _deadline(second_id)},
            actual_work={second_id: 1200, first_id: 600},
            events=(first_event, second_event),
            manual=(manual,),
        )
    )
    reordered = _fingerprint(
        _snapshot(
            tasks=(second_task, first_task),
            deadlines={second_id: _deadline(second_id), first_id: _deadline(first_id)},
            actual_work={first_id: 600, second_id: 1200},
            events=(second_event, first_event),
            manual=(manual,),
        )
    )
    different = _fingerprint(
        _snapshot(
            tasks=(second_task, first_task),
            deadlines={second_id: _deadline(second_id), first_id: _deadline(first_id, hours=1)},
            actual_work={first_id: 600, second_id: 1200},
            events=(second_event, first_event),
            manual=(manual,),
        )
    )

    assert ordered == reordered
    assert ordered != different


def test_every_scheduling_relevant_field_moves_the_fingerprint() -> None:
    task_id = uuid4()
    task = _task(task_id)
    baseline = _fingerprint(_snapshot(tasks=(task,), actual_work={task_id: 600}))

    fingerprints = {
        baseline,
        _fingerprint(_snapshot(revision=4, tasks=(task,), actual_work={task_id: 600})),
        _fingerprint(
            _snapshot(
                tasks=(_task(task_id, priority=TaskPriority.HIGH),),
                actual_work={task_id: 600},
            )
        ),
        _fingerprint(_snapshot(tasks=(_task(task_id, estimate=120),), actual_work={task_id: 600})),
        _fingerprint(_snapshot(tasks=(task,), actual_work={task_id: 601})),
        _fingerprint(_snapshot(tasks=(task,), deadlines={task_id: _deadline(task_id)})),
        _fingerprint(
            _snapshot(
                tasks=(task,),
                actual_work={task_id: 600},
                events=(_event("Lecture", start_hours=1, end_hours=2),),
            )
        ),
        _fingerprint(
            _snapshot(
                tasks=(task,),
                actual_work={task_id: 600},
                manual=(_block(task_id, start_hours=1, end_hours=2),),
            )
        ),
        _fingerprint(
            _snapshot(
                tasks=(_task(task_id, updated_at=NOW + timedelta(minutes=1)),),
                actual_work={task_id: 600},
            )
        ),
    }

    assert len(fingerprints) == 9


def test_planner_blocks_are_part_of_the_fingerprint_even_though_they_are_not_busy() -> None:
    task_id = uuid4()
    without = _fingerprint(_snapshot(tasks=(_task(task_id),)))
    with_block = _fingerprint(
        _snapshot(
            tasks=(_task(task_id),),
            planner=(
                _block(
                    task_id,
                    start_hours=1,
                    end_hours=2,
                    origin=PlanBlockOrigin.PLANNER,
                    proposal_id=uuid4(),
                ),
            ),
        )
    )

    assert without != with_block


def test_wording_alone_does_not_change_a_fingerprint() -> None:
    task_id = uuid4()
    described = _task(task_id, title="Write SE lab report", description="draft one")
    reworded = _task(task_id, title="Completely different wording", description=None)

    assert _fingerprint(_snapshot(tasks=(described,))) == _fingerprint(
        _snapshot(tasks=(reworded,))
    )
