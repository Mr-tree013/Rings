"""Scheduler CLI: the durable notification inbox and scheduled-job visibility (ADR-0016).

Read-only except for `pw notification read`. There is deliberately no `pw scheduled create`,
no manual retry and no payload editing: jobs exist because a commitment mutation materialized
them, and a CLI that could inject arbitrary jobs would be a way around every invariant in
ADR-0016.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from assistant.cli_commitments import Services, run
from assistant.cli_support import console, format_local, short_id
from assistant.domain.notification import Notification
from assistant.domain.scheduled_job import (
    ACTIVE_JOB_STATUSES,
    ScheduledJob,
)

notifications_app = typer.Typer(help="Durable notification inbox.", no_args_is_help=False)


@notifications_app.callback(invoke_without_command=True)
def notifications_list(
    ctx: typer.Context,
    include_read: Annotated[
        bool, typer.Option("--all", help="Include notifications that are already read.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many to show.")] = 20,
) -> None:
    """List notifications, newest first (unread only by default)."""
    if ctx.invoked_subcommand is not None:
        return
    rows = run(
        lambda services: _list_notifications(services, include_read=include_read, limit=limit),
        load_config=False,
    )
    if not rows:
        console.print("no notifications")
        return
    table = Table(title="notifications")
    for column in ("ID", "Kind", "Status", "Created", "Title", "Related"):
        table.add_column(column)
    for notification in rows:
        table.add_row(
            short_id(notification.id),
            notification.kind.value,
            notification.status.value,
            format_local(notification.created_at),
            notification.title,
            _related_label(notification),
        )
    console.print(table)


async def _list_notifications(
    services: Services, *, include_read: bool, limit: int
) -> list[Notification]:
    return await services.scheduler.list_notifications(
        unread_only=not include_read, limit=limit
    )


@notifications_app.command("show")
def notification_show(
    reference: Annotated[str, typer.Argument(help="Notification id or unique prefix.")]
) -> None:
    """Show one notification in full."""
    notification = run(lambda services: _show(services, reference), load_config=False)
    table = Table(
        title=f"notification {short_id(notification.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("ID", str(notification.id))
    table.add_row("Kind", notification.kind.value)
    table.add_row("Status", notification.status.value)
    table.add_row("Title", notification.title)
    table.add_row("Body", notification.body)
    table.add_row("Related", _related_label(notification))
    table.add_row("Created", format_local(notification.created_at))
    if notification.read_at is not None:
        table.add_row("Read", format_local(notification.read_at))
    console.print(table)


async def _show(services: Services, reference: str) -> Notification:
    notification_id = await services.scheduler.resolve_notification_id(reference)
    notification = await services.scheduler.get_notification(notification_id)
    if notification is None:  # pragma: no cover - defensive: resolution just found it
        raise typer.Exit(code=1)
    return notification


@notifications_app.command("read")
def notification_read(
    reference: Annotated[str, typer.Argument(help="Notification id or unique prefix.")]
) -> None:
    """Mark a notification read. Reading an already-read notification succeeds."""
    notification = run(lambda services: _read(services, reference), load_config=False)
    console.print(f"[green]read[/green] {short_id(notification.id)}  {notification.title}")


async def _read(services: Services, reference: str) -> Notification:
    notification_id = await services.scheduler.resolve_notification_id(reference)
    return await services.scheduler.mark_notification_read(
        notification_id, at=services.clock.now()
    )


def _related_label(notification: Notification) -> str:
    related: list[str] = []
    if notification.related_task_id is not None:
        related.append(f"task {short_id(notification.related_task_id)}")
    if notification.related_proposal_id is not None:
        related.append(f"proposal {short_id(notification.related_proposal_id)}")
    return ", ".join(related) or "-"


def scheduled_jobs(
    include_terminal: Annotated[
        bool, typer.Option("--all", help="Include completed, cancelled and dead-lettered jobs.")
    ] = False,
) -> None:
    """Show scheduled jobs: active ones by default (debug visibility, read-only)."""
    rows = run(
        lambda services: _list_jobs(services, include_terminal=include_terminal),
        load_config=False,
    )
    if not rows:
        console.print("no scheduled jobs")
        return
    table = Table(title="scheduled jobs")
    for column in ("ID", "Kind", "Status", "Due", "Attempts"):
        table.add_column(column)
    for job in rows:
        table.add_row(
            short_id(job.id),
            job.kind.value,
            job.status.value,
            format_local(job.effective_due_at),
            str(job.attempts),
        )
    console.print(table)


async def _list_jobs(services: Services, *, include_terminal: bool) -> list[ScheduledJob]:
    return await services.scheduler.list_jobs(
        statuses=None if include_terminal else ACTIVE_JOB_STATUSES, limit=50
    )


def register(app: typer.Typer) -> None:
    """Register the scheduler commands on the root app."""
    app.add_typer(notifications_app, name="notifications")
    app.add_typer(notifications_app, name="notification", hidden=True)
    app.command("scheduled")(scheduled_jobs)


__all__ = ["notifications_app", "register", "scheduled_jobs"]
