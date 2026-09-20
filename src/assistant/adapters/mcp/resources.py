"""The four resources the local MCP surface exposes (ADR-0030).

Each one is a fixed URI registered at construction time. There is no URI template, no parameter, no
path and no "read whatever the client asks for": a client can read these four documents, or nothing.
That is why the default surface cannot hand an editor agent a mail body or a knowledge excerpt by
accident — those resources do not exist here.

Every document is JSON built from a bounded DTO, with a fixed field set and deterministic ordering,
and it is serialized compactly and sorted so two reads of unchanged state are byte-identical.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer

from assistant.application.mcp_facade import (
    McpCaseDetail,
    McpCaseSummary,
    McpFacade,
    McpPlanView,
    McpStatusView,
    McpTaskDetail,
    McpTaskSummary,
)
from assistant.domain.errors import DomainError

STATUS_URI = "assistant://status"
OPEN_TASKS_URI = "assistant://tasks/open"
OPEN_CASES_URI = "assistant://cases/open"
CURRENT_PLAN_URI = "assistant://plan/current"

RESOURCE_URIS: tuple[str, ...] = (
    STATUS_URI,
    OPEN_TASKS_URI,
    OPEN_CASES_URI,
    CURRENT_PLAN_URI,
)
"""The complete default resource surface, in registration order."""

JSON_MIME_TYPE = "application/json"

_STATUS_DESCRIPTION = (
    "Local status of the assistant: version, MCP capability mode and counts of open tasks, open "
    "cases and unread notifications. Contains no mail, draft, fact, playbook, action or credential."
)
_OPEN_TASKS_DESCRIPTION = (
    "Up to 50 open tasks with their deadline, in the project's deterministic task order."
)
_OPEN_CASES_DESCRIPTION = (
    "Up to 50 open cases with their lifecycle fields. Actions, approvals and executions inside a "
    "case are not part of this resource."
)
_CURRENT_PLAN_DESCRIPTION = (
    "The current planning week's blocks (up to 100), or configured=false when planning is not "
    "configured. Reading this resource never plans, applies or replans anything."
)


def dumps(document: dict[str, Any]) -> str:
    """Serialize one resource document: compact, sorted and UTF-8 safe."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def moment(value: Any) -> str | None:
    """Render an instant deterministically, or `None`."""
    return None if value is None else value.isoformat()


def status_document(view: McpStatusView) -> dict[str, Any]:
    """The status resource: counts and capability mode, and nothing else."""
    return {
        "version": view.version,
        "mcp": {
            "transport": "stdio",
            "write_scope": view.write_scope,
            "knowledge_exposure": view.knowledge_exposure,
        },
        "counts": {
            "open_tasks": view.open_task_count,
            "open_cases": view.open_case_count,
            "unread_notifications": view.unread_notification_count,
        },
        "capabilities": list(view.capabilities),
    }


def task_summary_document(task: McpTaskSummary) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "priority": task.priority,
        "status": task.status,
        "estimated_minutes": task.estimated_minutes,
        "deadline": moment(task.deadline),
    }


def task_detail_document(task: McpTaskDetail) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "status": task.status,
        "estimated_minutes": task.estimated_minutes,
        "deadline": moment(task.deadline),
        "created_at": moment(task.created_at),
        "updated_at": moment(task.updated_at),
    }


def case_summary_document(case: McpCaseSummary) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "status": case.status,
        "created_at": moment(case.created_at),
        "updated_at": moment(case.updated_at),
    }


def case_detail_document(case: McpCaseDetail) -> dict[str, Any]:
    return {
        "id": case.id,
        "title": case.title,
        "status": case.status,
        "created_at": moment(case.created_at),
        "updated_at": moment(case.updated_at),
        "completed_at": moment(case.completed_at),
        "cancelled_at": moment(case.cancelled_at),
    }


def plan_document(plan: McpPlanView) -> dict[str, Any]:
    return {
        "configured": plan.configured,
        "timezone": plan.timezone,
        "starts_at": moment(plan.starts_at),
        "ends_at": moment(plan.ends_at),
        "blocks": [
            {
                "task_id": block.task_id,
                "start": moment(block.starts_at),
                "end": moment(block.ends_at),
                "origin": block.origin,
            }
            for block in plan.blocks
        ],
    }


def register_resources(server: MCPServer, facade: McpFacade) -> tuple[str, ...]:
    """Register the four resources on `server` and return their URIs, in order."""
    readers: tuple[tuple[str, str, str, Callable[[], Any]], ...] = (
        (STATUS_URI, "assistant-status", _STATUS_DESCRIPTION, facade.status),
        (OPEN_TASKS_URI, "assistant-open-tasks", _OPEN_TASKS_DESCRIPTION, facade.open_tasks),
        (OPEN_CASES_URI, "assistant-open-cases", _OPEN_CASES_DESCRIPTION, facade.open_cases),
        (
            CURRENT_PLAN_URI,
            "assistant-current-plan",
            _CURRENT_PLAN_DESCRIPTION,
            facade.current_plan,
        ),
    )
    builders: dict[str, Callable[[Any], dict[str, Any]]] = {
        STATUS_URI: status_document,
        OPEN_TASKS_URI: _tasks_document,
        OPEN_CASES_URI: _cases_document,
        CURRENT_PLAN_URI: plan_document,
    }

    for uri, name, description, reader in readers:
        server.resource(
            uri, name=name, description=description, mime_type=JSON_MIME_TYPE
        )(_resource_handler(uri, reader, builders[uri]))
    return RESOURCE_URIS


def _resource_handler(
    uri: str, reader: Callable[[], Any], builder: Callable[[Any], dict[str, Any]]
) -> Callable[[], Awaitable[str]]:
    """Wrap one read method so a domain error becomes a bounded message, not a traceback."""

    async def handler() -> str:
        try:
            return dumps(builder(await reader()))
        except DomainError as exc:
            # A resource read has no error channel in this SDK beyond the payload itself, so the
            # failure is reported as a small document rather than as an exception with internals.
            return dumps({"error": {"kind": type(exc).__name__, "message": str(exc)}})

    handler.__name__ = uri.replace("://", "_").replace("/", "_")
    return handler


def _tasks_document(tasks: tuple[McpTaskSummary, ...]) -> dict[str, Any]:
    return {"tasks": [task_summary_document(task) for task in tasks]}


def _cases_document(cases: tuple[McpCaseSummary, ...]) -> dict[str, Any]:
    return {"cases": [case_summary_document(case) for case in cases]}


__all__ = [
    "CURRENT_PLAN_URI",
    "JSON_MIME_TYPE",
    "OPEN_CASES_URI",
    "OPEN_TASKS_URI",
    "RESOURCE_URIS",
    "STATUS_URI",
    "case_detail_document",
    "case_summary_document",
    "dumps",
    "moment",
    "plan_document",
    "register_resources",
    "status_document",
    "task_detail_document",
    "task_summary_document",
]
