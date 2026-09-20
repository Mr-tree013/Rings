"""`pw cases` — durable case containers (ADR-0023).

A case is a title and a lifecycle. Creating one is free of external effect, and completing or
cancelling one is a local state change: no command here can approve an action, and none can
execute one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.case_service import CaseDetail, CaseService
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.case import Case, CaseStatus
from assistant.domain.errors import AmbiguousId, CaseNotFound, DomainError
from assistant.store.errors import StoreError

MAX_CASE_LIST = 200
"""How many cases one list view may ask for."""

cases_app = typer.Typer(
    help="Cases: the container a multi-step piece of work lives in.",
    invoke_without_command=True,
)
case_app = typer.Typer(
    help="One case: add, show, complete or cancel it.", no_args_is_help=True
)


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except (CaseNotFound, AmbiguousId) as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"case store failure: {exc}")


async def _service() -> CaseService:
    clock = bootstrap.system_clock()
    return bootstrap.case_service(clock, bootstrap.runtime_database(clock))


@cases_app.callback()
def cases_root(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status", help="Only cases in this status (open/completed/cancelled)."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many cases to show.")] = 20,
) -> None:
    """List cases, newest update first."""
    if ctx.invoked_subcommand is not None:
        return
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_CASE_LIST:
        fail(f"--limit must be at most {MAX_CASE_LIST}")
    statuses = None
    if status is not None:
        try:
            statuses = (CaseStatus(status),)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in CaseStatus)
            fail(f"unknown status {status!r}; expected one of: {allowed}")
            raise AssertionError("unreachable") from exc
    cases = _run(lambda: _list_cases(statuses, limit))
    if not cases:
        console.print("no cases")
        return
    table = Table(title="cases")
    for column in ("ID", "Status", "Updated", "Title"):
        table.add_column(column)
    for case in cases:
        table.add_row(
            short_id(case.id),
            case.status.value,
            format_local(case.updated_at),
            _preview(case.title),
        )
    console.print(table)


async def _list_cases(
    statuses: tuple[CaseStatus, ...] | None, limit: int
) -> list[Case]:
    service = await _service()
    return await service.list_cases(statuses=statuses, limit=limit)


def _preview(text: str, limit: int = 60) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "\u2026"


@case_app.command("add")
def case_add(
    title: Annotated[str, typer.Argument(help="What this case is about.")]
) -> None:
    """Open a new case."""
    case = _run(lambda: _add_case(title))
    console.print(f"[green]created[/green] {short_id(case.id)}  {case.title}")


async def _add_case(title: str) -> Case:
    service = await _service()
    return await service.create_case(title)


@case_app.command("show")
def case_show(
    reference: Annotated[str, typer.Argument(help="Case id or unique prefix.")]
) -> None:
    """Show one case and the actions prepared inside it."""
    detail = _run(lambda: _load_case(reference))
    case = detail.case
    table = Table(
        title=f"case {short_id(case.id)}", show_header=False, title_justify="left"
    )
    table.add_row("ID", str(case.id))
    table.add_row("Title", case.title)
    table.add_row("Status", case.status.value)
    table.add_row("Created", format_local(case.created_at))
    table.add_row("Updated", format_local(case.updated_at))
    if case.completed_at is not None:
        table.add_row("Completed", format_local(case.completed_at))
    if case.cancelled_at is not None:
        table.add_row("Cancelled", format_local(case.cancelled_at))
    console.print(table)
    if not detail.actions:
        console.print("actions: none")
        return
    actions = Table(title="actions")
    for column in ("ID", "Type", "Status", "Fingerprint", "Prepared"):
        actions.add_column(column)
    for action in detail.actions:
        actions.add_row(
            short_id(action.id),
            action.action_type.value,
            action.status.value,
            short_id(action.fingerprint),
            format_local(action.created_at),
        )
    console.print(actions)


async def _load_case(reference: str) -> CaseDetail:
    service = await _service()
    return await service.get_case(reference)


@case_app.command("done")
def case_done(
    reference: Annotated[str, typer.Argument(help="Case id or unique prefix.")]
) -> None:
    """Mark an open case completed."""
    case = _run(lambda: _complete_case(reference))
    console.print(f"[green]completed[/green] {short_id(case.id)}  {case.title}")


async def _complete_case(reference: str) -> Case:
    service = await _service()
    return await service.complete_case(reference)


@case_app.command("cancel")
def case_cancel(
    reference: Annotated[str, typer.Argument(help="Case id or unique prefix.")]
) -> None:
    """Cancel an open case."""
    case = _run(lambda: _cancel_case(reference))
    console.print(f"[yellow]cancelled[/yellow] {short_id(case.id)}  {case.title}")


async def _cancel_case(reference: str) -> Case:
    service = await _service()
    return await service.cancel_case(reference)


def register(app: typer.Typer) -> None:
    """Register the `pw cases` and `pw case` groups on the root app."""
    app.add_typer(cases_app, name="cases")
    app.add_typer(case_app, name="case")


__all__ = ["cases_app", "register"]
