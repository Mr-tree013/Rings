"""`pw ingest` — handing the assistant text a person pasted in (ADR-0029).

```text
pw ingest text "Forwarded notice" --source qq-forward
pw ingest list
pw ingest show INPUT
```

`pw ingest text` stores the text durably and queues one `manual.input.received` event. It does not
call a model: the daemon's worker does that, when a provider is configured, which is why the
command says so out loud instead of pretending the analysis already happened.

`pw ingest show` prints the stored text because a person asked for it. What it never prints is a
path, a hash of anything private, or anything that is not in the row the user is looking at.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.manual_input_service import (
    ManualInputService,
    ManualInputStored,
)
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.errors import (
    AmbiguousId,
    DomainError,
    InvalidAssistantConfig,
    InvalidManualInput,
    ManualInputNotFound,
)
from assistant.domain.manual_input import ManualInput, ManualInputSource
from assistant.domain.observation_analysis import ObservationAnalysis
from assistant.store.errors import StoreError

MAX_INGEST_LIST = 500
"""How many rows one list view may ask for."""

ingest_app = typer.Typer(
    help="Manual input: store pasted text and queue it for analysis.",
    no_args_is_help=True,
)

_EXPECTED_FAILURES = (
    AmbiguousId,
    InvalidAssistantConfig,
    InvalidManualInput,
    ManualInputNotFound,
)

_SOURCES = {
    "manual": ManualInputSource.MANUAL,
    "qq-forward": ManualInputSource.QQ_FORWARD,
    "other": ManualInputSource.OTHER,
}


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except _EXPECTED_FAILURES as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"manual input store failure: {exc}")


def _service() -> ManualInputService:
    clock = bootstrap.system_clock()
    return bootstrap.manual_input_service(clock, bootstrap.runtime_database(clock))


@ingest_app.command("text")
def ingest_text(
    text: Annotated[str, typer.Argument(help="The text to store, in your own words.")],
    source: Annotated[
        str,
        typer.Option("--source", help="Where it came from: manual, qq-forward or other."),
    ] = "manual",
) -> None:
    """Store pasted text and queue it for analysis. No model is called here."""
    if source not in _SOURCES:
        allowed = ", ".join(sorted(_SOURCES))
        fail(f"unknown source {source!r}; expected one of: {allowed}")
    stored = _run(lambda: _create(text, _SOURCES[source]))
    console.print(f"[green]Stored manual input:[/green] {stored.manual_input.id}")
    console.print(f"Queued inbound event: {stored.event_id}")
    console.print("")
    console.print(
        "If model analysis is configured and assistantd is running, the text may be sent to the "
        "configured model provider for classification."
    )


async def _create(text: str, source: ManualInputSource) -> ManualInputStored:
    return await _service().create_input(text, source=source)


@ingest_app.command("list")
def ingest_list(
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List stored manual input, newest first."""
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_INGEST_LIST:
        fail(f"--limit must be at most {MAX_INGEST_LIST}")
    inputs = _run(lambda: _list(limit))
    if not inputs:
        console.print("no manual input stored")
        return
    table = Table(title="manual input")
    for column in ("ID", "Source", "Created", "Text preview"):
        table.add_column(column)
    for manual_input in inputs:
        table.add_row(
            short_id(manual_input.id),
            manual_input.source.value,
            format_local(manual_input.created_at),
            manual_input.preview(),
        )
    console.print(table)
    console.print(
        "Manual input is untrusted quoted text: it may be forwarded content from someone else."
    )


async def _list(limit: int) -> list[ManualInput]:
    return await _service().list_inputs(limit=limit)


@ingest_app.command("show")
def ingest_show(
    reference: Annotated[str, typer.Argument(help="Manual input id or unique prefix.")]
) -> None:
    """Show one stored input, its text and the analysis of it, if any."""
    detail = _run(lambda: _load(reference))
    manual_input = detail.manual_input
    table = Table(title="manual input", show_header=False, title_justify="left")
    table.add_row("Input ID", str(manual_input.id))
    table.add_row("Source", manual_input.source.value)
    table.add_row("Created", format_local(manual_input.created_at))
    console.print(table)
    console.print("")
    console.print("[bold]Text[/bold]")
    console.print(manual_input.text)
    console.print("")
    console.print("[bold]Analysis[/bold]")
    if detail.analysis is None:
        console.print("  pending (no analysis recorded for this input yet)")
    else:
        console.print(f"  category: {detail.analysis.category.value}")
        console.print(f"  summary: {detail.analysis.summary}")
        for candidate in detail.analysis.action_candidates:
            time_part = (
                ""
                if candidate.time_text is None
                else f"  [{candidate.temporal_kind.value}: {candidate.time_text}]"
            )
            console.print(f"  - {candidate.text}{time_part}")


class _ManualInputDetail:
    """One stored input plus the analysis of the event that queued it."""

    def __init__(
        self,
        manual_input: ManualInput,
        analysis: ObservationAnalysis | None,
    ) -> None:
        self.manual_input = manual_input
        self.analysis = analysis


async def _load(reference: str) -> _ManualInputDetail:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.manual_input_service(clock, database)
    manual_input = await service.get_input(reference)
    repository = bootstrap.manual_input_repository(database)
    event_id = await repository.get_linked_event_id(manual_input.id)
    analysis = None
    if event_id is not None:
        analysis = await bootstrap.observation_analysis_repository(database).get_analysis(
            event_id
        )
    return _ManualInputDetail(manual_input=manual_input, analysis=analysis)


def register(app: typer.Typer) -> None:
    """Register the `pw ingest` group on the root app."""
    app.add_typer(ingest_app, name="ingest")


__all__ = ["ingest_app", "register"]
