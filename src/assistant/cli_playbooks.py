"""`pw playbook`, `pw playbooks` — reviewed, non-executing reference blueprints (ADR-0028).

```text
pw playbook candidates                          what is waiting for review
pw playbook candidate add ACTION --name ... --note ...
                                              remember one successful action, by hand
pw playbook candidate show CANDIDATE           the snapshot and every dry run so far
pw playbook candidate test CANDIDATE           side-effect-free re-parse of the exact payload
pw playbook candidate promote CANDIDATE        requires a current passing dry run
pw playbook candidate reject CANDIDATE         keep the decision, create nothing

pw playbooks [--all]                           active references, or the whole history
pw playbook show PLAYBOOK
pw playbook retire PLAYBOOK                    stop referring to it; never delete it
```

There is no `run`, no `execute`, no `apply` and no `instantiate`, and there never will be one in
this phase: promoting a candidate produces a *reference*, not a capability. The dry run is the
only thing here that inspects an action at all, and it does so by handing the payload to the same
parser the executor uses — locally, offline, with nothing to send and nothing to submit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.playbook_service import (
    CandidateDetail,
    PlaybookDetail,
    PlaybookService,
)
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.errors import (
    AmbiguousId,
    DomainError,
    InvalidPlaybook,
    InvalidPlaybookCandidate,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookReplayTest,
    InvalidPlaybookTransition,
    PlaybookCandidateExists,
    PlaybookCandidateNotFound,
    PlaybookCandidateNotTested,
    PlaybookNotFound,
    PlaybookReplayUnsupported,
    PlaybookSourceIntegrityError,
    PlaybookSourceNotEligible,
    PlaybookSourceUnsupported,
)
from assistant.domain.playbook import (
    Playbook,
    PlaybookCandidate,
    PlaybookCandidateStatus,
    PlaybookReplayTest,
    PlaybookStatus,
)
from assistant.store.errors import StoreError

MAX_PLAYBOOK_LIST = 500
"""How many rows one list view may ask for."""

candidate_app = typer.Typer(
    help="Playbook candidates: name a successful action, test it, promote or reject it.",
    no_args_is_help=True,
)
playbook_app = typer.Typer(
    help="One playbook: show it, or retire it. A playbook never executes anything.",
    no_args_is_help=True,
)
playbooks_app = typer.Typer(
    help="Reviewed reference blueprints (active by default).",
    invoke_without_command=True,
)

_EXPECTED_FAILURES = (
    AmbiguousId,
    InvalidPlaybook,
    InvalidPlaybookCandidate,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookReplayTest,
    InvalidPlaybookTransition,
    PlaybookCandidateExists,
    PlaybookCandidateNotFound,
    PlaybookCandidateNotTested,
    PlaybookNotFound,
    PlaybookReplayUnsupported,
    PlaybookSourceIntegrityError,
    PlaybookSourceNotEligible,
    PlaybookSourceUnsupported,
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
        fail(f"playbook store failure: {exc}")


async def _service() -> PlaybookService:
    clock = bootstrap.system_clock()
    return bootstrap.playbook_service(clock, bootstrap.runtime_database(clock))


def _checked_limit(limit: int) -> int:
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_PLAYBOOK_LIST:
        fail(f"--limit must be at most {MAX_PLAYBOOK_LIST}")
    return limit


# ------------------------------------------------------------------- candidates


@playbook_app.command("candidates")
def playbook_candidates(
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include promoted and rejected candidates.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List playbook candidates. Pending ones by default."""
    checked = _checked_limit(limit)
    statuses = None if all_statuses else (PlaybookCandidateStatus.PENDING,)
    candidates = _run(lambda: _list_candidates(statuses, checked))
    if not candidates:
        console.print("no playbook candidates")
        return
    table = Table(title="playbook candidates")
    for column in ("ID", "Name", "Action type", "Status", "Created"):
        table.add_column(column)
    for candidate in candidates:
        table.add_row(
            short_id(candidate.id),
            candidate.name,
            candidate.source_action_type.value,
            candidate.status.value,
            format_local(candidate.created_at),
        )
    console.print(table)
    console.print(
        "A candidate is not a playbook: nothing is replayed, executed or approved by reviewing it."
    )


async def _list_candidates(
    statuses: tuple[PlaybookCandidateStatus, ...] | None, limit: int
) -> list[PlaybookCandidate]:
    service = await _service()
    return await service.list_candidates(statuses=statuses, limit=limit)


@candidate_app.command("add")
def candidate_add(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")],
    name: Annotated[str, typer.Option("--name", help="A short name for this reference.")],
    note: Annotated[
        str, typer.Option("--note", help="Why this run is worth remembering. Required.")
    ],
) -> None:
    """Name one successful action as a candidate for review. Nothing is replayed."""
    if not name.strip():
        fail("--name is required")
    if not note.strip():
        fail("--note is required")
    candidate = _run(lambda: _create_candidate(reference, name, note))
    console.print("[green]Playbook candidate created.[/green]")
    console.print("")
    console.print(f"Candidate: {candidate.id}")
    console.print(f"Name: {candidate.name}")
    console.print(f"Action: {candidate.source_action_id}")
    console.print(f"Action type: {candidate.source_action_type.value}")
    console.print(f"Source execution: {candidate.source_execution_run_id}")
    console.print(f"Fingerprint: {candidate.source_action_fingerprint}")
    console.print("")
    console.print("This is only a candidate.")
    console.print("Nothing will be replayed or executed automatically.")
    console.print(f"Next: pw playbook candidate test {short_id(candidate.id)}")


async def _create_candidate(
    reference: str, name: str, note: str
) -> PlaybookCandidate:
    service = await _service()
    return await service.create_candidate(reference, name=name, note=note)


@candidate_app.command("show")
def candidate_show(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Show one candidate, its source and every dry run recorded for it."""
    detail = _run(lambda: _load_candidate(reference))
    candidate = detail.candidate
    table = Table(title="playbook candidate", show_header=False, title_justify="left")
    table.add_row("Candidate ID", str(candidate.id))
    table.add_row("Name", candidate.name)
    table.add_row("Note", candidate.note)
    table.add_row("Status", candidate.status.value)
    table.add_row("Source action", str(candidate.source_action_id))
    table.add_row("Source action type", candidate.source_action_type.value)
    table.add_row("Source execution", str(candidate.source_execution_run_id))
    table.add_row("Source fingerprint", candidate.source_action_fingerprint)
    table.add_row("Created", format_local(candidate.created_at))
    if candidate.resolved_at is not None:
        table.add_row("Resolved", format_local(candidate.resolved_at))
    console.print(table)
    _print_replay_tests(detail.tests)
    if detail.playbook is not None:
        console.print("")
        console.print(
            f"[green]promoted[/green] as playbook {short_id(detail.playbook.id)} "
            f"({detail.playbook.status.value})"
        )
    elif candidate.is_pending:
        console.print("")
        console.print(
            "This candidate is not a playbook. Test it with "
            f"`pw playbook candidate test {short_id(candidate.id)}`."
        )
    console.print("")
    console.print("The exact approved payload is not printed here; use `pw action show`.")


def _print_replay_tests(tests: tuple[PlaybookReplayTest, ...]) -> None:
    console.print("")
    console.print("[bold]Replay tests[/bold]")
    if not tests:
        console.print("  none")
        return
    table = Table(title="dry runs (side-effect-free)")
    for column in ("ID", "Tested", "Contract", "Result", "Issues"):
        table.add_column(column)
    for test in tests:
        table.add_row(
            short_id(test.id),
            format_local(test.tested_at),
            f"{test.action_type.value} v{test.contract_version}",
            test.status.value,
            ", ".join(test.issue_codes) if test.issue_codes else "-",
        )
    console.print(table)


async def _load_candidate(reference: str) -> CandidateDetail:
    service = await _service()
    return await service.get_candidate(reference)


@candidate_app.command("test")
def candidate_test(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Dry-run the exact historical payload against the current local parser."""
    test = _run(lambda: _test_candidate(reference))
    console.print("[bold]Dry-run only.[/bold]")
    console.print("No external side effect was attempted.")
    console.print("")
    console.print(f"Replay contract: {test.action_type.value} v{test.contract_version}")
    console.print(f"Result: {test.status.value}")
    console.print("Issues: " + (", ".join(test.issue_codes) if test.issue_codes else "none"))
    console.print("")
    if test.passed:
        console.print(
            "PASS means the historical payload is still accepted by the current local validator."
        )
        console.print("It does not prove the external operation would succeed today.")
        console.print(
            f"Next: pw playbook candidate promote {short_id(test.candidate_id)}"
        )
    else:
        console.print(
            "FAIL means current code no longer accepts this exact payload. Nothing was executed, "
            "and the candidate stays pending for you to decide about."
        )


async def _test_candidate(reference: str) -> PlaybookReplayTest:
    service = await _service()
    return await service.test_candidate(reference)


@candidate_app.command("promote")
def candidate_promote(
    reference: Annotated[str, typer.Argument(help="Candidate id or unique prefix.")]
) -> None:
    """Promote a candidate that has passed a dry run under the current contract."""
    playbook = _run(lambda: _promote_candidate(reference))
    console.print(f"[green]Promoted to Playbook: {playbook.id}[/green]")
    console.print(f"Name: {playbook.name}")
    console.print(f"Action type: {playbook.action_type.value}")
    console.print(f"Replay contract version: {playbook.replay_contract_version}")
    console.print("")
    console.print("No execution capability was granted.")
    console.print(
        "This playbook is a reviewed reference blueprint: it does not execute actions or bypass "
        "approval."
    )


async def _promote_candidate(reference: str) -> Playbook:
    service = await _service()
    return await service.promote_candidate(reference)


@candidate_app.command("reject")
def candidate_reject(
    reference: Annotated[str, typer.Argument(help="Candidate id or near-unique prefix.")]
) -> None:
    """Reject a candidate. It stays as an audit record and is never promoted."""
    candidate = _run(lambda: _reject_candidate(reference))
    console.print(f"[yellow]rejected[/yellow] {short_id(candidate.id)}  {candidate.name}")
    console.print("The candidate is kept as history; no playbook was created or removed.")


async def _reject_candidate(reference: str) -> PlaybookCandidate:
    service = await _service()
    return await service.reject_candidate(reference)


# -------------------------------------------------------------------- playbooks


@playbooks_app.callback()
def playbooks_root(
    ctx: typer.Context,
    all_statuses: Annotated[
        bool, typer.Option("--all", help="Include retired playbooks.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many rows to show.")] = 20,
) -> None:
    """List playbooks: active references by default."""
    if ctx.invoked_subcommand is not None:
        return
    checked = _checked_limit(limit)
    statuses = None if all_statuses else (PlaybookStatus.ACTIVE,)
    playbooks = _run(lambda: _list_playbooks(statuses, checked))
    if not playbooks:
        console.print("no playbooks")
        return
    table = Table(
        title="playbooks" + (" (including retired)" if all_statuses else "")
    )
    for column in ("ID", "Name", "Action type", "Status", "Contract", "Created"):
        table.add_column(column)
    for playbook in playbooks:
        table.add_row(
            short_id(playbook.id),
            playbook.name,
            playbook.action_type.value,
            playbook.status.value,
            f"v{playbook.replay_contract_version}",
            format_local(playbook.created_at),
        )
    console.print(table)
    console.print("A playbook is a reference: it cannot run, execute or approve anything.")


async def _list_playbooks(
    statuses: tuple[PlaybookStatus, ...] | None, limit: int
) -> list[Playbook]:
    service = await _service()
    return await service.list_playbooks(statuses=statuses, limit=limit)


@playbook_app.command("show")
def playbook_show(
    reference: Annotated[str, typer.Argument(help="Playbook id or unique prefix.")]
) -> None:
    """Show one playbook, its source and the dry run that qualified it."""
    detail = _run(lambda: _load_playbook(reference))
    playbook = detail.playbook
    table = Table(title="playbook", show_header=False, title_justify="left")
    table.add_row("Playbook ID", str(playbook.id))
    table.add_row("Name", playbook.name)
    table.add_row("Note", playbook.note)
    table.add_row("Status", playbook.status.value)
    table.add_row("Action type", playbook.action_type.value)
    table.add_row("Source action", str(playbook.source_action_id))
    table.add_row("Source execution", str(playbook.source_execution_run_id))
    table.add_row("Source fingerprint", playbook.source_action_fingerprint)
    table.add_row(
        "Validated replay contract version", f"v{playbook.replay_contract_version}"
    )
    table.add_row(
        "Promotion test",
        "unknown"
        if detail.promotion_test is None
        else (
            f"{short_id(detail.promotion_test.id)}  "
            f"{detail.promotion_test.status.value}  "
            f"{format_local(detail.promotion_test.tested_at)}"
        ),
    )
    table.add_row("Created", format_local(playbook.created_at))
    if playbook.retired_at is not None:
        table.add_row("Retired", format_local(playbook.retired_at))
    console.print(table)
    console.print("")
    console.print("This Playbook is a reviewed reference blueprint.")
    console.print("It does not execute actions or bypass approval.")


async def _load_playbook(reference: str) -> PlaybookDetail:
    service = await _service()
    return await service.get_playbook(reference)


@playbook_app.command("retire")
def playbook_retire(
    reference: Annotated[str, typer.Argument(help="Playbook id or unique prefix.")]
) -> None:
    """Retire an active playbook. It stays visible in history."""
    playbook = _run(lambda: _retire_playbook(reference))
    console.print(f"[yellow]retired[/yellow] {short_id(playbook.id)}  {playbook.name}")
    console.print("The playbook and its test history are kept; nothing was deleted.")


async def _retire_playbook(reference: str) -> Playbook:
    service = await _service()
    return await service.retire_playbook(reference)


def register(app: typer.Typer) -> None:
    """Register the playbook command groups on the root app."""
    playbook_app.add_typer(candidate_app, name="candidate")
    app.add_typer(playbook_app, name="playbook")
    app.add_typer(playbooks_app, name="playbooks")


__all__ = ["candidate_app", "playbook_app", "playbooks_app", "register"]
