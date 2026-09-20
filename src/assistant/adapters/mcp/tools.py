"""The tools the local MCP surface exposes, and the annotations that describe them (ADR-0030).

Two read tools always exist (`assistant_get_task`, `assistant_get_case`), one optional knowledge
tool exists only when the host opts in, and two task write tools exist only when the host chose
`write_scope = "tasks"`. Nothing else is registered, and the absence is implemented as absence:
a capability that is not configured has no tool, so a client cannot call it by guessing a name.

Every tool returns an explicit `CallToolResult`:

- success carries one compact JSON document with a fixed field set;
- a refused call carries `isError = true` and a small `{"error": {"kind", "message"}}` document,
  whose message is written here — never a stack trace, a SQL fragment or a filesystem path.

Annotations travel to the client (`readOnlyHint`, `destructiveHint`, `openWorldHint`) so VS Code can
show its own confirmation affordances, but they are defence in depth: the authorization boundary is
which tools exist and what the facade will do, not what a client chooses to ask.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp.server import MCPServer

from assistant.adapters.mcp.resources import (
    case_detail_document,
    dumps,
    task_detail_document,
)
from assistant.application.mcp_facade import (
    McpFacade,
    McpKnowledgeResult,
)
from assistant.domain.errors import (
    AmbiguousId,
    CaseNotFound,
    DomainError,
    InvalidTask,
    McpInvalidArgument,
    McpToolUnavailable,
    StaleTaskUpdate,
    TaskNotFound,
    TaskNotOpen,
)
from assistant.domain.instants import parse_iso_instant

GET_TASK_TOOL = "assistant_get_task"
GET_CASE_TOOL = "assistant_get_case"
SEARCH_KNOWLEDGE_TOOL = "assistant_search_knowledge"
CREATE_TASK_TOOL = "assistant_create_task"
COMPLETE_TASK_TOOL = "assistant_complete_task"

READ_TOOL_NAMES: tuple[str, ...] = (GET_TASK_TOOL, GET_CASE_TOOL)
WRITE_TOOL_NAMES: tuple[str, ...] = (CREATE_TASK_TOOL, COMPLETE_TASK_TOOL)

_GET_TASK_DESCRIPTION = (
    "Return one task by full UUID or unique prefix: title, description, priority, status, "
    "estimate, deadline and timestamps. Read-only."
)
_GET_CASE_DESCRIPTION = (
    "Return one case by full UUID or unique prefix: title, status and lifecycle timestamps. "
    "The actions, approvals and executions inside a case are not returned. Read-only."
)
_SEARCH_KNOWLEDGE_DESCRIPTION = (
    "Search the user's LOCAL indexed personal knowledge and return up to 8 bounded excerpts with "
    "their logical URI and source span. This is a local deterministic full-text search: no model, "
    "no network and no web search are involved. Returns excerpts from the user's local indexed "
    "knowledge to the connected MCP client/model."
)
_CREATE_TASK_DESCRIPTION = (
    "Create one task with an explicit title, optional priority, optional estimate and optional "
    "timezone-aware ISO 8601 deadline. Uses the same TaskService the CLI uses, so deadlines, "
    "reminders and replan requests behave exactly as they do from `pw task add`."
)
_COMPLETE_TASK_DESCRIPTION = (
    "Complete one open task by full UUID or unique prefix. This is a terminal transition with the "
    "same compare-and-set semantics as `pw task done`; completing an already completed task fails."
)


def register_tools(server: MCPServer, facade: McpFacade) -> tuple[str, ...]:
    """Register exactly the tools this configuration allows, and return their names in order."""
    annotations = _annotations()
    registered: list[str] = []

    @server.tool(
        name=GET_TASK_TOOL,
        description=_GET_TASK_DESCRIPTION,
        annotations=annotations["read"],
    )
    async def get_task(reference: str) -> Any:
        return await _call(lambda: facade.get_task(reference), task_detail_document)

    registered.append(GET_TASK_TOOL)

    @server.tool(
        name=GET_CASE_TOOL,
        description=_GET_CASE_DESCRIPTION,
        annotations=annotations["read"],
    )
    async def get_case(reference: str) -> Any:
        return await _call(lambda: facade.get_case(reference), case_detail_document)

    registered.append(GET_CASE_TOOL)

    if facade.exposes_knowledge:

        @server.tool(
            name=SEARCH_KNOWLEDGE_TOOL,
            description=_SEARCH_KNOWLEDGE_DESCRIPTION,
            annotations=annotations["read"],
        )
        async def search_knowledge(
            query: str, root_id: str | None = None, limit: int = 5
        ) -> Any:
            return await _call(
                lambda: facade.search_knowledge(query, root_id=root_id, limit=limit),
                knowledge_document,
            )

        registered.append(SEARCH_KNOWLEDGE_TOOL)

    if facade.allows_task_writes:

        @server.tool(
            name=CREATE_TASK_TOOL,
            description=_CREATE_TASK_DESCRIPTION,
            annotations=annotations["create"],
        )
        async def create_task(
            title: str,
            priority: str | None = None,
            estimated_minutes: int | None = None,
            deadline: str | None = None,
        ) -> Any:
            return await _call(
                lambda: facade.create_task(
                    title=title,
                    priority=priority,
                    estimated_minutes=estimated_minutes,
                    deadline=_deadline(deadline),
                ),
                task_detail_document,
            )

        registered.append(CREATE_TASK_TOOL)

        @server.tool(
            name=COMPLETE_TASK_TOOL,
            description=_COMPLETE_TASK_DESCRIPTION,
            annotations=annotations["complete"],
        )
        async def complete_task(reference: str) -> Any:
            return await _call(
                lambda: facade.complete_task(reference), task_detail_document
            )

        registered.append(COMPLETE_TASK_TOOL)

    return tuple(registered)


def knowledge_document(result: McpKnowledgeResult) -> dict[str, Any]:
    """The knowledge result: logical URIs, spans and bounded excerpts only."""
    return {
        "query": result.query,
        "hits": [
            {
                "logical_uri": hit.logical_uri,
                "source_span": hit.source_span,
                "excerpt": hit.excerpt,
            }
            for hit in result.hits
        ],
        "offline_roots": list(result.offline_roots),
    }


def _deadline(value: str | None) -> datetime | None:
    """Parse an optional ISO 8601 deadline that must carry an explicit offset."""
    if value is None:
        return None
    try:
        return parse_iso_instant(value)
    except ValueError as exc:
        raise McpInvalidArgument("deadline", str(exc)) from exc


async def _call(operation: Callable[[], Any], builder: Callable[[Any], dict[str, Any]]) -> Any:
    """Run one facade call and turn its result or refusal into one bounded JSON document."""
    from mcp.types import CallToolResult, TextContent

    try:
        document = builder(await operation())
    except _REFUSALS as exc:
        return _error_result(type(exc).__name__, str(exc))
    except DomainError as exc:
        return _error_result(type(exc).__name__, str(exc))
    return CallToolResult(
        content=[TextContent(type="text", text=dumps(document))], is_error=False
    )


_REFUSALS = (
    AmbiguousId,
    CaseNotFound,
    InvalidTask,
    McpInvalidArgument,
    McpToolUnavailable,
    StaleTaskUpdate,
    TaskNotFound,
    TaskNotOpen,
)
"""Domain refusals a client is allowed to see a *message* for.

Everything else — a `ValueError` from an unexpected place, a store failure — is left to propagate,
because those carry implementation detail and the SDK reports them without echoing internals.
"""


def _error_result(kind: str, message: str) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": {"kind": kind, "message": message}},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        ],
        is_error=True,
    )


def _annotations() -> dict[str, Any]:
    """The annotations each tool carries, built from the SDK's own model."""
    from mcp.types import ToolAnnotations

    return {
        "read": ToolAnnotations(read_only_hint=True, open_world_hint=False),
        "create": ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
        "complete": ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=False
        ),
    }


__all__ = [
    "COMPLETE_TASK_TOOL",
    "CREATE_TASK_TOOL",
    "GET_CASE_TOOL",
    "GET_TASK_TOOL",
    "READ_TOOL_NAMES",
    "SEARCH_KNOWLEDGE_TOOL",
    "WRITE_TOOL_NAMES",
    "knowledge_document",
    "register_tools",
]
