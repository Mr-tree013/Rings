"""`pw mcp` — describing the local MCP surface, and printing a VS Code snippet (ADR-0030).

```text
pw mcp status          what this host would expose, and under which capability mode
pw mcp vscode-config   a JSON snippet to paste into the editor's MCP configuration
```

Both commands are local and read-only. `status` does not start a server and does not contact one;
`vscode-config` prints a snippet and never writes to `.vscode/`, to the user's editor configuration
or anywhere else — the editor's configuration belongs to the user.

The status output is generated from the same registration functions the server uses, so what it
prints is what a client would actually discover rather than a second, drifting description.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.mcp.resources import RESOURCE_URIS
from assistant.adapters.mcp.tools import (
    COMPLETE_TASK_TOOL,
    CREATE_TASK_TOOL,
    GET_CASE_TOOL,
    GET_TASK_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
)
from assistant.cli_support import console, fail
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import InvalidAssistantConfig

MCP_SERVER_NAME = "growing-assistant"
"""The server name the VS Code snippet uses; it must match the server's own reported name."""

mcp_app = typer.Typer(
    help="Local MCP surface: status, and a VS Code configuration snippet.",
    no_args_is_help=True,
)


def _config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def _surface(config: AssistantConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The exact resources and tools this configuration registers."""
    resources = RESOURCE_URIS
    tools: list[str] = [GET_TASK_TOOL, GET_CASE_TOOL]
    if config.mcp.expose_knowledge:
        tools.append(SEARCH_KNOWLEDGE_TOOL)
    if config.mcp.allows_task_writes:
        tools.extend((CREATE_TASK_TOOL, COMPLETE_TASK_TOOL))
    return resources, tuple(tools)


@mcp_app.command("status")
def mcp_status() -> None:
    """Show the MCP capability mode and the exact surface it registers. Local only."""
    config = _config_or_fail()
    resources, tools = _surface(config)
    table = Table(title="mcp status", show_header=False, title_justify="left")
    table.add_row("Enabled", "yes" if config.mcp.enabled else "no")
    table.add_row("Transport", "stdio")
    table.add_row("Write scope", config.mcp.write_scope)
    table.add_row(
        "Knowledge exposure", "yes" if config.mcp.expose_knowledge else "no"
    )
    console.print(table)
    console.print("[bold]Resources[/bold]")
    for uri in resources:
        console.print(f"- {uri}")
    console.print("[bold]Tools[/bold]")
    for tool in tools:
        console.print(f"- {tool}")
    if not config.mcp.enabled:
        console.print("")
        console.print(
            "The MCP server is disabled: `growing-assistant-mcp` exits without serving. "
            "Set [mcp] enabled = true in the host configuration to turn it on."
        )
    if config.mcp.write_scope == "none":
        console.print("")
        console.print(
            "Write scope is none, so no task tool exists on this surface: a client cannot create "
            "or complete a task even if it asks for one by name."
        )
    console.print("")
    console.print(
        "This surface has no approval, execution, mail, eHall, browser, filesystem, shell or "
        "model capability, on any configuration."
    )


@mcp_app.command("vscode-config")
def mcp_vscode_config(
    project_root: Annotated[
        Path | None,
        typer.Option("--project-root", help="Directory the server should run in."),
    ] = None,
) -> None:
    """Print a VS Code MCP configuration snippet. Nothing is written anywhere."""
    config = _config_or_fail()
    root = project_root if project_root is not None else _default_project_root()
    snippet = {
        "servers": {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "growing-assistant-mcp"],
                "cwd": str(root),
            }
        }
    }
    console.print(json.dumps(snippet, indent=2))
    console.print("")
    if not config.mcp.enabled:
        console.print(
            "Note: [mcp] enabled is false, so the server will exit when VS Code starts it. "
            "Enable it first."
        )
    console.print(
        "Paste this into your VS Code MCP configuration (workspace or user `mcp.json`); this "
        "command does not write it for you. VS Code will ask you to trust the server — that "
        "consent is the client's, not this project's authorization."
    )
    console.print(
        f"Current surface: write_scope={config.mcp.write_scope}, "
        f"expose_knowledge={'true' if config.mcp.expose_knowledge else 'false'}. "
        "It contains no API key, password or token."
    )


def _default_project_root() -> Path:
    """The repository root this package is installed from, used only as a snippet default."""
    return Path(__file__).resolve().parents[2]


def register(app: typer.Typer) -> None:
    """Register the `pw mcp` group on the root app."""
    app.add_typer(mcp_app, name="mcp")


__all__ = ["mcp_app", "register"]
