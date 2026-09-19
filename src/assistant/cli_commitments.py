"""Structured commitment CLI: tasks, calendar, plan and work (ADR-0014).

Every command here is *structured input*: titles, ISO timestamps and explicit options. There
is no natural-language parsing, no clock guessing and no LLM. Each command builds the
application services via `assistant.bootstrap` and calls a single service method.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.calendar_service import (
    CalendarService,
    CreateCalendarEvent,
    CreatePlanBlock,
)
from assistant.application.task_service import CreateTask, TaskService
from assistant.application.work_service import WorkService
from assistant.cli_support import console, fail, format_local, parse_aware_datetime, short_id
from assistant.domain.calendar_event import CalendarEvent
from assistant.domain.deadline import Deadline
from assistant.domain.errors import DomainError
from assistant.domain.plan_block import PlanBlock
from assistant.domain.task import Task, TaskPriority
from assistant.domain.work_session import WorkSession
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.store.errors import StoreError


@dataclass(frozen=True, slots=True)
class Services:
    """The commitment services one CLI command may need."""

    clock: Clock
    tasks: TaskService
    calendar: CalendarService
    work: WorkService
    commitments: CommitmentRepository


def services_for(database: Any, clock: Clock) -> Services:
    """Build the commitment services for one command run."""
    return Services(
        clock=clock,
        tasks=bootstrap.task_service(database, clock),
        calendar=bootstrap.calendar_service(database, clock),
        work=bootstrap.work_service(database, clock),
        commitments=bootstrap.commitment_repository(database),
    )


def run[T](action: Callable[[Services], Coroutine[Any, Any, T]]) -> T:
    """Build services, run one async action, and turn project errors into CLI failures."""

    async def runner() -> T:
        clock = bootstrap.system_clock()
        database = bootstrap.runtime_database(clock)
        return await action(services_for(database, clock))

    try:
        return asyncio.run(runner())
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"commitment store failure: {exc}")
    except ValueError as exc:
        fail(str(exc))


def priority_of(value: str) -> TaskPriority:
    """Parse a priority option, with the allowed values in the error message."""
    try:
        return TaskPriority(value.strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in TaskPriority)
        fail(f"priority must be one of: {allowed}")


def timestamp(value: str, *, field_name: str) -> datetime:
    """Parse a user timestamp, reporting a clean CLI failure instead of a traceback."""
    try:
        return parse_aware_datetime(value, field_name=field_name)
    except ValueError as exc:
        fail(str(exc))


task_app = typer.Typer(help="Single-task commands.", no_args_is_help=True)
calendar_app = typer.Typer(help="Calendar events and busy time.", no_args_is_help=False)
plan_app = typer.Typer(help="Planned time blocks for a task.", no_args_is_help=True)
work_app = typer.Typer(help="Actual work sessions.", no_args_is_help=True)


def list_tasks_command(
    include_terminal: Annotated[
        bool, typer.Option("--all", help="Include completed and cancelled tasks.")
    ] = False,
) -> None:
    """List tasks: OPEN by default, ordered by deadline then creation."""
    rows = run(lambda services: _task_rows(services, include_terminal=include_terminal))
    if not rows:
        console.print("no tasks")
        return
    table = Table(title="tasks")
    for column in ("ID", "Status", "Priority", "Title", "Estimate", "Deadline", "Actual"):
        table.add_column(column)
    for task, deadline_at, actual_seconds in rows:
        table.add_row(
            short_id(task.id),
            task.status.value,
            task.priority.value,
            task.title,
            _minutes(task.estimated_minutes),
            "-" if deadline_at is None else format_local(deadline_at),
            _duration(actual_seconds),
        )
    console.print(table)


async def _task_rows(
    services: Services, *, include_terminal: bool
) -> list[tuple[Task, datetime | None, int]]:
    tasks = await services.tasks.list_tasks(include_terminal=include_terminal)
    if not tasks:
        return []
    deadlines = await services.commitments.list_deadlines([task.id for task in tasks])
    totals = await services.work.actual_seconds_for_tasks([task.id for task in tasks])
    rows = [
        (
            task,
            deadlines[task.id].due_at if task.id in deadlines else None,
            totals.get(task.id, 0),
        )
        for task in tasks
    ]
    rows.sort(key=_task_sort_key)
    return rows


def _task_sort_key(row: tuple[Task, datetime | None, int]) -> tuple[int, datetime, str]:
    task, deadline_at, _seconds = row
    if deadline_at is not None:
        return (0, deadline_at, str(task.id))
    return (1, task.created_at, str(task.id))


@task_app.command("add")
def task_add(
    title: Annotated[str, typer.Argument(help="Task title.")],
    estimate: Annotated[
        int | None, typer.Option("--estimate", help="Estimated minutes (>= 1).")
    ] = None,
    priority: Annotated[str, typer.Option("--priority", help="low | normal | high.")] = "normal",
    description: Annotated[
        str | None, typer.Option("--description", help="Optional description.")
    ] = None,
    deadline: Annotated[
        str | None,
        typer.Option("--deadline", help="ISO 8601 deadline with offset, e.g. +08:00."),
    ] = None,
) -> None:
    """Create a task (structured input only; no natural-language dates)."""
    due_at = timestamp(deadline, field_name="--deadline") if deadline else None
    command = CreateTask(
        title=title,
        description=description,
        priority=priority_of(priority),
        estimated_minutes=estimate,
        due_at=due_at,
    )
    task = run(lambda services: services.tasks.create_task(command))
    console.print(f"[green]created[/green] {short_id(task.id)}  {task.title}")
    console.print(f"  id: {task.id}")
    if due_at is not None:
        console.print(f"  deadline: {format_local(due_at)}")


@task_app.command("show")
def task_show(reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")]) -> None:
    """Show one task with its deadline, plan blocks and work sessions."""
    task, deadline, blocks, sessions, actual_seconds = run(
        lambda services: _task_detail(services, reference)
    )
    table = Table(title=f"task {short_id(task.id)}", show_header=False, title_justify="left")
    table.add_row("ID", str(task.id))
    table.add_row("Title", task.title)
    table.add_row("Description", task.description or "-")
    table.add_row("Status", task.status.value)
    table.add_row("Priority", task.priority.value)
    table.add_row("Estimate", _minutes(task.estimated_minutes))
    table.add_row("Actual work", f"{_duration(actual_seconds)} ({actual_seconds}s)")
    table.add_row("Deadline", "-" if deadline is None else format_local(deadline.due_at))
    table.add_row("Created", format_local(task.created_at))
    table.add_row("Updated", format_local(task.updated_at))
    if task.completed_at is not None:
        table.add_row("Completed", format_local(task.completed_at))
    if task.cancelled_at is not None:
        table.add_row("Cancelled", format_local(task.cancelled_at))
    console.print(table)
    _print_plan_blocks(blocks)
    _print_work_sessions(sessions)


async def _task_detail(
    services: Services, reference: str
) -> tuple[Task, Deadline | None, list[PlanBlock], list[WorkSession], int]:
    task_id = await services.tasks.resolve_task_id(reference)
    task = await services.tasks.require_task(task_id)
    deadline = await services.tasks.get_deadline(task_id)
    blocks = await services.calendar.list_plan_blocks_for_task(task_id, include_cancelled=True)
    sessions = await services.work.list_task_sessions(task_id)
    actual_seconds = await services.work.get_task_actual_seconds(task_id)
    return task, deadline, blocks, sessions, actual_seconds


@task_app.command("done")
def task_done(reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")]) -> None:
    """Complete a task; unfinished plan blocks are cancelled atomically."""
    result = run(lambda services: _complete(services, reference))
    console.print(f"[green]completed[/green] {short_id(result.task.id)}  {result.task.title}")
    console.print(f"  cancelled plan blocks: {result.cancelled_plan_blocks}")


async def _complete(services: Services, reference: str) -> Any:
    task_id = await services.tasks.resolve_task_id(reference)
    return await services.tasks.complete_task(task_id)


@task_app.command("cancel")
def task_cancel(
    reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")]
) -> None:
    """Cancel a task; unfinished plan blocks are cancelled atomically."""
    result = run(lambda services: _cancel(services, reference))
    console.print(f"[yellow]cancelled[/yellow] {short_id(result.task.id)}  {result.task.title}")
    console.print(f"  cancelled plan blocks: {result.cancelled_plan_blocks}")


async def _cancel(services: Services, reference: str) -> Any:
    task_id = await services.tasks.resolve_task_id(reference)
    return await services.tasks.cancel_task(task_id)


@task_app.command("deadline")
def task_deadline(
    reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")],
    due_at: Annotated[
        str | None, typer.Argument(help="ISO 8601 due time with offset.")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the active deadline.")] = False,
) -> None:
    """Set, move or clear a task's deadline (OPEN tasks only)."""
    if clear and due_at is not None:
        fail("pass either a due time or --clear, not both")
    if not clear and due_at is None:
        fail("a due time is required unless --clear is used")
    if clear:
        run(lambda services: _clear_deadline(services, reference))
        console.print(f"[green]deadline cleared[/green] {reference}")
        return
    parsed = timestamp(due_at or "", field_name="due time")
    deadline = run(lambda services: _set_deadline(services, reference, parsed))
    console.print(f"[green]deadline set[/green] {format_local(deadline.due_at)}")


async def _set_deadline(services: Services, reference: str, due_at: datetime) -> Deadline:
    task_id = await services.tasks.resolve_task_id(reference)
    return await services.tasks.set_deadline(task_id, due_at)


async def _clear_deadline(services: Services, reference: str) -> None:
    task_id = await services.tasks.resolve_task_id(reference)
    await services.tasks.clear_deadline(task_id)


@calendar_app.callback(invoke_without_command=True)
def calendar_default(
    ctx: typer.Context,
    days: Annotated[int, typer.Option("--days", help="Window size in days.")] = 7,
) -> None:
    """Show active calendar events and plan blocks for the next `--days` days."""
    if ctx.invoked_subcommand is not None:
        return
    intervals = run(lambda services: _busy(services, days))
    if not intervals:
        console.print("nothing scheduled")
        return
    table = Table(title=f"busy time (next {days} days)")
    for column in ("Kind", "ID", "Start", "End", "Title"):
        table.add_column(column)
    for interval in intervals:
        table.add_row(
            interval.source_kind.value,
            short_id(interval.source_id),
            format_local(interval.starts_at),
            format_local(interval.ends_at),
            interval.title or "-",
        )
    console.print(table)


async def _busy(services: Services, days: int) -> list[Any]:
    now = services.clock.now()
    return await services.calendar.get_busy_intervals(
        query_start=now, query_end=now + timedelta(days=days)
    )


@calendar_app.command("add")
def calendar_add(
    title: Annotated[str, typer.Argument(help="What occupies the time.")],
    start: Annotated[str, typer.Option("--start", help="ISO 8601 start with offset.")],
    end: Annotated[str, typer.Option("--end", help="ISO 8601 end with offset.")],
    description: Annotated[
        str | None, typer.Option("--description", help="Optional description.")
    ] = None,
) -> None:
    """Record time that is already occupied."""
    command = CreateCalendarEvent(
        title=title,
        starts_at=timestamp(start, field_name="--start"),
        ends_at=timestamp(end, field_name="--end"),
        description=description,
    )
    event: CalendarEvent = run(lambda services: services.calendar.create_event(command))
    console.print(
        f"[green]event[/green] {short_id(event.id)}  "
        f"{format_local(event.starts_at)} -> {format_local(event.ends_at)}  {event.title}"
    )


@plan_app.command("add")
def plan_add(
    reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")],
    start: Annotated[str, typer.Option("--start", help="ISO 8601 start with offset.")],
    end: Annotated[str, typer.Option("--end", help="ISO 8601 end with offset.")],
) -> None:
    """Plan time for an OPEN task."""
    parsed_start = timestamp(start, field_name="--start")
    parsed_end = timestamp(end, field_name="--end")
    block: PlanBlock = run(
        lambda services: _plan_add(services, reference, parsed_start, parsed_end)
    )
    console.print(
        f"[green]planned[/green] {short_id(block.id)}  "
        f"{format_local(block.starts_at)} -> {format_local(block.ends_at)}"
    )


async def _plan_add(
    services: Services, reference: str, starts_at: datetime, ends_at: datetime
) -> PlanBlock:
    task_id = await services.tasks.resolve_task_id(reference)
    return await services.calendar.create_plan_block(
        CreatePlanBlock(task_id=task_id, starts_at=starts_at, ends_at=ends_at)
    )


@plan_app.command("cancel")
def plan_cancel(
    reference: Annotated[str, typer.Argument(help="Plan block id or unique prefix.")]
) -> None:
    """Cancel a plan block by hand."""
    block: PlanBlock = run(lambda services: _plan_cancel(services, reference))
    console.print(f"[yellow]cancelled[/yellow] {short_id(block.id)}")


async def _plan_cancel(services: Services, reference: str) -> PlanBlock:
    block_id = await services.calendar.resolve_plan_block_id(reference)
    return await services.calendar.cancel_plan_block(block_id)


@work_app.command("add")
def work_add(
    reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")],
    start: Annotated[str, typer.Option("--start", help="ISO 8601 start with offset.")],
    end: Annotated[str, typer.Option("--end", help="ISO 8601 end with offset.")],
) -> None:
    """Record work that actually happened (allowed after completion, for back-filling)."""
    parsed_start = timestamp(start, field_name="--start")
    parsed_end = timestamp(end, field_name="--end")
    session: WorkSession = run(
        lambda services: _work_add(services, reference, parsed_start, parsed_end)
    )
    console.print(
        f"[green]logged[/green] {short_id(session.id)}  "
        f"{session.duration_seconds}s ({_duration(session.duration_seconds)})"
    )


async def _work_add(
    services: Services, reference: str, started_at: datetime, ended_at: datetime
) -> WorkSession:
    task_id = await services.tasks.resolve_task_id(reference)
    return await services.work.record_session(
        task_id=task_id, started_at=started_at, ended_at=ended_at
    )


@work_app.command("list")
def work_list(reference: Annotated[str, typer.Argument(help="Task id or unique prefix.")]) -> None:
    """List a task's work sessions and total actual effort."""
    sessions, total = run(lambda services: _work_list(services, reference))
    _print_work_sessions(sessions)
    console.print(f"total actual work: {_duration(total)} ({total}s)")


async def _work_list(services: Services, reference: str) -> tuple[list[WorkSession], int]:
    task_id = await services.tasks.resolve_task_id(reference)
    sessions = await services.work.list_task_sessions(task_id)
    total = await services.work.get_task_actual_seconds(task_id)
    return sessions, total


def _print_plan_blocks(blocks: list[PlanBlock]) -> None:
    if not blocks:
        console.print("plan blocks: none")
        return
    table = Table(title="plan blocks")
    for column in ("ID", "Status", "Start", "End", "Planned"):
        table.add_column(column)
    for block in blocks:
        table.add_row(
            short_id(block.id),
            block.status.value,
            format_local(block.starts_at),
            format_local(block.ends_at),
            _duration(block.planned_seconds),
        )
    console.print(table)


def _print_work_sessions(sessions: list[WorkSession]) -> None:
    if not sessions:
        console.print("work sessions: none")
        return
    table = Table(title="work sessions")
    for column in ("ID", "Start", "End", "Duration"):
        table.add_column(column)
    for session in sessions:
        table.add_row(
            short_id(session.id),
            format_local(session.started_at),
            format_local(session.ended_at),
            _duration(session.duration_seconds),
        )
    console.print(table)


def _minutes(value: int | None) -> str:
    return "-" if value is None else f"{value}m"


def _duration(seconds: int) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    if minutes == 0:
        return f"{remainder}s"
    return f"{minutes}m{remainder:02d}s"


def register(app: typer.Typer) -> None:
    """Attach the commitment commands to the root CLI."""
    app.add_typer(task_app, name="task")
    app.add_typer(calendar_app, name="calendar")
    app.add_typer(plan_app, name="plan")
    app.add_typer(work_app, name="work")
    app.command("tasks")(list_tasks_command)


__all__ = ["register", "run", "services_for"]
