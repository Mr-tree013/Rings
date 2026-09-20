"""`pw mobile` — pairing, sessions and one-shot approval links (ADR-0026).

```bash
pw mobile status                 local, read-only: config, sessions, candidate LAN URLs
pw mobile pair                   mint a one-time code and print it once
pw mobile sessions               list sessions, active or not
pw mobile revoke SESSION         cut one session off
pw mobile approval-link ACTION   a link that approves one exact action, and stops there
```

Nothing here starts a server or contacts the network. The pairing code and the approval link are
printed to the terminal exactly once: they are never written to a log, never put in a URL query
string, and never stored in plaintext anywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.web.server import lan_addresses
from assistant.application.action_service import ActionService
from assistant.application.mobile_auth import MobileAuthService, MobileStatus
from assistant.cli_support import console, fail, format_local, short_id
from assistant.domain.action import ActionRequest
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    ActionRequestNotFound,
    AmbiguousId,
    DomainError,
    InvalidAssistantConfig,
    MobileDisabled,
    MobileSessionNotFound,
)
from assistant.domain.mobile import MobilePairingIssued
from assistant.store.errors import StoreError

mobile_app = typer.Typer(
    help="Same-LAN mobile control plane: pairing, sessions and approval links.",
    no_args_is_help=True,
)

_EXPECTED_FAILURES = (
    ActionRequestNotFound,
    AmbiguousId,
    MobileDisabled,
    MobileSessionNotFound,
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
        fail(f"store failure: {exc}")


def _load_config_or_fail() -> AssistantConfig:
    """Load the host configuration, or fail with a readable message."""
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def _service(
    config: AssistantConfig, factory: Callable[..., MobileAuthService]
) -> MobileAuthService:
    """Build the auth service for one command run."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return factory(config, clock, database)


def _urls(config: AssistantConfig) -> tuple[str, ...]:
    """Candidate base URLs a phone could use. Reported as candidates, never as facts.

    A LAN bind prints every address the kernel reported. When the host cannot enumerate one — a
    sandbox, a restricted container, a machine with no route — the loopback URL is printed instead
    with a note, because a pairing command that prints nothing is worse than one that prints an
    address the user has to adjust.
    """
    addresses = lan_addresses() if config.mobile.bind == "lan" else ()
    if not addresses:
        addresses = ("127.0.0.1",)
    return tuple(f"http://{address}:{config.mobile.port}" for address in addresses)


@mobile_app.command("status")
def mobile_status() -> None:
    """Show the local mobile capability. This starts no server and contacts nothing."""
    config = _load_config_or_fail()
    status: MobileStatus = _run(
        lambda: _service(config, bootstrap.mobile_auth_service).status(
            bind=config.mobile.bind,
            port=config.mobile.port,
            enabled=config.mobile.enabled,
        )
    )
    table = Table(title="mobile status", show_header=False, title_justify="left")
    table.add_row("Enabled", "yes" if status.enabled else "no")
    table.add_row("Bind mode", status.bind)
    table.add_row("Port", str(status.port))
    table.add_row("Active sessions", str(status.active_sessions))
    table.add_row("Total sessions", str(status.total_sessions))
    console.print(table)
    console.print("[bold]Candidate URLs[/bold]")
    for url in _urls(config):
        console.print(f"- {url}")
    if config.mobile.bind == "lan" and not lan_addresses():
        console.print(
            "[yellow]No LAN address could be detected on this host; replace the address above "
            "with this machine's local IP.[/yellow]"
        )
    console.print(
        "These are candidates: this command does not check whether the port is reachable or "
        "whether the daemon is running."
    )


@mobile_app.command("pair")
def mobile_pair() -> None:
    """Mint a one-time pairing code and print it once."""
    config = _load_config_or_fail()
    issued: MobilePairingIssued = _run(
        lambda: _service(config, bootstrap.mobile_auth_service).create_pairing_token()
    )
    console.print("[bold]Open:[/bold] " + ", ".join(f"{url}/pair" for url in _urls(config)))
    console.print("")
    console.print("[bold]Pairing code:[/bold]")
    console.print(issued.token)
    console.print("")
    console.print(f"Expires: {format_local(issued.pairing.expires_at)}")
    console.print(
        "The code is shown once, is stored only as a hash, and works for one pairing. "
        "Paste it into the pairing page by hand: it is never put in a URL."
    )


@mobile_app.command("sessions")
def mobile_sessions(
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Include revoked and expired sessions.")
    ] = True,
) -> None:
    """List paired browsers. This is local state only."""
    config = _load_config_or_fail()
    sessions = _run(
        lambda: _service(config, bootstrap.mobile_auth_service).list_sessions(
            include_inactive=all_sessions
        )
    )
    if not sessions:
        console.print("no paired sessions")
        return
    now = bootstrap.system_clock().now()
    table = Table(title="mobile sessions")
    for column in ("ID", "Created", "Last seen", "Expires", "State"):
        table.add_column(column)
    for session in sessions:
        state = "revoked" if session.is_revoked() else (
            "expired" if session.is_expired(now) else "active"
        )
        table.add_row(
            short_id(session.id),
            format_local(session.created_at),
            format_local(session.last_seen_at),
            format_local(session.expires_at),
            state,
        )
    console.print(table)


@mobile_app.command("revoke")
def mobile_revoke(
    reference: Annotated[str, typer.Argument(help="Session id or unique prefix.")]
) -> None:
    """Revoke one session, so that browser must pair again."""
    config = _load_config_or_fail()
    session = _run(
        lambda: _service(config, bootstrap.mobile_auth_service).revoke_session(reference)
    )
    console.print(f"[yellow]revoked[/yellow] {short_id(session.id)}")


@mobile_app.command("approval-link")
def mobile_approval_link(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Print a link that approves exactly one action — and never executes it."""
    config = _load_config_or_fail()
    action, token, expires_at = _run(lambda: _approval_link(config, reference))
    for base in _urls(config):
        console.print(f"{base}/approve/{action.id}#token={token}")
    console.print("")
    console.print(f"Action: {action.id}")
    console.print(f"Fingerprint: {action.fingerprint}")
    console.print(f"Expires: {format_local(expires_at)}")
    console.print(
        "The token is in the URL fragment, so it never reaches the server's logs. The page strips "
        "it from the address bar as soon as it loads."
    )
    console.print(
        "[bold]Approving this link does not execute anything.[/bold] Run "
        f"`pw action execute {short_id(action.id)}` on the host when you are ready."
    )


async def _approval_link(
    config: AssistantConfig, reference: str
) -> tuple[ActionRequest, str, datetime]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    actions = ActionService(bootstrap.action_repository(database), clock)
    action = await actions.require_action(reference)
    issued = await bootstrap.approval_service(clock, database).create_challenge(action.id)
    return action, issued.token, issued.challenge.expires_at


def register(app: typer.Typer) -> None:
    """Register the `pw mobile` group on the root app."""
    app.add_typer(mobile_app, name="mobile")


__all__ = ["mobile_app", "register"]
