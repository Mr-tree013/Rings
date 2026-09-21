"""The attention inbox over the real chat HTTP surface (ADR-0042 §18-19, §68).

Two things are being pinned here. The inbox is readable and settleable only through the existing
session and CSRF pair, and the *startup* failure a browser sees is told apart correctly: a host
that is answering perfectly well while refusing this browser is not a host that is offline.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from assistant import bootstrap
from assistant.adapters.web.app import CSRF_COOKIE, SESSION_COOKIE
from assistant.application.conversation_event_broker import (
    SAFE_EVENT_NAMES,
    ConversationEventBroker,
)
from assistant.daemon.attention_service import EVENT_NAME, AttentionRefreshService
from assistant.domain.attention import AttentionStatus
from tests.support.chat import ChatStack, build_chat


@pytest.fixture
async def stack(tmp_path: Path) -> ChatStack:
    return await build_chat(tmp_path)


async def _overdue_task(stack: ChatStack) -> None:
    task = await stack.harness.create_task("写报告")
    await stack.harness.tasks.set_deadline(task.id, stack.clock.now() - timedelta(hours=2))


async def test_the_inbox_requires_the_existing_session(stack: ChatStack) -> None:
    with stack.client() as client:
        assert client.get("/api/chat/attention").status_code == 401


async def test_settling_requires_the_csrf_pair(stack: ChatStack) -> None:
    await _overdue_task(stack)
    with stack.client() as client:
        tokens = await stack.pair(client)
        item_id = client.get("/api/chat/attention").json()["items"][0]["id"]

        refused = client.post(f"/api/chat/attention/{item_id}/acknowledge")
        assert refused.status_code == 403
        assert client.get("/api/chat/attention").json()["items"][0]["status"] == "open"

        wrong = client.post(
            f"/api/chat/attention/{item_id}/dismiss",
            headers={"X-CSRF-Token": "not-the-csrf-token"},
        )
        assert wrong.status_code == 403
        assert client.get("/api/chat/attention").json()["items"][0]["status"] == "open"
        assert tokens["csrf"]


async def test_the_inbox_reads_and_settles_over_http(stack: ChatStack) -> None:
    await _overdue_task(stack)
    with stack.client() as client:
        tokens = await stack.pair(client)

        payload = client.get("/api/chat/attention").json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["severity"] == "high"
        assert "写报告" in item["title"]
        # Product language only: no kind constant, no subsystem name, no source internals.
        assert "task_overdue" not in item["title"]
        assert "fingerprint" not in item
        assert "dedupe_key" not in item

        acknowledged = client.post(
            f"/api/chat/attention/{item['id']}/acknowledge",
            headers=stack.headers(tokens["csrf"]),
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["item"]["status"] == "acknowledged"

        # Acknowledging is not resolving: the situation is still live, and still listed.
        assert client.get("/api/chat/attention").json()["total"] == 1


async def test_dismissing_does_not_change_the_task(stack: ChatStack) -> None:
    await _overdue_task(stack)
    with stack.client() as client:
        tokens = await stack.pair(client)
        item_id = client.get("/api/chat/attention").json()["items"][0]["id"]

        client.post(
            f"/api/chat/attention/{item_id}/dismiss", headers=stack.headers(tokens["csrf"])
        )

        item = client.get("/api/chat/attention").json()["items"][0]
        assert item["status"] == "dismissed"
        tasks = client.get("/api/tasks").json()
        assert [entry["status"] for entry in tasks["tasks"]] != ["completed"]


async def test_settling_an_item_that_is_gone_is_an_honest_404(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        response = client.post(
            "/api/chat/attention/8b1e0f7a-0000-4000-8000-000000000000/acknowledge",
            headers=stack.headers(tokens["csrf"]),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"


async def test_bootstrap_is_401_rather_than_offline_for_an_unpaired_browser(
    stack: ChatStack,
) -> None:
    """The v1.2 dogfooding bug: a 401 used to be rendered as "无法连接主机"."""
    with stack.client() as client:
        response = client.get("/api/chat/bootstrap")
        assert response.status_code == 401
        # And the shell is still served, which is exactly why the browser has to tell the two apart
        # from the response rather than from whether the request completed at all.
        assert client.get("/chat").status_code == 200


async def test_the_pair_page_says_browser_not_phone(stack: ChatStack) -> None:
    with stack.client() as client:
        page = client.get("/pair")
        assert page.status_code == 200
        assert "配对此浏览器" in page.text
        assert "Pair this browser" in page.text
        assert "phone" not in page.text.lower()


async def test_the_attention_event_is_a_hint_and_reaches_every_subscriber(
    stack: ChatStack,
) -> None:
    assert EVENT_NAME in SAFE_EVENT_NAMES
    broker = ConversationEventBroker()
    first = broker.subscribe(uuid4())
    second = broker.subscribe(uuid4())

    delivered = broker.broadcast(EVENT_NAME, {"changed": True})

    assert delivered == 2
    assert (await first.next_event(timeout=0.1)).name == EVENT_NAME
    assert (await second.next_event(timeout=0.1)).name == EVENT_NAME


async def test_the_refresh_service_publishes_only_when_something_changed(
    stack: ChatStack,
) -> None:
    await _overdue_task(stack)
    broker = ConversationEventBroker()
    broker.subscribe(uuid4())
    service = AttentionRefreshService(
        bootstrap.attention_projector(stack.database, stack.clock, stack.harness.config),
        broker=broker,
    )

    await service.refresh_once()
    await service.refresh_once()
    await service.refresh_once()

    # One change, one hint — a subscriber is never asked to redraw for nothing.
    assert broker.subscriber_count == 1
    summary = await bootstrap.attention_service(stack.database, stack.clock).list_live()
    assert summary.total == 1


async def test_the_web_assets_have_no_third_party_reference(stack: ChatStack) -> None:
    with stack.client() as client:
        script = client.get("/chat.js").text
        markup = client.get("/chat").text

    assert "attention.updated" in script
    assert "需要处理" in markup
    assert "无法连接主机" in script  # still used, but only for a real connection failure
    assert "配对" in script
    assert "SESSION_COOKIE" in script
    assert SESSION_COOKIE == "ga_mobile_session"
    assert CSRF_COOKIE == "ga_mobile_csrf"
    for asset in (script, markup):
        assert "https://" not in asset
        assert "innerHTML" not in asset


async def test_attention_items_are_not_approvals_or_executions(stack: ChatStack) -> None:
    """§21/§75: settling an item must never create an approval or start a run."""
    import sqlite3

    await _overdue_task(stack)
    with stack.client() as client:
        tokens = await stack.pair(client)
        item_id = client.get("/api/chat/attention").json()["items"][0]["id"]
        client.post(
            f"/api/chat/attention/{item_id}/acknowledge", headers=stack.headers(tokens["csrf"])
        )

    connection = sqlite3.connect(str(stack.database.path))
    try:
        approvals = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
        actions = connection.execute("SELECT COUNT(*) FROM action_requests").fetchone()[0]
    finally:
        connection.close()

    assert (approvals, runs, actions) == (0, 0, 0)


def test_the_attention_routes_are_exactly_three() -> None:
    """§41/§75: the surface is read, acknowledge, dismiss — and there is no execute endpoint."""
    from pathlib import Path as _Path

    source = _Path("src/assistant/adapters/web/chat.py").read_text(encoding="utf-8")
    routes = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("@app.get(", "@app.post("))
    ]
    attention_routes = [route for route in routes if "attention" in route]

    assert attention_routes == [
        '@app.get("/api/chat/attention")',
        '@app.post("/api/chat/attention/{item_id}/acknowledge")',
        '@app.post("/api/chat/attention/{item_id}/dismiss")',
    ]
    for forbidden in ("execute", "approve", "submit", "send", "retry"):
        assert f"/{forbidden}" not in "".join(attention_routes)


def test_attention_status_vocabulary_is_closed() -> None:
    assert [status.value for status in AttentionStatus] == [
        "open",
        "acknowledged",
        "dismissed",
        "resolved",
    ]
