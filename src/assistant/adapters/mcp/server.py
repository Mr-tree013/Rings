"""The local stdio MCP server: composition, lifecycle and nothing else (ADR-0030).

```text
VS Code (MCP host)
      │  spawns a subprocess, stdio only
      ▼
growing-assistant-mcp
      │  load host config → require [mcp].enabled
      │  build the bounded facade → register the exact surface
      ▼
MCPServer.run_stdio_async()      stdout = protocol, stderr = logs
```

Three properties are deliberate:

- **the process exists to be a local adapter.** It is not the daemon, it does not contact the
  daemon, and it works with `assistantd` stopped: SQLite's own transactional semantics handle
  concurrency, exactly as they do for two CLI invocations at once;
- **stdout is protocol-only.** Logging is configured onto stderr and the module never prints, so the
  JSON-RPC stream cannot be polluted by a banner, a warning or a Rich table;
- **the surface is decided before the server starts.** Which resources and tools exist is a function
  of the host configuration, so a client that asks for something the host did not enable gets an
  unknown tool rather than a refusal it can argue with.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from assistant import bootstrap
from assistant.adapters.mcp.resources import register_resources
from assistant.adapters.mcp.tools import register_tools
from assistant.application.mcp_facade import McpFacade
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import InvalidAssistantConfig

LOGGER = logging.getLogger("assistant.mcp")

SERVER_NAME = "growing-assistant"
"""The name this server reports to the MCP host."""


def configure_logging(stream: Any = None) -> None:
    """Send every log record to stderr, leaving stdout to the protocol.

    The stream is injectable so a test can capture records; in production it is always `sys.stderr`,
    because writing a log line to stdout would corrupt the JSON-RPC stream.
    """
    target = sys.stderr if stream is None else stream
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def build_server(
    facade: McpFacade,
    *,
    version: str,
    instructions: str | None = None,
) -> Any:
    """Build the server and register exactly the surface this configuration allows."""
    from mcp.server import MCPServer

    server = MCPServer(
        SERVER_NAME,
        version=version,
        instructions=instructions or _INSTRUCTIONS,
    )
    register_resources(server, facade)
    register_tools(server, facade)
    return server


_INSTRUCTIONS = (
    "This is a local, read-mostly view of the user's own growing-assistant state: open tasks, open "
    "cases and the current week's plan. It cannot approve or execute anything, cannot send mail, "
    "cannot submit anything to a university system, cannot confirm facts or promote playbooks, and "
    "has no filesystem, shell, HTTP or browser capability. Ask the user to run the corresponding "
    "CLI command when an action is needed."
)


async def serve(config: AssistantConfig, *, version: str | None = None) -> None:
    """Run the stdio server for one host configuration.

    Raises:
        McpDisabled: the host has not enabled the MCP surface.
    """
    facade = bootstrap.mcp_facade(config)
    server = build_server(
        facade, version=version if version is not None else bootstrap.assistant_version()
    )
    LOGGER.info(
        "mcp server starting transport=stdio write_scope=%s knowledge=%s",
        facade.write_scope,
        facade.exposes_knowledge,
    )
    await server.run_stdio_async()


def main() -> None:
    """Console-script entry point (`growing-assistant-mcp`).

    Deliberately takes no arguments: there is no `--transport`, no `--port`, no `--host` and no
    `--unsafe`. The only supported transport is stdio, and the capability surface comes from the
    host configuration file rather than from a command line.

    An unrecognised argument is an error rather than something to ignore: a user who typed
    `--transport http` must be told that the option does not exist, instead of getting a stdio
    server and believing they asked for something else.
    """
    configure_logging()
    arguments = sys.argv[1:]
    if arguments and arguments != ["--version"]:
        LOGGER.error(
            "unknown argument(s): %s; this server is stdio-only and takes no options",
            " ".join(arguments),
        )
        raise SystemExit(2)
    if arguments == ["--version"]:
        sys.stderr.write(f"growing-assistant-mcp {bootstrap.assistant_version()}\n")
        raise SystemExit(0)
    try:
        config = asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        LOGGER.error("invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    if not config.mcp.enabled:
        # Nothing is written to stdout here: a client parsing the stream must not receive prose.
        LOGGER.error(
            "the MCP server is disabled; set [mcp] enabled = true in the host configuration"
        )
        raise SystemExit(2)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:  # pragma: no cover - the host closes stdin first
        LOGGER.info("mcp server interrupted")


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    main()


__all__ = ["SERVER_NAME", "build_server", "configure_logging", "main", "serve"]
