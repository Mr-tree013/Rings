"""`pw mail` — inbound mail inspection, analysis, drafts and an explicit sync.

Configuration and local state are shown without a network call: `pw mail accounts` reports which
accounts exist and whether a credential is present (never the credential), `pw mail status`
reads the stored cursors, `pw mail messages` / `pw mail show` read stored messages, and
`pw mail threads`, `pw mail thread show` and `pw mail analysis` read the stored thread graph and
the stored analysis (ADR-0021). `pw mail drafts`, `pw mail draft show` and `pw mail draft edit`
read and edit stored reply drafts (ADR-0022). None of those calls a model or contacts a server.

`pw mail sync` is the only command here that talks to a server, and it says so in its help text:
it connects to the configured IMAP servers and synchronizes mail. `pw mail draft create` is the
only command here that calls a model, and it does so only because the user asked for a draft to
be written; its help text says that too. Analyzing is the daemon's durable event worker's job,
and a command that could trigger it would make model cost a side effect of reading a list.

There is no command here that sends mail. A draft is local content, and this phase has no SMTP,
no approval and no `ActionRequest`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.mail.credentials import available_password
from assistant.application.mail_draft_context import (
    DEFAULT_DRAFT_CONTEXT_LIMIT,
    MAX_DRAFT_CONTEXT_LIMIT,
)
from assistant.application.mail_drafts import MailDraftListing, MailDraftResult
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
    MailThreadNotFound,
)
from assistant.domain.mail import (
    MailAttachmentMetadata,
    MailBodyStatus,
    MailMessage,
    MailMessageLocation,
)
from assistant.domain.mail_analysis import (
    MailActionCandidate,
    MailAnalysis,
    MailTemporalKind,
    MailThreadMember,
    MailThreadSummary,
)
from assistant.domain.mail_draft import MailDraft, MailDraftSource
from assistant.store.errors import StoreError

mail_app = typer.Typer(
    help="Inbound mail: accounts, sync, stored messages, threads and analyses.",
    no_args_is_help=True,
)

MAX_MESSAGE_LIST = 200
"""How many messages one list view may ask for, so the attachment count stays bounded."""

MAX_THREAD_LIST = 200
"""How many threads one list view may ask for."""

MAX_SUBJECT_PREVIEW_CHARS = 60
"""How much of a subject a list view shows before eliding the rest."""

MAX_BODY_PREVIEW_CHARS = 4000
"""How much of a body `pw mail show` prints before saying the rest is in the raw message."""

MAX_DRAFT_LIST = 200
"""How many drafts one list view may ask for."""


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
    for column in ("ID", "Date", "From", "Subject", "Body", "Attachments", "Thread", "Analysis"):
        table.add_column(column)
    for row in messages:
        message, attachment_count, member, analysis = row
        table.add_row(
            short_id(message.id),
            "-" if message.sent_at is None else format_local(message.sent_at),
            message.from_address or "-",
            message.subject or "-",
            message.body_status.value,
            str(attachment_count),
            "-" if member is None else short_id(member.thread_id),
            "-" if analysis is None else analysis.category.value,
        )
    console.print(table)


async def _list_messages(
    account: str | None, limit: int
) -> list[tuple[MailMessage, int, MailThreadMember | None, MailAnalysis | None]]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    intelligence = bootstrap.mail_intelligence_repository(database)
    messages = await repository.list_messages(
        account_id=account, limit=limit
    )
    return [
        (
            message,
            len(await repository.list_attachments(message.id)),
            await intelligence.get_member(message.id),
            await intelligence.get_analysis(message.id),
        )
        for message in messages
    ]


@mail_app.command("show")
def mail_show(
    reference: Annotated[str, typer.Argument(help="Message id or unique prefix.")]
) -> None:
    """Show one stored message, including its body text and attachment metadata."""
    detail = _run(lambda: _load_message(reference))
    message, locations, attachments, member, analysis = detail
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
    table.add_row("Thread", "-" if member is None else str(member.thread_id))
    table.add_row("Thread link", "-" if member is None else member.link_status.value)
    if analysis is None:
        table.add_row("Analysis", "-")
    else:
        reply = "yes" if analysis.requires_reply else "no"
        table.add_row("Analysis", f"{analysis.category.value} (requires reply: {reply})")
    console.print(table)
    _print_locations(locations)
    _print_attachment_metadata(attachments)
    if analysis is not None:
        _print_analysis(analysis)
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
        MailThreadMember | None,
        MailAnalysis | None,
    ]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    intelligence = bootstrap.mail_intelligence_repository(database)
    message_id = await repository.resolve_message_id(reference)
    message = await repository.get_message(message_id)
    if message is None:  # pragma: no cover - resolution just found it
        raise MailMessageNotFound(reference)
    return (
        message,
        await repository.list_locations(message_id),
        await repository.list_attachments(message_id),
        await intelligence.get_member(message_id),
        await intelligence.get_analysis(message_id),
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


mail_thread_app = typer.Typer(
    help="Deterministic mail threads (read-only).", no_args_is_help=True
)


@mail_app.command("threads")
def mail_threads(
    account: Annotated[
        str | None, typer.Option("--account", help="Only this configured account.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many threads to show.")] = 20,
) -> None:
    """List deterministic mail threads, newest first. This never contacts a server."""
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_THREAD_LIST:
        fail(f"--limit must be at most {MAX_THREAD_LIST}")
    summaries = _run(lambda: _list_threads(account, limit))
    if not summaries:
        console.print("no mail threads")
        return
    table = Table(title="mail threads")
    for column in ("ID", "Account", "Messages", "Latest", "Subject"):
        table.add_column(column)
    for summary in summaries:
        table.add_row(
            short_id(summary.thread.id),
            summary.thread.account_id,
            str(summary.message_count),
            format_local(summary.latest_at),
            _subject_preview(summary.subject_preview),
        )
    console.print(table)


async def _list_threads(account: str | None, limit: int) -> list[MailThreadSummary]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return await bootstrap.mail_intelligence_repository(database).list_thread_summaries(
        account_id=account, limit=limit
    )


@mail_thread_app.command("show")
def mail_thread_show(
    reference: Annotated[str, typer.Argument(help="Thread id or unique prefix.")]
) -> None:
    """Show one thread's messages in conversation order. This never calls a model."""
    thread, entries = _run(lambda: _load_thread(reference))
    console.print(f"[bold]thread[/bold] {thread.thread.id}")
    console.print(f"account: {thread.thread.account_id}")
    console.print(f"messages: {thread.message_count}")
    table = Table(title="thread messages")
    for column in ("ID", "Sent", "From", "Subject", "Link", "Analysis"):
        table.add_column(column)
    for message, member, analysis in entries:
        table.add_row(
            short_id(message.id),
            "-" if message.sent_at is None else format_local(message.sent_at),
            message.from_address or "-",
            _subject_preview(message.subject),
            member.link_status.value if member is not None else "-",
            analysis.category.value if analysis is not None else "-",
        )
    console.print(table)


async def _load_thread(
    reference: str,
) -> tuple[
        MailThreadSummary,
        list[tuple[MailMessage, MailThreadMember | None, MailAnalysis | None]],
    ]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    intelligence = bootstrap.mail_intelligence_repository(database)
    thread_id = await intelligence.resolve_thread_id(reference)
    summaries = await intelligence.list_thread_summaries(account_id=None, limit=None)
    summary = next((item for item in summaries if item.thread.id == thread_id), None)
    if summary is None:  # pragma: no cover - resolution just found it
        raise MailThreadNotFound(reference)
    members = {member.message_id: member for member in await intelligence.list_thread_members(
        thread_id
    )}
    entries = [
        (message, members.get(message.id), await intelligence.get_analysis(message.id))
        for message in await intelligence.list_thread_messages(thread_id)
    ]
    return summary, entries


@mail_app.command("analysis")
def mail_analysis_view(
    reference: Annotated[str, typer.Argument(help="Message id or unique prefix.")]
) -> None:
    """Show the stored analysis of one message. This never calls a model."""
    message, stored = _run(lambda: _load_analysis(reference))
    if stored is None:
        console.print(f"{short_id(message.id)}: No analysis yet.")
        return
    table = Table(
        title=f"mail analysis {short_id(message.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("Message", str(message.id))
    table.add_row("Account", message.account_id)
    table.add_row("Analyzer version", str(stored.analyzer_version))
    table.add_row("Category", stored.category.value)
    table.add_row("Requires reply", "yes" if stored.requires_reply else "no")
    table.add_row("Summary", stored.summary)
    table.add_row("Analyzed", format_local(stored.updated_at))
    console.print(table)
    _print_candidates(stored.action_candidates)


async def _load_analysis(reference: str) -> tuple[MailMessage, MailAnalysis | None]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    repository = bootstrap.mail_repository(database)
    message_id = await repository.resolve_message_id(reference)
    message = await repository.get_message(message_id)
    if message is None:  # pragma: no cover - resolution just found it
        raise MailMessageNotFound(reference)
    return message, await bootstrap.mail_intelligence_repository(database).get_analysis(
        message_id
    )


def _print_analysis(analysis: MailAnalysis) -> None:
    console.print("[bold]Analysis[/bold]")
    console.print(analysis.summary)
    _print_candidates(analysis.action_candidates)


def _print_candidates(candidates: tuple[MailActionCandidate, ...]) -> None:
    if not candidates:
        console.print("action candidates: none")
        return
    table = Table(title="action candidates")
    for column in ("Text", "Temporal kind", "Time text", "Interpreted at"):
        table.add_column(column)
    for candidate in candidates:
        table.add_row(
            candidate.text,
            candidate.temporal_kind.value,
            candidate.time_text or "-",
            (
                "-"
                if candidate.temporal_kind is MailTemporalKind.NONE
                or candidate.interpreted_at is None
                else format_local(candidate.interpreted_at)
            ),
        )
    console.print(table)


def _subject_preview(subject: str | None) -> str:
    """A bounded, single-line subject preview for list output."""
    if subject is None:
        return "-"
    collapsed = " ".join(subject.split())
    if not collapsed:
        return "-"
    if len(collapsed) <= MAX_SUBJECT_PREVIEW_CHARS:
        return collapsed
    return collapsed[: MAX_SUBJECT_PREVIEW_CHARS - 1] + "\u2026"


@mail_app.command("drafts")
def mail_drafts(
    limit: Annotated[int, typer.Option("--limit", help="How many drafts to show.")] = 20,
) -> None:
    """List local reply drafts. This never contacts a server or calls a model."""
    if limit < 1:
        fail("--limit must be a positive integer")
    if limit > MAX_DRAFT_LIST:
        fail(f"--limit must be at most {MAX_DRAFT_LIST}")
    listed = _run(lambda: _list_drafts(limit))
    if not listed:
        console.print("no mail drafts")
        return
    table = Table(title="mail drafts")
    for column in ("ID", "Updated", "To", "Subject", "Origin", "Needs input", "Sources"):
        table.add_column(column)
    for entry in listed:
        draft = entry.draft
        table.add_row(
            short_id(draft.id),
            format_local(draft.updated_at),
            ", ".join(draft.to_addresses) or "-",
            _subject_preview(draft.subject),
            draft.origin.value,
            str(len(draft.needs_user_input)),
            str(entry.source_count),
        )
    console.print(table)
    console.print("These are local drafts. Nothing was sent.")


async def _list_drafts(limit: int) -> list[MailDraftListing]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return await bootstrap.mail_draft_service(clock, database).list_drafts(limit=limit)


mail_draft_app = typer.Typer(
    help="Local reply drafts: create, read and edit. Nothing here sends mail.",
    no_args_is_help=True,
)


@mail_draft_app.command("create")
def mail_draft_create(
    reference: Annotated[str, typer.Argument(help="Message id or unique prefix to reply to.")],
    context_query: Annotated[
        str | None,
        typer.Option(
            "--context-query",
            help=(
                "Search your indexed personal sources for this query and let the model use the "
                "result. Without it, no personal knowledge is read or sent."
            ),
        ),
    ] = None,
    root: Annotated[
        str | None,
        typer.Option("--root", help="Restrict the knowledge search to one storage root."),
    ] = None,
    context_limit: Annotated[
        int,
        typer.Option(
            "--context-limit",
            help=f"How many knowledge excerpts may be used (1..{MAX_DRAFT_CONTEXT_LIMIT}).",
        ),
    ] = DEFAULT_DRAFT_CONTEXT_LIMIT,
) -> None:
    """Write a reply draft with the configured model. This is not sending."""
    if root is not None and not (context_query or "").strip():
        fail("--root requires --context-query; without a query no knowledge is searched")
    if not 1 <= context_limit <= MAX_DRAFT_CONTEXT_LIMIT:
        fail(f"--context-limit must be between 1 and {MAX_DRAFT_CONTEXT_LIMIT}")
    config = _load_config_or_fail()
    result = _run(lambda: _create_draft(config, reference, context_query, root, context_limit))
    _print_created_draft(result)


async def _create_draft(
    config: AssistantConfig,
    reference: str,
    context_query: str | None,
    root: str | None,
    context_limit: int,
) -> MailDraftResult:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    message_id = await bootstrap.mail_repository(database).resolve_message_id(reference)
    service = bootstrap.mail_draft_writer(config, clock, database)
    return await service.create_reply_draft(
        message_id,
        context_query=context_query,
        root_id=root,
        context_limit=context_limit,
    )


def _print_created_draft(result: MailDraftResult) -> None:
    draft = result.draft
    console.print(f"[green]draft[/green] {draft.id}")
    console.print(f"to: {', '.join(draft.to_addresses)}")
    console.print(f"subject: {draft.subject}")
    console.print(f"knowledge sources used: {len(result.sources)}")
    if result.offline_roots:
        console.print(
            "offline roots (their content was not used): " + ", ".join(result.offline_roots)
        )
    console.print(f"open questions: {len(draft.needs_user_input)}")
    _print_draft_body(draft)
    _print_needs_input(draft)
    _print_sources(result.sources)
    console.print("[bold]This is a local draft. Nothing was sent.[/bold]")


@mail_draft_app.command("show")
def mail_draft_show(
    reference: Annotated[str, typer.Argument(help="Draft id or unique prefix.")]
) -> None:
    """Show one local draft. This never calls a model."""
    result, original = _run(lambda: _load_draft(reference))
    draft = result.draft
    table = Table(
        title=f"mail draft {short_id(draft.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("Draft ID", str(draft.id))
    table.add_row("Replying to", str(draft.reply_to_message_id))
    if original is not None:
        table.add_row("Original subject", original.subject or "-")
        table.add_row("Originally from", original.from_address or "-")
    table.add_row("To", ", ".join(draft.to_addresses))
    table.add_row("Subject", draft.subject)
    table.add_row("Version", str(draft.version))
    table.add_row("Origin", draft.origin.value)
    table.add_row("Created", format_local(draft.created_at))
    table.add_row("Updated", format_local(draft.updated_at))
    console.print(table)
    _print_draft_body(draft)
    _print_needs_input(draft)
    _print_sources(result.sources)
    console.print("[bold]This is a local draft. Nothing was sent.[/bold]")


async def _load_draft(reference: str) -> tuple[MailDraftResult, MailMessage | None]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    result = await bootstrap.mail_draft_service(clock, database).get_draft(reference)
    original = await bootstrap.mail_repository(database).get_message(
        result.draft.reply_to_message_id
    )
    return result, original


@mail_draft_app.command("edit")
def mail_draft_edit(
    reference: Annotated[str, typer.Argument(help="Draft id or unique prefix.")],
    subject: Annotated[
        str | None, typer.Option("--subject", help="Replace the draft subject.")
    ] = None,
    body: Annotated[str | None, typer.Option("--body", help="Replace the draft body.")] = None,
) -> None:
    """Edit a local draft's subject and/or body. This never sends anything."""
    if subject is None and body is None:
        fail("an edit needs --subject or --body")
    draft = _run(lambda: _edit_draft(reference, subject, body))
    console.print(
        f"[green]draft[/green] {draft.id} is now version {draft.version} "
        f"({draft.origin.value})"
    )
    console.print("This is a local draft. Nothing was sent.")


async def _edit_draft(reference: str, subject: str | None, body: str | None) -> MailDraft:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.mail_draft_service(clock, database)
    return await service.edit_draft(reference, subject=subject, body=body)


def _print_draft_body(draft: MailDraft) -> None:
    console.print("[bold]Body[/bold]")
    console.print(draft.body_text)


def _print_needs_input(draft: MailDraft) -> None:
    if not draft.needs_user_input:
        return
    console.print("[bold]Needs user input[/bold]")
    for item in draft.needs_user_input:
        console.print(f"- {item}")
    console.print(
        "[dim]The model could not support these from the supplied material; "
        "answer them in the body rather than letting a draft guess.[/dim]"
    )


def _print_sources(sources: tuple[MailDraftSource, ...]) -> None:
    if not sources:
        console.print("Knowledge sources: none (no context query was supplied).")
        return
    console.print("[bold]Knowledge sources[/bold]")
    for position, source in enumerate(sources, start=1):
        # Resolved locally from the stored provenance; never from model output.
        console.print(f"[{position}] {source.logical_uri} \u2014 {source.source_span.describe()}")


def register(app: typer.Typer) -> None:
    """Register the `pw mail` group on the root app."""
    app.add_typer(mail_app, name="mail")
    mail_app.add_typer(mail_thread_app, name="thread")
    mail_app.add_typer(mail_draft_app, name="draft")


__all__ = ["mail_app", "register"]
