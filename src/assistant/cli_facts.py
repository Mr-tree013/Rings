"""`pw fact`, `pw facts`, `pw correction`, `pw corrections` — human-confirmed facts (ADR-0027).

```text
pw corrections                      what the user has told the assistant
pw correction add TEXT              record a correction on its own
pw correction show CORRECTION       one correction and the candidates it produced

pw fact candidate add KEY VALUE --note "why"   propose a fact, with its provenance
pw fact candidates [--all]                     what is waiting for a decision
pw fact candidate show CANDIDATE               the exact proposal and where it came from
pw fact candidate confirm CANDIDATE            promote it (the only way a fact is trusted)
pw fact candidate reject CANDIDATE             discard it, keeping the record

pw facts [--all]                    active, unexpired facts — or the whole history
pw fact show FACT                   one fact and the sentence it came from
```

Two absences are the point of this module. There is no `pw fact confirm-all` and no
`--edit-value`: the value a user approves has to be the value that was reviewed, so a change means
a new candidate with its own provenance. And nothing here writes anything except the three
learning tables — no task, no mail, no action and no fact is ever used to fill a form in this
phase.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.learning_service import (
    CandidateDetail,
    CorrectionDetail,
    FactDetail,
    FactOverview,
    FactProposal,
    LearningService,
)
from assistant.cli_support import (
    console,
    fail,
    format_local,
    parse_aware_datetime,
    short_id,
)
from assistant.domain.correction import Correction
from assistant.domain.errors import (
    AmbiguousId,
    ConfirmedFactNotFound,
    CorrectionNotFound,
    DomainError,
    ExpiredFactCandidate,
    FactCandidateNotFound,
    ForbiddenFactKey,
    InvalidCorrection,
    InvalidFactCandidate,
    InvalidFactCandidateTransition,
    InvalidFactKey,
)
from assistant.domain.fact import (
    FactCandidate,
    FactCandidateStatus,
)
from assistant.ports.learning_repository import FactConfirmation
from assistant.store.errors import StoreError

MAX_LEARNING_LIST = 500
"""How many rows one list view may ask for."""

candidate_app = typer.Typer(
    help="Candidate facts: propose, inspect, confirm or reject.", no_args_is_help=True
)
fact_app = typer.Typer(
    help="One fact: show it, or work with the candidates and facts around it.",
    no_args_is_help=True,
)
facts_app = typer.Typer(
    help="Confirmed personal facts (active and unexpired by default).",
    invoke_without_command=True,
)
correction_app = typer.Typer(
    help="One correction: add it or show it.", no_args_is_help=True
)
corrections_app = typer.Typer(
    help="What the user has told the assistant, in their own words.",
    invoke_without_command=True,
)

_EXPECTED_FAILURES = (
    AmbiguousId,
    ConfirmedFactNotFound,
    CorrectionNotFound,
    ExpiredFactCandidate,
    FactCandidateNotFound,
    ForbiddenFactKey,
    InvalidCorrection,
    InvalidFactCandidate,
    InvalidFactCandidateTransition,
    InvalidFactKey,
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
        fail(f"learning store failure: {exc}")


async def _service() -> LearningService:
    clock = bootstrap.system_clock()
    return bootstrap.learning_service(clock, bootstrap.runtime_database(clock))


def _checked_limit(limit: int) -> int:
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_LEARNING_LIST:
        fail(f"--limit must be at most {MAX_LEARNING_LIST}")
    return limit


def _now() -> datetime:
    """Wall-clock time for display only. Every write takes its time from the service clock."""
    return bootstrap.system_clock().now()


# ---------------------------------------------------------------------- corrections


@corrections_app.callback()
def corrections_root(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List corrections, newest first."""
    if ctx.invoked_subcommand is not None:
        return
    checked = _checked_limit(limit)
    corrections = _run(lambda: _list_corrections(checked))
    if not corrections:
        console.print("no corrections")
        return
    table = Table(title="corrections")
    for column in ("ID", "Created", "Text preview"):
        table.add_column(column)
    for correction in corrections:
        table.add_row(
            short_id(correction.id),
            format_local(correction.created_at),
            correction.preview(),
        )
    console.print(table)


async def _list_corrections(limit: int) -> list[Correction]:
    service = await _service()
    return await service.list_corrections(limit=limit)


@correction_app.command("add")
def correction_add(
    text: Annotated[str, typer.Argument(help="What is correct, in your own words.")]
) -> None:
    """Record a correction. Nothing is inferred from it."""
    correction = _run(lambda: _add_correction(text))
    console.print(f"[green]recorded[/green] {short_id(correction.id)}")
    console.print(correction.text)


async def _add_correction(text: str) -> Correction:
    service = await _service()
    return await service.add_correction(text)


@correction_app.command("show")
def correction_show(
    reference: Annotated[str, typer.Argument(help="Correction id or unique prefix.")]
) -> None:
    """Show one correction and the candidates it produced."""
    detail = _run(lambda: _load_correction(reference))
    correction = detail.correction
    console.print(f"[bold]Correction {short_id(correction.id)}[/bold]")
    console.print(f"Created: {format_local(correction.created_at)}")
    console.print("")
    console.print(correction.text)
    if not detail.candidates:
        console.print("")
        console.print("candidates: none")
        return
    console.print("")
    table = Table(title="candidates from this correction")
    for column in ("ID", "Key", "Value", "Status"):
        table.add_column(column)
    for candidate in detail.candidates:
        table.add_row(
            short_id(candidate.id),
            candidate.fact_key,
            candidate.preview(),
            candidate.status.value,
        )
    console.print(table)


async def _load_correction(reference: str) -> CorrectionDetail:
    service = await _service()
    return await service.get_correction(reference)


# ----------------------------------------------------------------------- candidates


@fact_app.command("candidates")
def fact_candidates(
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include confirmed and rejected candidates.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List candidate facts. Pending ones by default."""
    checked = _checked_limit(limit)
    statuses = None if all_statuses else (FactCandidateStatus.PENDING,)
    candidates = _run(lambda: _list_candidates(statuses, checked))
    if not candidates:
        console.print("no candidates")
        return
    table = Table(title="fact candidates")
    for column in ("ID", "Key", "Value preview", "Status", "Created"):
        table.add_column(column)
    for candidate in candidates:
        table.add_row(
            short_id(candidate.id),
            candidate.fact_key,
            candidate.preview(40),
            candidate.status.value,
            format_local(candidate.created_at),
        )
    console.print(table)
    console.print("A candidate is not a fact: it is not used for anything until you confirm it.")


async def _list_candidates(
    statuses: tuple[FactCandidateStatus, ...] | None, limit: int
) -> list[FactCandidate]:
    service = await _service()
    return await service.list_candidates(statuses=statuses, limit=limit)


@candidate_app.command("add")
def candidate_add(
    key: Annotated[str, typer.Argument(help="Namespaced fact key, e.g. profile.office.")],
    value: Annotated[str, typer.Argument(help="The value, as text.")],
    note: Annotated[
        str, typer.Option("--note", help="Why this is correct. Required, and stored as-is.")
    ],
    valid_until: Annotated[
        str | None,
        typer.Option(
            "--valid-until",
            help="Optional aware ISO 8601 expiry (e.g. 2026-12-31T00:00:00+08:00).",
        ),
    ] = None,
) -> None:
    """Propose a fact: the value plus the correction that justifies it."""
    if not note.strip():
        fail("--note is required: a candidate must carry the correction it came from")
    expiry = None
    if valid_until is not None:
        try:
            expiry = parse_aware_datetime(valid_until, field_name="--valid-until")
        except ValueError as exc:
            fail(str(exc))
    proposal = _run(lambda: _propose(key, value, note, expiry))
    console.print("[green]candidate created[/green]")
    console.print(f"Candidate: {proposal.candidate.id}")
    console.print(f"Key: {proposal.candidate.fact_key}")
    console.print(f"Value: {proposal.candidate.value}")
    console.print(f"Source correction: {proposal.correction.id}")
    console.print(f"Status: {proposal.candidate.status.value}")
    console.print("")
    console.print("This fact is not confirmed and will not be used automatically.")
    console.print(f"Next: pw fact candidate confirm {short_id(proposal.candidate.id)}")


async def _propose(
    key: str, value: str, note: str, valid_until: datetime | None
) -> FactProposal:
    service = await _service()
    return await service.propose_fact(
        key, value, note, valid_until=valid_until
    )


@candidate_app.command("show")
def candidate_show(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Show one candidate, its status, and the correction it came from."""
    detail = _run(lambda: _load_candidate(reference))
    candidate = detail.candidate
    table = Table(title="fact candidate", show_header=False, title_justify="left")
    table.add_row("Candidate", str(candidate.id))
    table.add_row("Key", candidate.fact_key)
    table.add_row("Value", candidate.value)
    table.add_row(
        "Proposed expiry",
        "none"
        if candidate.proposed_valid_until is None
        else format_local(candidate.proposed_valid_until),
    )
    table.add_row("Status", candidate.status.value)
    table.add_row("Created", format_local(candidate.created_at))
    if candidate.resolved_at is not None:
        table.add_row("Resolved", format_local(candidate.resolved_at))
    console.print(table)
    console.print("[bold]Source correction[/bold]")
    console.print(
        f"  {short_id(detail.correction.id)}  "
        f"({format_local(detail.correction.created_at)})"
    )
    console.print(detail.correction.text)
    if detail.fact is not None:
        console.print("")
        console.print(
            f"[green]confirmed[/green] as fact {short_id(detail.fact.id)} "
            f"({detail.fact.state_at(_now()).value})"
        )
    elif candidate.is_pending:
        console.print("")
        console.print(
            f"This candidate is not a confirmed fact. Confirm it with "
            f"`pw fact candidate confirm {short_id(candidate.id)}`."
        )


async def _load_candidate(reference: str) -> CandidateDetail:
    service = await _service()
    return await service.get_candidate(reference)


@candidate_app.command("confirm")
def candidate_confirm(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Confirm a candidate as a personal fact. This is the only way a fact is trusted."""
    confirmation = _run(lambda: _confirm(reference))
    fact = confirmation.fact
    console.print("[green]Confirmed fact:[/green]")
    console.print(f"Key: {fact.fact_key}")
    console.print(f"Value: {fact.value}")
    console.print(
        "Valid until: none"
        if fact.valid_until is None
        else f"Valid until: {format_local(fact.valid_until)}"
    )
    console.print("")
    console.print("Previous current value:")
    if confirmation.superseded is None:
        console.print("  none")
    else:
        console.print(f"  {confirmation.superseded.value}")
        console.print("")
        console.print("The previous fact was retained in history.")
    console.print("")
    console.print(
        "Nothing uses this fact yet: it is stored, reviewable and queryable, and it is not "
        "injected into models, mail drafts or eHall forms."
    )


async def _confirm(reference: str) -> FactConfirmation:
    service = await _service()
    return await service.confirm_fact(reference)


@candidate_app.command("reject")
def candidate_reject(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Reject a candidate. It stays as an audit record and is never used."""
    candidate = _run(lambda: _reject(reference))
    console.print(f"[yellow]rejected[/yellow] {short_id(candidate.id)}  {candidate.fact_key}")
    console.print("The candidate is kept as history; no fact was created or removed.")


async def _reject(reference: str) -> FactCandidate:
    service = await _service()
    return await service.reject_fact(reference)


# ------------------------------------------------------------------------- facts


@facts_app.callback()
def facts_root(
    ctx: typer.Context,
    all_facts: Annotated[
        bool, typer.Option("--all", help="Include expired and superseded history.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List confirmed facts: active and unexpired by default."""
    if ctx.invoked_subcommand is not None:
        return
    checked = _checked_limit(limit)
    overviews = _run(lambda: _list_facts(all_facts, checked))
    if not overviews:
        console.print("no facts")
        return
    table = Table(title="confirmed facts" + (" (including history)" if all_facts else ""))
    for column in ("ID", "Key", "Value preview", "State", "Valid until", "Source"):
        table.add_column(column)
    now = _now()
    for overview in overviews:
        fact = overview.fact
        table.add_row(
            short_id(fact.id),
            fact.fact_key,
            fact.preview(40),
            fact.state_at(now).value,
            "none"
            if fact.valid_until is None
            else format_local(fact.valid_until),
            ""
            if overview.correction is None
            else short_id(overview.correction.id),
        )
    console.print(table)


async def _list_facts(all_facts: bool, limit: int) -> list[FactOverview]:
    service = await _service()
    return await service.list_fact_overviews(include_inactive=all_facts, limit=limit)


@fact_app.command("show")
def fact_show(
    reference: Annotated[str, typer.Argument(help="Fact id or unique prefix.")]
) -> None:
    """Show one confirmed fact and the correction it came from."""
    detail = _run(lambda: _load_fact(reference))
    fact = detail.fact
    table = Table(title="confirmed fact", show_header=False, title_justify="left")
    table.add_row("Fact", str(fact.id))
    table.add_row("Key", fact.fact_key)
    table.add_row("Value", fact.value)
    table.add_row("State", fact.state_at(_now()).value)
    table.add_row("Valid from", format_local(fact.valid_from))
    table.add_row(
        "Valid until",
        "none" if fact.valid_until is None else format_local(fact.valid_until),
    )
    table.add_row("Confirmed", format_local(fact.created_at))
    if fact.superseded_at is not None:
        table.add_row("Superseded", format_local(fact.superseded_at))
    console.print(table)
    console.print("[bold]Provenance[/bold]")
    console.print(f"  candidate: {short_id(detail.candidate.id)}  {detail.candidate.value}")
    console.print(
        f"  correction: {short_id(detail.correction.id)} "
        f"({format_local(detail.correction.created_at)})"
    )
    console.print(detail.correction.text)


async def _load_fact(reference: str) -> FactDetail:
    service = await _service()
    return await service.get_fact(reference)


def register(app: typer.Typer) -> None:
    """Register the fact and correction command groups on the root app."""
    fact_app.add_typer(candidate_app, name="candidate")
    app.add_typer(fact_app, name="fact")
    app.add_typer(facts_app, name="facts")
    app.add_typer(correction_app, name="correction")
    app.add_typer(corrections_app, name="corrections")


__all__ = ["correction_app", "corrections_app", "fact_app", "facts_app", "register"]
