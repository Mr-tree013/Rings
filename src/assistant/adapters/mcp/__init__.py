"""The local MCP adapter: a stdio server with a fixed, configuration-bounded surface (ADR-0030).

This package is the only place the MCP SDK is imported. It exposes four resources and between two
and five tools, all of them built from the bounded DTOs in `application/mcp_facade.py`, and it has
no capability of its own: no approval, no execution, no mail, no eHall, no browser, no shell, no
filesystem and no model.
"""

from assistant.adapters.mcp.server import SERVER_NAME, build_server, main

__all__ = ["SERVER_NAME", "build_server", "main"]
