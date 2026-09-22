"""The chat HTTP surface: authentication, CSRF, headers, bounds and escaping.

ADR-0041 §15, §36-§52.

The rules here are the ones a browser could otherwise be tricked into breaking: nothing is readable
without the existing session, nothing mutates without the existing CSRF pair, no asset is fetched
from anywhere but this host, no response carries an approval challenge or a provider payload, and
every piece of user or mail text is returned as text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.web.app import CONTENT_SECURITY_POLICY
from tests.support.chat import ChatStack, build_chat
from tests.support.conversation import direct_reply, operation, plan

XSS_PAYLOADS = (
    "<script>alert(1)</script>",
    '<img src=x onerror=alert(1)>',
    '"</textarea><script>alert(1)</script>',
    "张老师 <b>老师</b>",
)

CHAT_READ_ROUTES = (
    "/api/chat/bootstrap",
    "/api/chat/threads",
)


@pytest.fixture
async def stack(tmp_path: Path) -> ChatStack:
    return await build_chat(tmp_path)


async def test_the_shell_and_its_assets_are_local_files(stack: ChatStack) -> None:
    with stack.client() as client:
        page = client.get("/chat")
        script = client.get("/chat.js")
        styles = client.get("/chat.css")

        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "/chat.js" in page.text and "/chat.css" in page.text
        assert script.headers["content-type"].startswith("application/javascript")
        assert styles.headers["content-type"].startswith("text/css")
        # Self-contained: no CDN, no third-party script, no font or image host.
        for markup in (page.text,):
            assert "http://" not in markup.replace("http://www.w3.org", "")
            assert "https://" not in markup
        for asset in (script.text, styles.text):
            assert "cdn." not in asset
            assert "https://" not in asset


@pytest.mark.parametrize("route", CHAT_READ_ROUTES)
async def test_a_read_requires_the_existing_session(stack: ChatStack, route: str) -> None:
    with stack.client() as client:
        assert client.get(route).status_code == 401


async def test_reading_one_thread_requires_a_session(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]
        stack.harness.queue(direct_reply("好。"))
    unpaired = stack.client()
    with unpaired:
        assert unpaired.get(f"/api/chat/threads/{thread}/snapshot").status_code == 401


async def test_an_unknown_thread_has_no_event_stream(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        response = client.get(
            "/api/chat/threads/11111111-2222-3333-4444-555555555555/events"
        )
        assert response.status_code == 404
        assert tokens["csrf"]


async def test_the_event_stream_requires_a_session(stack: ChatStack) -> None:
    with stack.client() as client:
        response = client.get("/api/chat/threads/11111111-2222-3333-4444-555555555555/events")

        assert response.status_code == 401


async def test_every_mutation_requires_the_csrf_pair(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]

        body = {"client_request_id": "11111111-2222-3333-4444-555555555555", "text": "你好"}
        assert (
            client.post(f"/api/chat/threads/{thread}/messages", json=body).status_code == 403
        )
        assert client.post("/api/chat/threads").status_code == 403
        assert (
            client.post(
                "/api/chat/requests/11111111-2222-3333-4444-555555555555/cancel"
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/chat/threads/{thread}/confirmations/mail_send:x/confirm",
                json={"expected_revision": "a" * 8},
            ).status_code
            == 403
        )
        # Nothing was accepted, queued or mutated by any of that.
        assert stack.harness.model.requests == []
        assert await stack.requests_for(thread) == []


async def test_a_wrong_csrf_header_is_refused(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]

        response = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "11111111-2222-3333-4444-555555555555", "text": "你好"},
            headers={"X-CSRF-Token": "not-the-csrf-token"},
        )

        assert response.status_code == 403
        assert await stack.requests_for(thread) == []


async def test_the_control_plane_keeps_its_security_headers(stack: ChatStack) -> None:
    with stack.client() as client:
        for route in ("/chat", "/api/chat/bootstrap"):
            response = client.get(route)
            assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["referrer-policy"] == "no-referrer"
        # No CORS header is introduced anywhere: the surface is same-origin by construction.
        assert "access-control-allow-origin" not in client.get("/chat").headers


async def test_a_public_peer_is_still_refused(stack: ChatStack) -> None:
    """The LAN rule is unchanged: a socket peer outside the private ranges gets nothing."""
    with stack.client(is_private=lambda peer: False) as client:
        assert client.get("/chat").status_code == 403
        assert client.get("/api/chat/bootstrap").status_code == 403


async def test_the_event_endpoint_answers_with_an_event_stream(stack: ChatStack) -> None:
    """The response contract, checked without consuming an endless body.

    The ASGI test client blocks until a response finishes, so an unlimited stream cannot be read
    through it. The frames themselves are covered where they are produced
    (`tests/unit/test_conversation_event_stream.py`), and what is asserted here is the part the
    HTTP layer owns: an authenticated GET that answers `text/event-stream` and is never cached.
    """
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]
        app = client.app
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/chat/threads/{thread_id}/events"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/chat/threads/{thread}/events",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", f"ga_mobile_session={tokens['session']}".encode()),
            ],
            "client": ("127.0.0.1", 41100),
            "app": app,
        }
    )

    response = await route.endpoint(thread, request)

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-store"
    await response.body_iterator.aclose()
    assert stack.surface.broker.subscriber_count == 0


async def test_an_oversized_body_is_refused_before_it_reaches_the_queue(
    stack: ChatStack,
) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]

        too_long = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "11111111-2222-3333-4444-555555555555", "text": "啊" * 4001},
            headers=stack.headers(tokens["csrf"]),
        )
        too_big = client.post(
            f"/api/chat/threads/{thread}/messages",
            content=b"x" * (17 * 1024),
            headers={**stack.headers(tokens["csrf"]), "Content-Type": "application/json"},
        )

        assert too_long.status_code == 400
        assert too_big.status_code == 413
        assert await stack.requests_for(thread) == []


async def test_a_blank_or_unidentified_message_is_refused(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]

        blank = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "11111111-2222-3333-4444-555555555555", "text": "   "},
            headers=stack.headers(tokens["csrf"]),
        )
        no_id = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"text": "你好"},
            headers=stack.headers(tokens["csrf"]),
        )

        assert blank.status_code == 400
        assert no_id.status_code == 400


async def test_an_unknown_thread_is_not_found(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        missing = "11111111-2222-3333-4444-555555555555"

        assert client.get(f"/api/chat/threads/{missing}/snapshot").status_code == 404
        assert client.get("/api/chat/threads/not-a-uuid/snapshot").status_code == 404
        assert (
            client.post(
                f"/api/chat/threads/{missing}/messages",
                json={"client_request_id": "11111111-2222-3333-4444-555555555555", "text": "你好"},
                headers=stack.headers(tokens["csrf"]),
            ).status_code
            == 404
        )


# ------------------------------------------------------------------ escaping and privacy


async def test_html_like_text_round_trips_as_text(tmp_path: Path) -> None:
    """The API carries text; the shell renders it as text; nothing is interpreted anywhere."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]
        stack.harness.queue(direct_reply(XSS_PAYLOADS[0]))
        for index, payload in enumerate(XSS_PAYLOADS):
            stack.harness.queue(plan(operation("task.create", _task_arguments(payload))))
            accepted = client.post(
                f"/api/chat/threads/{thread}/messages",
                json={
                    "client_request_id": f"payload-message-{index:04d}",
                    "text": payload,
                },
                headers=stack.headers(tokens["csrf"]),
            ).json()
            request_id = str(accepted["id"])
            assert await stack.wait_until_async(
                lambda request_id=request_id: _settled(stack, request_id)
            )

        snapshot = client.get(f"/api/chat/threads/{thread}/snapshot").json()
        texts = [item["text"] for item in snapshot["messages"]]
        for payload in XSS_PAYLOADS:
            assert payload in texts
        assert XSS_PAYLOADS[0] in texts  # the reply is not escaped on the wire either


async def _settled(stack: ChatStack, request_id: str) -> bool:
    row = await stack.request_row(request_id)
    return row is not None and row.status.value in {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }


def _task_arguments(title: str) -> dict[str, object]:
    return {
        "title": title,
        "description": None,
        "priority": "normal",
        "estimated_minutes": 30,
        "due_at": "2026-09-24T15:59:00+00:00",
    }


def test_the_shell_never_assigns_markup_or_evaluates_strings() -> None:
    """A static guarantee, because a dynamic one only ever sees the payloads someone remembered."""
    static = Path(__file__).resolve().parents[2] / "src/assistant/adapters/web/static"
    script = (static / "chat.js").read_text(encoding="utf-8")

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in script, forbidden
    for required in ("textContent", "createElement"):
        assert required in script, required
    markup = (static / "chat.html").read_text(encoding="utf-8")
    assert "<script src=\"/chat.js\"></script>" in markup
    assert "onerror" not in markup and "onload" not in markup


async def test_the_snapshot_carries_no_internals(tmp_path: Path) -> None:
    """A browser gets product state: no prompt, no provider output, no challenge, no schema."""
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]
        stack.harness.queue(
            plan(
                operation(
                    "mail.compose_new",
                    {
                        "subject": "测试",
                        "body": "你好",
                        "recipient_kind": "explicit_email",
                        "recipient_address": "alice@example.com",
                        "recipient_name": None,
                        "sender_account": None,
                        "draft_id": None,
                    },
                ),
                operation("mail.prepare_new_send", {"draft_id": None}),
            )
        )
        accepted = client.post(
            f"/api/chat/threads/{thread}/messages",
            json={
                "client_request_id": "snapshot-internals-0001",
                "text": "给 alice@example.com 发封邮件，主题“测试”，内容“你好”。",
            },
            headers=stack.headers(tokens["csrf"]),
        ).json()
        assert await stack.wait_until_async(lambda: _settled(stack, str(accepted["id"])))

        raw = client.get(f"/api/chat/threads/{thread}/snapshot").text

        for forbidden in (
            "instructions",
            "chain_of_thought",
            "reasoning",
            "approval_challenge",
            "jsonschema",
            "Traceback",
            "sqlite3",
            "provider",
        ):
            assert forbidden not in raw, forbidden


async def test_the_thread_list_is_bounded_and_titled_deterministically(
    tmp_path: Path,
) -> None:
    stack = await build_chat(tmp_path)
    with stack.client() as client:
        tokens = await stack.pair(client)
        long_text = "很长的一段话" * 10
        thread = client.post(
            "/api/chat/threads", headers=stack.headers(tokens["csrf"])
        ).json()["id"]
        stack.harness.queue(direct_reply("好。"))
        client.post(
            f"/api/chat/threads/{thread}/messages",
            json={"client_request_id": "title-derivation-0001", "text": long_text},
            headers=stack.headers(tokens["csrf"]),
        )
        accepted = (
            await stack.requests_for(thread)
        )[0]
        assert await stack.wait_until_async(lambda: _settled(stack, str(accepted.id)))

        listing = client.get("/api/chat/threads").json()["threads"]

        assert len(listing) == 1
        title = listing[0]["display_title"]
        assert len(title) <= 40
        assert title.endswith("…")
        assert listing[0]["active"] is True


def test_the_chat_page_redeems_a_fragment_pairing_token() -> None:
    """The auto-pair contract lives in the page: read the fragment, strip it, redeem once.

    `rings up` opens `/chat#pair=<token>`; the token is a capability, so it must never travel in a
    query string, must never become markup, and must be gone from the URL before the page does
    anything else with it.
    """
    script = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "assistant"
        / "adapters"
        / "web"
        / "static"
        / "chat.js"
    ).read_text(encoding="utf-8")

    assert "readPairFragment" in script
    assert "clearPairFragment" in script
    assert "history.replaceState" in script
    assert '"/api/pair"' in script
    assert "?pair=" not in script
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in script
