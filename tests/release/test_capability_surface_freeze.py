"""v1 freezes the capability surfaces, so a release cannot widen one (sections 22, 41-45, 67).

Every earlier phase spent its effort on what the system *cannot* do: no approval without a human, no
external effect without an exact approved fingerprint, no executor the code does not have,
no watcher URL from a model, no fact that fills a form by itself, no playbook that runs. Release
hardening is when "just one convenience" would slip in, so the surfaces are pinned
here by name: mobile routes, MCP resources and tools, production executors, event types, watcher
protocols, eHall verbs and the learning verbs.

These are *freeze* assertions rather than behaviour tests: behaviour has its own suites. If one of
these fails, the question is not "how do I make the test pass" but "which ADR authorised the new
capability".
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from assistant.adapters.mcp.resources import RESOURCE_URIS
from assistant.adapters.mcp.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES
from assistant.application.ehall_certificate import E_HALL_CERTIFICATE_ACTION_TYPE
from assistant.application.mail_event_handler import MAIL_EVENT_TYPE
from assistant.application.mail_send_actions import MAIL_SEND_ACTION_TYPE
from assistant.application.observation_event_handler import (
    MANUAL_EVENT_TYPE,
    WEB_EVENT_TYPE,
)
from tests.support.ops import NOW

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"

PRODUCTION_EXECUTORS = {MAIL_SEND_ACTION_TYPE.value, E_HALL_CERTIFICATE_ACTION_TYPE.value}
"""The only two external capabilities v1 can perform, and only with an approval."""

EVENT_TYPES = frozenset({MAIL_EVENT_TYPE, WEB_EVENT_TYPE, MANUAL_EVENT_TYPE})
"""The complete set of inbound event types the worker dispatches."""


def _mobile_routes(tmp_path: Path) -> set[tuple[str, str]]:
    """The route table of the real app, built over a real MobileStack (no fakes of our own)."""
    from assistant.adapters.web.app import build_app
    from assistant.adapters.web.server import is_private_client
    from assistant.store.db import Database
    from assistant.store.migrations import apply_migrations
    from tests.support.fakes import FakeClock
    from tests.support.mobile import MobileStack, ScriptedTokenFactory

    database = Database.at(tmp_path / "assistant.db")
    clock = FakeClock(start=NOW)
    apply_migrations(database, clock=clock)
    stack = MobileStack(database, clock, ScriptedTokenFactory())
    application = build_app(stack.dependencies(), is_private=is_private_client)
    routes: set[tuple[str, str]] = set()
    for route in application.routes:
        if isinstance(route, APIRoute):
            routes.update((method, route.path) for method in route.methods)
    return routes


def test_the_mobile_route_set_is_frozen(tmp_path: Path) -> None:
    """§41: a phone may look and approve; nothing else was added, and nothing is about to be."""
    expected = {
        ("GET", "/"),
        ("GET", "/pair"),
        ("GET", "/approve/{action_id}"),
        ("GET", "/app.js"),
        ("GET", "/styles.css"),
        # Phase 11C (ADR-0043 §27): the settings shell is static like every other shell; its data
        # routes are registered only when this host has a mail configuration to manage.
        ("GET", "/settings"),
        ("GET", "/settings.js"),
        ("GET", "/settings.css"),
        ("POST", "/api/pair"),
        ("GET", "/api/me"),
        ("POST", "/api/logout"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("POST", "/api/tasks/{reference}/complete"),
        ("GET", "/api/cases"),
        ("GET", "/api/notifications"),
        ("POST", "/api/notifications/{reference}/read"),
        ("GET", "/api/mail/drafts"),
        ("GET", "/api/mail/drafts/{reference}"),
        ("PATCH", "/api/mail/drafts/{reference}"),
        ("GET", "/api/actions"),
        ("GET", "/api/actions/{reference}"),
        ("POST", "/api/actions/{reference}/challenge"),
        ("POST", "/api/actions/{reference}/approve"),
        ("POST", "/api/approval-link/preview"),
        ("POST", "/api/approval-link/approve"),
        # Phase 11D (ADR-0044 §44): the planning capacity rules, reported read-only. There is no
        # write route for the timezone, because it has exactly one authority.
        ("GET", "/api/settings/planning"),
    }

    assert _mobile_routes(tmp_path) == expected


def test_no_mobile_route_can_execute_send_or_submit(tmp_path: Path) -> None:
    """The route names themselves must not offer an effect: approval is not execution."""
    forbidden = ("execute", "send", "submit", "facts", "playbooks", "run", "shell")
    offenders = [
        f"{method} {path}"
        for method, path in _mobile_routes(tmp_path)
        if any(word in path.lower() for word in forbidden)
    ]

    assert offenders == []


def _chat_routes(tmp_path: Path) -> set[tuple[str, str]]:
    """The route table when the host *does* serve a conversation runtime."""
    import asyncio

    from assistant.adapters.web.app import build_app
    from assistant.adapters.web.server import is_private_client
    from tests.support.chat import build_chat

    stack = asyncio.run(build_chat(tmp_path))
    app = build_app(stack.dependencies(), is_private=is_private_client)
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.update((method, route.path) for method in route.methods)
    return routes


def test_the_web_chat_route_set_is_frozen(tmp_path: Path) -> None:
    """ADR-0041 §14: the browser chat surface is exactly this, and nothing generic is added.

    The control plane's own freeze (above) is unchanged, because a host without a conversation
    runtime still registers none of these routes.
    """
    expected = {
        ("GET", "/chat"),
        ("GET", "/chat.js"),
        ("GET", "/chat.css"),
        ("GET", "/api/chat/bootstrap"),
        ("GET", "/api/chat/threads"),
        ("POST", "/api/chat/threads"),
        ("GET", "/api/chat/threads/{thread_id}/snapshot"),
        ("POST", "/api/chat/threads/{thread_id}/messages"),
        ("POST", "/api/chat/requests/{request_id}/cancel"),
        ("GET", "/api/chat/threads/{thread_id}/events"),
        (
            "POST",
            "/api/chat/threads/{thread_id}/confirmations/{confirmation_id}/confirm",
        ),
        (
            "POST",
            "/api/chat/threads/{thread_id}/confirmations/{confirmation_id}/cancel",
        ),
        # Phase 11B (ADR-0042 §18, §21): read the inbox, settle one item. There is deliberately
        # no execute, approve, send, submit or retry route, and the assertion below pins that.
        ("GET", "/api/chat/attention"),
        ("POST", "/api/chat/attention/{item_id}/acknowledge"),
        ("POST", "/api/chat/attention/{item_id}/dismiss"),
    }
    chat_routes = {route for route in _chat_routes(tmp_path) if "/chat" in route[1]}

    assert chat_routes == expected
    for verb in ("execute", "approve", "send", "submit", "retry", "resend"):
        assert not [route for route in chat_routes if f"/{verb}" in route[1]]


def test_no_chat_route_offers_a_generic_capability(tmp_path: Path) -> None:
    """There is no `/execute`, no `/tool`, no `/action`, no `/approval` and no `/sql`."""
    forbidden = (
        "execute",
        "tool",
        "sql",
        "shell",
        "approval",
        "/api/chat/actions",
        "generic",
    )
    offenders = [
        f"{method} {path}"
        for method, path in _chat_routes(tmp_path)
        if "/chat" in path and any(word in path.lower() for word in forbidden)
    ]

    assert offenders == []


def test_the_mcp_surface_is_frozen() -> None:
    """§42: four resources, and a tool set decided by configuration rather than by version."""
    assert RESOURCE_URIS == (
        "assistant://status",
        "assistant://tasks/open",
        "assistant://cases/open",
        "assistant://plan/current",
    )
    assert READ_TOOL_NAMES == ("assistant_get_task", "assistant_get_case")
    assert WRITE_TOOL_NAMES == ("assistant_create_task", "assistant_complete_task")


def test_the_production_executor_set_is_frozen() -> None:
    """§43: two named capabilities. A third would be a new phase, not a release chore."""
    from assistant.bootstrap import registered_action_executors
    from assistant.domain.config import AssistantConfig, MailAccountConfig, MailConfig
    from tests.support.mail_send import assistant_config

    default = registered_action_executors(None)
    configured = registered_action_executors(assistant_config())
    empty = registered_action_executors(AssistantConfig(mail=MailConfig(accounts=())))
    account = MailAccountConfig(
        id="smail",
        host="imap.example.edu",
        port=993,
        username="student@example.edu",
        mailbox="INBOX",
        enabled=True,
    )
    inbound_only = registered_action_executors(
        AssistantConfig(mail=MailConfig(accounts=(account,)))
    )

    for executors in (default, configured, empty, inbound_only):
        assert {
            action_type.value for action_type in executors
        } <= PRODUCTION_EXECUTORS, executors
    assert MAIL_SEND_ACTION_TYPE.value in {item.value for item in configured}


def test_no_ehall_destructive_capability_exists() -> None:
    """§44: the risky errands are absent from the code, not forbidden by a prompt."""
    forbidden = (
        "drop_course",
        "drop-course",
        "dorm_checkout",
        "dorm-checkout",
        "delete_service",
        "cancel_application",
    )
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            if word in text:
                offenders.append(f"{path.name}: {word}")

    assert offenders == []


def test_the_event_worker_handles_exactly_three_event_types() -> None:
    """§67: a new inbound kind has to be a decision, not an accident of a refactor."""
    assert {
        "mail.message.received",
        "web.page.changed",
        "manual.input.received",
    } == EVENT_TYPES
    # The observation handler accepts exactly its two kinds; anything else is a permanent refusal.
    from assistant.application.observation_event_handler import ACCEPTED_EVENT_TYPES

    assert ACCEPTED_EVENT_TYPES == (WEB_EVENT_TYPE, MANUAL_EVENT_TYPE)
