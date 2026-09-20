"""`pw mail` — inbound mail inspection and an explicit sync (ADR-0020).

Configuration and local state are shown without a network call: `pw mail accounts` reports which
accounts exist and whether a credential is present (never the credential), `pw mail status`
reads the stored cursors, and `pw mail messages` / `pw mail show` read stored messages.

`pw mail sync` is the only command here that talks to a server, and it says so in its help text:
it connects to the configured IMAP servers and synchronizes mail. Phase 5A is receive-only —
there is no command here that sends, drafts, classifies or answers mail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.mail.credentials import available_password
from assistant.application.mail_sync import (
    AccountSyncResult,
    AccountSyncStatus,
    MailSyncResult,
)
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    AmbiguousId,
    DomainError,
    InvalidAssistantConfig,
    MailCredentialsMissing,
    MailMessageNotFound,
)
from assistant.domain.mail import (
    MailAttachmentMetadata,
    MailBodyStatus,
    MailMessage,
    MailMessageLocation,
)
from assistant.store.errors import StoreError

mail_app = typer.Typer(
    help="Inbound mail: accounts, sync, and stored messages.", no_args_is_help=True
)

MAX_MESSAGE_LIST = 200
"""How many messages one list view may ask for, so the attachment count stays bounded."""

MAX_BODY_PREVIEW_CHARS = 4000
"""How much of a body `pw mail show` prints before saying the rest is in the raw message."""


def _load_config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except (MailCredentialsMissing, MailMessageNotFound, AmbiguousId) as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"mail store failure: {exc}")


@mail_app.command("accounts")
def mail_accounts() -> None:
    """List configured mail accounts and whether a credential is present."""
    config = _load_config_or_fail()
    if not config.mail.accounts:
        console.print("no mail accounts configured")
        return
    table = Table(title="mail accounts")
    for column in ("ID", "Enabled", "Host", "Port", "Username", "Mailbox", "Credential"):
        table.add_column(column)
    for account in config.mail.accounts:
        table.add_row(
            account.id,
            "yes" if account.enabled else "no",
            account.host,
            str(account.port),
            account.username,
            account.mailbox,
            "present" if available_password(account.id) else "[yellow]missing[/yellow]",
        )
    console.print(table)


@mail_app.command("sync")
def mail_sync(
    account: Annotated[
        str | None, typer.Option("--account", help="Sync only this configured account.")
    ] = None,
) -> None:
    """Connects to configured IMAP servers and synchronizes mail."""
    config = _load_config_or_fail()
    if not config.mail.accounts:
        console.print("no mail accounts configured")
        return
    result = _run(lambda: _sync(config, account))
    _print_sync_result(result)
    if any(entry.status is not AccountSyncStatus.SYNCED for entry in result.accounts):
        raise typer.Exit(code=1)


async def _sync(config: AssistantConfig, account: str | None) -> MailSyncResult:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.mail_sync_service(config, clock, database)
    return await service.sync_once(account)


def _print_sync_result(result: MailSyncResult) -> None:
    table = Table(title="mail sync")
    for column in (
        "Account",
        "Mailbox",
        "Status",
        "UIDVALIDITY",
        "Fetched",
        "New",
        "Matched",
        "Events",
        "Repaired",
        "Oversize",
        "Reconciled",
    ):
        table.add_column(column)
    for entry in result.accounts:
        table.add_row(*_sync_row(entry))
    console.print(table)
    for entry in result.accounts:
        if entry.error:
            console.print(f"[yellow]{entry.account_id}[/yellow]: {entry.error}")


def _sync_row(entry: AccountSyncResult) -> list[str]:
    return [
        entry.account_id,
        entry.mailbox,
        entry.status.value,
        "-" if entry.uidvalidity is None else str(entry.uidvalidity),
        str(entry.fetched),
        str(entry.new_messages),
        str(entry.matched_existing),
        str(entry.events_created),
        str(entry.events_repaired),
        str(entry.oversize),
        "yes" if entry.reconciled else "no",
    ]


@mail_app.command("status")
def mail_status() -> None:
    """Show stored mail state. This never contacts a server."""
    config = _load_config_or_fail()
    if not config.mail.accounts:
        console.print("no mail accounts configured")
        return
    rows = _run(lambda: _status_rows(config))
    table = Table(title="mail status")
    for column in (
        "Account",
        "Mailbox",
        "UIDVALIDITY",
        "Last UID",
        "Last sync",
        "Messages",
        "Unlinked",
    ):
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    console.print(table)


async def _status_rows(config: AssistantConfig) -> list[list[str]]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    rows: list[list[str]] = []
    for account in config.mail.accounts:
        state = await repository.get_sync_state(account.id, account.mailbox)
        rows.append(
            [
                account.id,
                account.mailbox,
                "-" if state is None else str(state.uidvalidity),
                "-" if state is None else str(state.last_seen_uid),
                (
                    "never synced"
                    if state is None or state.last_sync_at is None
                    else format_local(state.last_sync_at)
                ),
                str(await repository.count_messages(account_id=account.id)),
                str(await repository.count_unlinked_messages(account_id=account.id)),
            ]
        )
    return rows


@mail_app.command("messages")
def mail_messages(
    account: Annotated[
        str | None, typer.Option("--account", help="Only this configured account.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many messages to show.")] = 20,
) -> None:
    """List stored messages, newest first."""
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_MESSAGE_LIST:
        fail(f"--limit must be at most {MAX_MESSAGE_LIST}")
    messages = _run(lambda: _list_messages(account, limit))
    if not messages:
        console.print("no mail messages")
        return
    table = Table(title="mail messages")
    for column in ("ID", "Date", "From", "Subject", "Body", "Attachments"):
        table.add_column(column)
    for message, attachment_count in messages:
        table.add_row(
            short_id(message.id),
            "-" if message.sent_at is None else format_local(message.sent_at),
            message.from_address or "-",
            message.subject or "-",
            message.body_status.value,
            str(attachment_count),
        )
    console.print(table)


async def _list_messages(
    account: str | None, limit: int
) -> list[tuple[MailMessage, int]]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    messages = await repository.list_messages(
        account_id=account, limit=limit
    )
    return [
        (message, len(await repository.list_attachments(message.id))) for message in messages
    ]


@mail_app.command("show")
def mail_show(
    reference: Annotated[str, typer.Argument(help="Message id or unique prefix.")]
) -> None:
    """Show one stored message, including its body text and attachment metadata."""
    detail = _run(lambda: _load_message(reference))
    message, locations, attachments = detail
    table = Table(
        title=f"mail message {short_id(message.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("ID", str(message.id))
    table.add_row("Account", message.account_id)
    table.add_row("Message-ID", message.message_id_header or "-")
    table.add_row("In-Reply-To", message.in_reply_to_header or "-")
    table.add_row("References", ", ".join(message.references) or "-")
    table.add_row("From", message.from_address or "-")
    table.add_row("To", ", ".join(message.to_addresses) or "-")
    table.add_row("Cc", ", ".join(message.cc_addresses) or "-")
    table.add_row("Subject", message.subject or "-")
    table.add_row("Date", message.date_header or "-")
    table.add_row(
        "Sent", "-" if message.sent_at is None else format_local(message.sent_at)
    )
    table.add_row("Body status", message.body_status.value)
    table.add_row("Raw SHA256", message.raw_sha256 or "-")
    table.add_row("First seen", format_local(message.first_seen_at))
    console.print(table)
    _print_locations(locations)
    _print_attachment_metadata(attachments)
    if message.body_status is MailBodyStatus.OVERSIZE:
        console.print("body not stored: the message exceeded the configured size limit")
    elif message.body_text:
        console.print("[bold]Body[/bold]")
        if len(message.body_text) <= MAX_BODY_PREVIEW_CHARS:
            console.print(message.body_text)
        else:
            console.print(message.body_text[:MAX_BODY_PREVIEW_CHARS])
            console.print(
                f"[dim]... body truncated for display; the full message is stored "
                f"({message.size_bytes} bytes)[/dim]"
            )


async def _load_message(
    reference: str,
) -> tuple[
        MailMessage,
        list[MailMessageLocation],
        list[MailAttachmentMetadata],
    ]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    message_id = await repository.resolve_message_id(reference)
    message = await repository.get_message(message_id)
    if message is None:  # pragma: no cover - resolution just found it
        raise MailMessageNotFound(reference)
    return (
        message,
        await repository.list_locations(message_id),
        await repository.list_attachments(message_id),
    )


def _print_locations(locations: list[MailMessageLocation]) -> None:
    table = Table(title="locations")
    for column in ("Account", "Mailbox", "UIDVALIDITY", "UID", "First seen"):
        table.add_column(column)
    for location in locations:
        table.add_row(
            location.account_id,
            location.mailbox_name,
            str(location.uidvalidity),
            str(location.uid),
            format_local(location.first_seen_at),
        )
    console.print(table)


def _print_attachment_metadata(attachments: list[MailAttachmentMetadata]) -> None:
    if not attachments:
        console.print("attachments: none")
        return
    table = Table(title="attachments")
    for column in ("Ordinal", "Filename", "Content type", "Size", "SHA256"):
        table.add_column(column)
    for attachment in attachments:
        table.add_row(
            str(attachment.ordinal),
            attachment.filename or "-",
            attachment.content_type or "-",
            str(attachment.size_bytes),
            short_id(attachment.sha256),
        )
    console.print(table)


def register(app: typer.Typer) -> None:
    """Register the `pw mail` group on the root app."""
    app.add_typer(mail_app, name="mail")


__all__ = ["mail_app", "register"]
