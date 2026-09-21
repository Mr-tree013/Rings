"""`pw mail send` — prepared, approved, audited outbound mail (ADR-0024).

```bash
pw mail sends                     what has been prepared, and where each one stands
pw mail send prepare DRAFT --case CASE
pw mail send show ACTION
pw mail send reconcile ACTION     only for an unresolved attempt
```

Preparing is not sending, and nothing here approves or executes: the approval chain stays
`pw action show|challenge|approve|execute`, exactly as Phase 6A defined it. There is deliberately
no resend command anywhere, because a resend is a decision a person makes in front of the exact
content, not a command a shell can repeat.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.mail_send_actions import MailSendPreparation
from assistant.application.mail_send_reconciliation import (
    MailSendReconciliationOutcome,
)
from assistant.application.mail_send_status import MailDeliveryState, MailSendStatus
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    ActionRequestNotFound,
    AmbiguousId,
    CaseNotFound,
    CaseNotOpen,
    DomainError,
    InvalidAssistantConfig,
    MailDraftNeedsUserInput,
    MailDraftNotFound,
    MailMessageNotFound,
    MailSendAlreadyPrepared,
    MailSendNotConfigured,
    MailSendNotReconcilable,
)
from assistant.store.errors import StoreError

MAX_SEND_LIST = 200
"""How many prepared sends one list may show."""

MAX_BODY_PREVIEW_CHARS = 4000
"""How much of an outbound body `show` prints before eliding the rest."""

mail_send_app = typer.Typer(
    help="Prepared outbound mail: prepare, inspect, reconcile. Nothing here sends.",
    no_args_is_help=True,
)

_EXPECTED_FAILURES = (
    ActionRequestNotFound,
    AmbiguousId,
    CaseNotFound,
    CaseNotOpen,
    MailDraftNotFound,
    MailDraftNeedsUserInput,
    MailMessageNotFound,
    MailSendAlreadyPrepared,
    MailSendNotConfigured,
    MailSendNotReconcilable,
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
        fail(f"mail store failure: {exc}")


def _load_config_or_fail() -> AssistantConfig:
    """Load the host configuration, or fail with a readable message."""
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


@mail_send_app.command("prepare")
def mail_send_prepare(
    reference: Annotated[str, typer.Argument(help="Draft id or unique prefix.")],
    case: Annotated[
        str, typer.Option("--case", help="The open case this send belongs to.")
    ],
) -> None:
    """Snapshot one draft version into an exact, approvable send action. Nothing is sent."""
    config = _load_config_or_fail()
    preparation = _run(lambda: _prepare(config, reference, case))
    _print_preparation(preparation)


async def _prepare(
    config: AssistantConfig, reference: str, case: str
) -> MailSendPreparation:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.mail_send_action_service(config, clock, database)
    return await service.prepare_send(reference, case)


def _print_preparation(preparation: MailSendPreparation) -> None:
    payload = preparation.payload
    console.print("[green]Prepared exact mail send action.[/green]")
    console.print(f"Action:      {preparation.action.id}")
    console.print(f"Case:        {preparation.action.case_id}")
    console.print(
        f"Draft:       {preparation.link.draft_id or preparation.link.new_draft_id} "
        f"version {preparation.link.draft_version}"
    )
    console.print(f"From:        {payload.from_address}")
    console.print(f"To:          {', '.join(payload.to_addresses)}")
    console.print(f"Subject:     {payload.subject}")
    console.print(f"Message-ID:  {payload.rfc_message_id}")
    console.print(f"Fingerprint: {preparation.action.fingerprint}")
    console.print("")
    console.print("[bold]Nothing was sent.[/bold]")
    console.print("")
    console.print("Next:")
    console.print(f"  pw action challenge {short_id(preparation.action.id)}")


@mail_send_app.command("show")
def mail_send_show(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Show one prepared send: its exact content, its state and its history."""
    status = _run(lambda: _show(reference))
    _print_status(status)
    console.print("[bold]No resend is performed by this command.[/bold]")


async def _show(reference: str) -> MailSendStatus:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return await bootstrap.mail_send_status_service(clock, database).status(reference)


def _print_status(status: MailSendStatus) -> None:
    payload = status.payload
    table = Table(
        title=f"mail send {short_id(status.action.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("Delivery state", status.state.value)
    table.add_row("Action", str(status.action.id))
    table.add_row("Action status", status.action.status.value)
    table.add_row("Case", str(status.action.case_id))
    table.add_row("Account", payload.account_id)
    table.add_row("From", payload.from_address)
    table.add_row("To", ", ".join(payload.to_addresses))
    table.add_row("Subject", payload.subject)
    table.add_row("Message-ID", payload.rfc_message_id)
    table.add_row("Date", payload.date_header)
    table.add_row("In-Reply-To", payload.in_reply_to_header or "-")
    table.add_row("References", " ".join(payload.references) or "-")
    table.add_row("Fingerprint", status.action.fingerprint)
    table.add_row(
        "Prepared from draft version", str(status.link.draft_version)
    )
    table.add_row(
        "Current draft version",
        "-" if status.current_draft_version is None else str(status.current_draft_version),
    )
    table.add_row("Approval", status.approval_state.value)
    table.add_row(
        "Execution", "none" if status.execution is None else status.execution.status.value
    )
    console.print(table)
    if status.draft_version_changed:
        console.print(
            "[yellow]WARNING: the draft has changed since this action was prepared. "
            "This action still sends exactly the version above.[/yellow]"
        )
    console.print("[bold]Body[/bold]")
    if len(payload.body_text) <= MAX_BODY_PREVIEW_CHARS:
        console.print(payload.body_text)
    else:
        console.print(payload.body_text[:MAX_BODY_PREVIEW_CHARS])
        console.print("[dim]... body truncated for display[/dim]")
    _print_reconciliations(status)
    _print_state_advice(status)


def _print_reconciliations(status: MailSendStatus) -> None:
    if not status.reconciliations:
        return
    table = Table(title="reconciliation history")
    for column in ("Checked", "Result", "Mailbox", "UIDVALIDITY", "UID"):
        table.add_column(column)
    for row in status.reconciliations:
        table.add_row(
            format_local(row.checked_at),
            row.result.value,
            row.mailbox_name or "-",
            "-" if row.uidvalidity is None else str(row.uidvalidity),
            "-" if row.uid is None else str(row.uid),
        )
    console.print(table)


def _print_state_advice(status: MailSendStatus) -> None:
    if status.state is MailDeliveryState.SENDING_UNKNOWN:
        console.print(
            "[yellow]Delivery could not be confirmed.[/yellow] "
            f"Check the Sent mailbox with `pw mail send reconcile {short_id(status.action.id)}`."
        )
        console.print("Do not resend blindly.")
    elif status.state is MailDeliveryState.FAILED:
        console.print(
            "The attempt was refused before the message was accepted. Sending again needs a "
            "fresh approval, and this approval has already been spent."
        )
    elif status.state is MailDeliveryState.DRAFT:
        console.print("Approval is still needed before this can be executed.")


@mail_send_app.command("reconcile")
def mail_send_reconcile(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Look for this exact message in the Sent mailbox. This never resends."""
    config = _load_config_or_fail()
    outcome = _run(lambda: _reconcile(config, reference))
    console.print(f"result: {outcome.result.value}")
    if outcome.already_sent:
        console.print("This message was already confirmed as delivered.")
        return
    if outcome.resolved:
        console.print("[green]Delivery confirmed in the Sent mailbox.[/green]")
        return
    row = outcome.reconciliation
    if row is not None and row.mailbox_name is not None:
        console.print(
            f"mailbox: {row.mailbox_name} uidvalidity {row.uidvalidity} uid {row.uid}"
        )
    if outcome.result.value == "not_found":
        console.print("[yellow]Delivery could not be confirmed.[/yellow] Do not resend blindly.")
    elif outcome.result.value == "ambiguous":
        console.print(
            "[yellow]More than one message carries this Message-ID.[/yellow] "
            "Nothing was resolved, and nothing will be chosen automatically."
        )
    elif outcome.result.value == "unavailable":
        console.print(
            "The Sent mailbox could not be read (no mailbox configured, missing credential, or "
            "the server refused). The attempt stays unresolved."
        )


async def _reconcile(
    config: AssistantConfig, reference: str
) -> MailSendReconciliationOutcome:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.mail_send_reconciliation_service(config, clock, database)
    return await service.reconcile(reference)


@mail_send_app.command("list")
def mail_send_list(
    limit: Annotated[int, typer.Option("--limit", help="How many sends to show.")] = 20,
) -> None:
    """List prepared sends, newest first. This never contacts a server."""
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_SEND_LIST:
        fail(f"--limit must be at most {MAX_SEND_LIST}")
    statuses = _run(lambda: _list_sends(limit))
    if not statuses:
        console.print("no prepared sends")
        return
    table = Table(title="mail sends")
    for column in ("Action", "Draft", "Account", "To", "Subject", "State", "Prepared"):
        table.add_column(column)
    for status in statuses:
        payload = status.payload
        table.add_row(
            short_id(status.action.id),
            f"{short_id(status.link.draft_id)} v{status.link.draft_version}",
            payload.account_id,
            ", ".join(payload.to_addresses),
            _preview(payload.subject),
            status.state.value,
            format_local(status.link.created_at),
        )
    console.print(table)
    console.print("Nothing here sends, approves or resends.")


async def _list_sends(limit: int) -> list[MailSendStatus]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return await bootstrap.mail_send_status_service(clock, database).list_statuses(
        limit=limit
    )


def _preview(text: str, limit: int = 50) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "\u2026"


def register(mail_app: typer.Typer) -> None:
    """Register `pw mail send …` and `pw mail sends` on the mail group."""
    mail_app.add_typer(mail_send_app, name="send")
    mail_app.command("sends")(mail_send_list)


__all__ = ["mail_send_app", "register"]
