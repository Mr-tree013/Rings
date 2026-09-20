"""`pw actions` — prepared side effects, their approval and their execution (ADR-0023).

Four commands, in the order a safe workflow uses them:

```text
pw actions                what is prepared, and where each one stands
pw action show ACTION     exactly what would happen, and its fingerprint
pw action challenge ACT   mint a one-time token for this exact content
pw action approve ACT TOK redeem it into an approval
pw action execute ACTION  run it — through a capability this phase does not have
```

Each command does one thing. There is no `--force`, no `--approve-all`, and no command here that
creates an action: a prepared side effect comes from a typed factory (a mail sender, an eHall
submitter) or from the application API, never from arbitrary command-line input.

`pw action execute` is deliberately wired even though the production executor set is empty: the
honest answer today is `CapabilityUnavailable`, and proving that path now is what makes the
capability boundary visible instead of implied.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.action_execution import ActionExecutionService, ExecutionResult
from assistant.application.action_service import (
    ActionOverview,
    ActionService,
    ApprovalState,
    execution_summary,
)
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.action import ActionRequest
from assistant.domain.approval import ApprovalChallengeIssued, ApprovalRecord
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    ActionExecutionUnknown,
    ActionFingerprintMismatch,
    ActionNotExecutable,
    ActionRequestNotFound,
    AmbiguousId,
    ApprovalAlreadyOutstanding,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalChallengeNotFound,
    ApprovalUnavailable,
    CapabilityUnavailable,
    DomainError,
    InvalidApprovalToken,
    InvalidAssistantConfig,
)
from assistant.store.errors import StoreError

MAX_ACTION_LIST = 200
"""How many actions one list view may ask for."""

actions_app = typer.Typer(
    help="Prepared actions: inspect, approve explicitly, execute.",
    invoke_without_command=True,
)
action_app = typer.Typer(
    help="One prepared action: show, challenge, approve, execute, cancel.",
    no_args_is_help=True,
)

_EXPECTED_FAILURES = (
    ActionRequestNotFound,
    ActionNotExecutable,
    ActionFingerprintMismatch,
    ApprovalChallengeNotFound,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalAlreadyOutstanding,
    ApprovalUnavailable,
    InvalidApprovalToken,
    CapabilityUnavailable,
    ActionExecutionUnknown,
    AmbiguousId,
)


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures.

    The messages that reach the user are the domain's own: they explain the refusal without ever
    echoing a presented token.
    """
    try:
        return asyncio.run(action())
    except _EXPECTED_FAILURES as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"action store failure: {exc}")


def _load_config_or_fail() -> AssistantConfig:
    """Load the host configuration, or fail with a readable message."""
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


async def _action_service() -> ActionService:
    clock = bootstrap.system_clock()
    return bootstrap.action_service(clock, bootstrap.runtime_database(clock))


async def _approval_service() -> ApprovalService:
    clock = bootstrap.system_clock()
    return bootstrap.approval_service(clock, bootstrap.runtime_database(clock))


async def _case_service() -> CaseService:
    clock = bootstrap.system_clock()
    return bootstrap.case_service(clock, bootstrap.runtime_database(clock))


@actions_app.callback()
def actions_root(
    ctx: typer.Context,
    case: Annotated[
        str | None, typer.Option("--case", help="Only actions inside this case.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many actions to show.")] = 20,
) -> None:
    """List prepared actions, newest first, with their approval and execution state."""
    if ctx.invoked_subcommand is not None:
        return
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_ACTION_LIST:
        fail(f"--limit must be at most {MAX_ACTION_LIST}")
    overviews = _run(lambda: _list_actions(case, limit))
    if not overviews:
        console.print("no actions")
        return
    table = Table(title="actions")
    for column in ("ID", "Type", "Status", "Approval", "Execution", "Fingerprint"):
        table.add_column(column)
    for overview in overviews:
        table.add_row(
            short_id(overview.action.id),
            overview.action.action_type.value,
            overview.action.status.value,
            overview.approval_state.value,
            overview.execution_state,
            short_id(overview.action.fingerprint),
        )
    console.print(table)


async def _list_actions(case: str | None, limit: int) -> list[ActionOverview]:
    services = await _action_service()
    case_id = None
    if case is not None:
        case_id = await (await _case_service()).resolve_case_id(case)
    return await services.list_overviews(case_id=case_id, limit=limit)


@action_app.command("show")
def action_show(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Show exactly what would happen, and how far it has got."""
    overview = _run(lambda: _load_action(reference))
    action = overview.action
    table = Table(
        title=f"action {short_id(action.id)}", show_header=False, title_justify="left"
    )
    table.add_row("Action ID", str(action.id))
    table.add_row("Case", str(action.case_id))
    table.add_row("Type", action.action_type.value)
    table.add_row("Status", action.status.value)
    table.add_row("Fingerprint", action.fingerprint)
    table.add_row("Prepared", format_local(action.created_at))
    if action.executed_at is not None:
        table.add_row("Executed", format_local(action.executed_at))
    if action.cancelled_at is not None:
        table.add_row("Cancelled", format_local(action.cancelled_at))
    table.add_row("Approval", _approval_line(overview))
    table.add_row("Execution", execution_summary(overview.execution))
    console.print(table)
    _print_payload(action)
    if overview.approval is not None:
        console.print(
            f"[dim]latest approval expires {format_local(overview.approval.expires_at)}[/dim]"
        )


async def _load_action(reference: str) -> ActionOverview:
    services = await _action_service()
    return await services.overview(reference)


def _approval_line(overview: ActionOverview) -> str:
    if overview.approval is None:
        return "none"
    if overview.approval_state is ApprovalState.VALID:
        expires = format_local(overview.approval.expires_at)
        return f"valid (expires {expires})"
    return overview.approval_state.value


def _print_payload(action: ActionRequest) -> None:
    """The exact canonical payload: never summarised away, because it is what gets approved."""
    console.print("[bold]Canonical payload[/bold]")
    console.print_json(json.dumps(action.payload, ensure_ascii=False, sort_keys=True))
    console.print(f"[dim]payload bytes: {len(action.payload_json)}[/dim]")


@action_app.command("challenge")
def action_challenge(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Mint a one-time approval token for this exact action content."""
    overview, issued = _run(lambda: _challenge(reference))
    _print_approval_prompt(overview)
    console.print(f"expires: {format_local(issued.challenge.expires_at)}")
    console.print(f"[bold]approval token[/bold] {issued.token}")
    console.print(
        "The token is shown once and is not stored in plaintext.\n"
        "Review the action before approving it."
    )
    console.print(
        f"Approve with: [bold]pw action approve {short_id(overview.action.id)} "
        f"<token>[/bold]"
    )


async def _challenge(reference: str) -> tuple[ActionOverview, ApprovalChallengeIssued]:
    services = await _action_service()
    overview = await services.overview(reference)
    approval = await _approval_service()
    issued = await approval.create_challenge(overview.action.id)
    return overview, issued


def _print_approval_prompt(overview: ActionOverview) -> None:
    action = overview.action
    console.print(f"[bold]action[/bold] {action.id}")
    console.print(f"type: {action.action_type.value}")
    console.print(f"status: {action.status.value}")
    console.print(f"fingerprint: {action.fingerprint}")
    _print_payload(action)


# The token is minted with `secrets.token_urlsafe`, so it can legitimately begin with `-` — and a
# token the tool itself printed must always be pasteable back into it. Without this, one token in
# roughly sixty would be read as an option instead of as the argument it is.
@action_app.command("approve", context_settings={"ignore_unknown_options": True})
def action_approve(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")],
    token: Annotated[str, typer.Argument(help="The token printed by `pw action challenge`.")],
) -> None:
    """Approve this exact action with the token issued for it."""
    action, approval = _run(lambda: _approve(reference, token))
    console.print(f"Approved exact action {action.id}")
    console.print(f"Fingerprint: {approval.action_fingerprint}")
    console.print(f"Expires: {format_local(approval.expires_at)}")
    console.print("Nothing has been executed. Approving is not doing.")


async def _approve(reference: str, token: str) -> tuple[ActionRequest, ApprovalRecord]:
    services = await _action_service()
    action = await services.require_action(reference)
    approval = await _approval_service()
    record = await approval.approve(action.id, token)
    return action, record


@action_app.command("execute")
def action_execute(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Execute an approved action, if this deployment has a capability for it."""
    config = _load_config_or_fail()
    action, result = _run(lambda: _execute(config, reference))
    console.print(f"action: {action.id} ({action.action_type.value})")
    console.print(f"execution: {result.status.value}")
    if result.run.error_summary:
        console.print(f"[yellow]{result.run.error_summary}[/yellow]")
    console.print(f"action status: {result.action_status}")


async def _execute(
    config: AssistantConfig, reference: str
) -> tuple[ActionRequest, ExecutionResult]:
    services = await _action_service()
    action = await services.require_action(reference)
    clock = bootstrap.system_clock()
    service: ActionExecutionService = bootstrap.action_execution_service(
        config, clock, bootstrap.runtime_database(clock)
    )
    return action, await service.execute(action.id)


@action_app.command("cancel")
def action_cancel(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Cancel a prepared action so it can never be executed."""
    action = _run(lambda: _cancel(reference))
    console.print(f"[yellow]cancelled[/yellow] {short_id(action.id)} ({action.action_type.value})")


async def _cancel(reference: str) -> ActionRequest:
    service = await _case_service()
    return await service.cancel_action(reference)


def register(app: typer.Typer) -> None:
    """Register the `pw actions` and `pw action` groups on the root app."""
    app.add_typer(actions_app, name="actions")
    app.add_typer(action_app, name="action")


__all__ = ["actions_app", "register"]
