"""`pw watch` — configured public pages and what changed on them (ADR-0029).

```text
pw watch targets                        what is configured, and when each was last checked
pw watch status                         state of every target: baseline, last change, counters
pw watch sync [--target TARGET]         fetch now (the only command here that uses the network)
pw watch observations [--target TARGET] the recorded versions, newest first
pw watch observation show OBSERVATION   one observation: hashes, preview, analysis
```

Only `pw watch sync` touches the network, and it does so through the same rules the daemon uses:
public addresses only, no redirects, a byte budget, and no content beyond what a configured page
returned. Everything else reads local state, which is why a status command works offline.

The observation view prints hashes, a bounded preview and the analysis — never a filesystem path,
because where a snapshot lives is not part of what a user needs to reason about.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.web_watch import TargetOutcome, WebTargetResult, WebWatchService
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    AmbiguousId,
    DomainError,
    InvalidAssistantConfig,
    WebObservationNotFound,
    WebTargetNotFound,
)
from assistant.domain.observation_analysis import ObservationAnalysis
from assistant.domain.web_watch import WebObservation, WebWatchState
from assistant.ports.web_watch_repository import WebWatchRepository
from assistant.store.errors import StoreError

MAX_WATCH_LIST = 500
"""How many rows one list view may ask for."""

watch_app = typer.Typer(
    help="Web watchers: what is configured, what changed, and the recorded observations.",
    no_args_is_help=True,
)
observation_app = typer.Typer(
    help="One observation: its hashes, a bounded preview and its analysis.",
    no_args_is_help=True,
)

_EXPECTED_FAILURES = (
    AmbiguousId,
    InvalidAssistantConfig,
    WebObservationNotFound,
    WebTargetNotFound,
)


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except _EXPECTED_FAILURES as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"watcher store failure: {exc}")


def _config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


async def _service() -> WebWatchService:
    clock = bootstrap.system_clock()
    config = await bootstrap.config_loader().load()
    return bootstrap.web_watch_service(
        config, clock, bootstrap.runtime_database(clock)
    )


def _repository() -> WebWatchRepository:
    """Reads go straight to the store: a status command must work with no configuration at all."""
    clock = bootstrap.system_clock()
    return bootstrap.web_watch_repository(bootstrap.runtime_database(clock))


def _checked_limit(limit: int) -> int:
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_WATCH_LIST:
        fail(f"--limit must be at most {MAX_WATCH_LIST}")
    return limit


# ---------------------------------------------------------------------- targets


@watch_app.command("targets")
def watch_targets() -> None:
    """List the configured watcher targets. This command contacts nothing."""
    config = _config_or_fail()
    if not config.watchers.web:
        console.print("no watcher targets are configured")
        return
    table = Table(title="watcher targets")
    for column in ("ID", "URL", "Enabled", "Last checked", "Last changed"):
        table.add_column(column)
    states = _run(lambda: _states([target.id for target in config.watchers.web]))
    for target in config.watchers.web:
        state = states[target.id]
        table.add_row(
            target.id,
            target.url,
            "yes" if target.enabled else "no",
            "never"
            if state is None or state.last_checked_at is None
            else format_local(state.last_checked_at),
            "never"
            if state is None or state.last_changed_at is None
            else format_local(state.last_changed_at),
        )
    console.print(table)
    console.print(
        f"Poll interval: {config.watchers.poll_interval_seconds}s   "
        f"Full fetch every {config.watchers.full_fetch_every} checks   "
        f"Max response: {config.watchers.max_response_bytes} bytes"
    )


async def _states(target_ids: list[str]) -> dict[str, WebWatchState | None]:
    """Read every requested target's state over one repository."""
    repository = _repository()
    return {target_id: await repository.get_state(target_id) for target_id in target_ids}


@watch_app.command("status")
def watch_status() -> None:
    """Show what is known about every target: baseline, hashes and counters. Local only."""
    config = _config_or_fail()
    if not config.watchers.enabled_targets:
        console.print("no enabled watcher targets")
        return
    table = Table(title="watcher status")
    for column in ("ID", "Baseline", "Content", "Checks since full", "Last checked"):
        table.add_column(column)
    states = _run(lambda: _states([target.id for target in config.watchers.enabled_targets]))
    for target in config.watchers.enabled_targets:
        state = states[target.id]
        table.add_row(
            target.id,
            "yes" if state is not None and state.has_baseline else "no",
            "none" if state is None or state.content_sha256 is None
            else short_id(state.content_sha256),
            "0" if state is None else str(state.checks_since_full),
            "never"
            if state is None or state.last_checked_at is None
            else format_local(state.last_checked_at),
        )
    console.print(table)
    console.print(
        "An initial fetch establishes a baseline and records no change event; "
        "only later content changes are bridged to the event inbox."
    )


@watch_app.command("sync")
def watch_sync(
    target: Annotated[
        str | None, typer.Option("--target", help="Only this configured target id.")
    ] = None,
) -> None:
    """Fetch now. This is the one command in this group that uses the network."""
    results = _run(lambda: _sync(target))
    table = Table(title="watcher sync")
    for column in ("Target", "Outcome", "Observation", "Event"):
        table.add_column(column)
    for result in results:
        table.add_row(
            result.target_id,
            result.outcome.value,
            "" if result.observation_id is None else short_id(result.observation_id),
            "" if result.event_id is None else short_id(result.event_id),
        )
    console.print(table)
    failed = [item for item in results if item.outcome is TargetOutcome.FAILED]
    for item in failed:
        console.print(f"[yellow]{item.target_id}:[/yellow] {item.error}")
    if not failed:
        console.print(
            "No external side effect was attempted beyond fetching the configured pages."
        )


async def _sync(target: str | None) -> tuple[WebTargetResult, ...]:
    service = await _service()
    return await service.sync_once(target)


# ----------------------------------------------------------------- observations


@watch_app.command("observations")
def watch_observations(
    target: Annotated[
        str | None, typer.Option("--target", help="Only this configured target id.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List recorded observations, newest first. Local only."""
    checked = _checked_limit(limit)
    if target is not None:
        config = _config_or_fail()
        known = {item.id for item in config.watchers.web}
        if target not in known:
            fail(f"no watcher target is configured with id {target!r}")
    observations = _run(lambda: _observations(target, checked))
    if not observations:
        console.print("no observations recorded yet")
        return
    table = Table(title="web observations")
    for column in ("ID", "Target", "Fetched", "Kind", "Content", "Previous"):
        table.add_column(column)
    for observation in observations:
        table.add_row(
            short_id(observation.id),
            observation.target_id,
            format_local(observation.fetched_at),
            "baseline" if observation.is_baseline else "change",
            short_id(observation.content_sha256),
            ""
            if observation.previous_observation_id is None
            else short_id(observation.previous_observation_id),
        )
    console.print(table)


async def _observations(
    target: str | None, limit: int
) -> list[WebObservation]:
    return await _repository().list_observations(target_id=target, limit=limit)


@observation_app.command("show")
def observation_show(
    reference: Annotated[str, typer.Argument(help="Observation id or unique prefix.")]
) -> None:
    """Show one observation: hashes, a bounded preview and the analysis, if any."""
    detail = _run(lambda: _load_observation(reference))
    observation = detail.observation
    table = Table(title="web observation", show_header=False, title_justify="left")
    table.add_row("Observation ID", str(observation.id))
    table.add_row("Target", observation.target_id)
    table.add_row("URL", observation.url)
    table.add_row("Fetched", format_local(observation.fetched_at))
    table.add_row("Kind", "baseline" if observation.is_baseline else "change")
    table.add_row("Hash", observation.content_sha256)
    table.add_row(
        "Previous",
        "none"
        if observation.previous_observation_id is None
        else str(observation.previous_observation_id),
    )
    console.print(table)
    console.print("")
    console.print("[bold]Content[/bold]")
    console.print(_preview(detail.content, limit=2000))
    console.print("")
    console.print("[bold]Analysis[/bold]")
    if detail.analysis is None:
        console.print("  pending (no analysis recorded for this observation yet)")
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
    console.print("")
    console.print("Snapshots live in the runtime data directory; this view never shows a path.")


def _preview(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


class _ObservationDetail:
    """One observation, its stored text and the analysis of its event, if any."""

    def __init__(
        self,
        observation: WebObservation,
        content: str,
        analysis: ObservationAnalysis | None,
    ) -> None:
        self.observation = observation
        self.content = content
        self.analysis = analysis


async def _load_observation(reference: str) -> _ObservationDetail:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.web_watch_repository(database)
    observation_id = await repository.resolve_observation_id(reference)
    observation = await repository.get_observation(observation_id)
    if observation is None:  # pragma: no cover - resolve already proved it exists
        raise WebObservationNotFound(observation_id)
    content = await bootstrap.web_snapshot_store().read_text(observation.storage_key)
    analysis = None
    event_id = await repository.get_linked_event_id(observation.id)
    if event_id is not None:
        stored = await bootstrap.observation_analysis_repository(database).get_analysis(
            event_id
        )
        analysis = stored
    return _ObservationDetail(
        observation=observation, content=content, analysis=analysis
    )


def register(app: typer.Typer) -> None:
    """Register the `pw watch` and `pw watch observation` groups on the root app."""
    watch_app.add_typer(observation_app, name="observation")
    app.add_typer(watch_app, name="watch")


__all__ = ["observation_app", "register", "watch_app"]
