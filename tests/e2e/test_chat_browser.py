"""The browser acceptance: a real Chromium against the real app (ADR-0041 §73-§74).

Everything below is the shipped code path: the real ASGI app over a real runtime database, a real
Uvicorn server on loopback, the real chat page and the real conversation runtime — with only the
provider and the SMTP transport scripted, because a test must never spend credit or send mail.

The browser is used for what only a browser can prove: that the page renders escaped text, that the
composer is IME-safe and multiline-aware, that a card click settles exactly one thing, and that a
reload reconstructs state instead of replaying it.

This module is skipped when no Chromium binary is available. The suite refuses outbound sockets on
purpose, so the download is not part of the quality gate; the same workflow is covered
deterministically by the HTTP integration tests, and the release notes say so.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest

from assistant.adapters.web.app import CSRF_COOKIE, SESSION_COOKIE, build_app
from assistant.adapters.web.server import is_private_client
from tests.support.chat import ChatStack, build_chat
from tests.support.conversation import direct_reply, operation, plan

pytestmark = pytest.mark.e2e

PLAYWRIGHT_CACHE = Path.home() / ".cache" / "ms-playwright"

XSS_PAYLOAD = "<script>alert(1)</script>"


def _chromium_executable() -> str | None:
    """The newest downloaded Chromium, if this machine has one."""
    candidates = sorted(PLAYWRIGHT_CACHE.glob("chromium-*/chrome-linux*/chrome"))
    if not candidates:
        candidates = sorted(PLAYWRIGHT_CACHE.glob("chromium-*/chrome-linux*/headless_shell"))
    return None if not candidates else str(candidates[-1])


CHROMIUM = _chromium_executable()

requires_browser = pytest.mark.skipif(
    CHROMIUM is None,
    reason="no downloaded Chromium in ~/.cache/ms-playwright; the HTTP integration tests cover "
    "the same workflow deterministically",
)


@pytest.fixture
def no_network() -> None:
    """Override the suite's socket guard: this module *serves* on loopback on purpose.

    The override is deliberately narrow: the test below replaces the guard with a check that allows
    loopback and refuses everything else, so "the suite never talks to the network" still holds.
    """
    return None


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch: pytest.MonkeyPatch, no_network: None) -> None:
    """Let this module open loopback sockets, and nothing else."""
    original = socket.socket.connect

    def connect(self: socket.socket, address: object, *args: object, **kwargs: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return original(self, address, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("the browser acceptance may only talk to its own loopback server")

    monkeypatch.setattr(socket.socket, "connect", connect)


class _Server:
    """A real Uvicorn server for one test, on a loopback port the kernel chooses."""

    def __init__(self, app: object) -> None:
        import uvicorn

        self._config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server: object | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import uvicorn

        server = uvicorn.Server(self._config)
        self._server = server
        server.run()

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._socket() is not None:
                return self
            time.sleep(0.02)
        raise AssertionError("the test server did not start")

    def _socket(self) -> socket.socket | None:
        """Uvicorn's first listening socket, once it exists."""
        servers = getattr(self._server, "servers", None) or []
        sockets = getattr(servers[0], "sockets", None) if servers else None
        return None if not sockets else sockets[0]

    @property
    def port(self) -> int:
        bound = self._socket()
        assert bound is not None, "the server has no listening socket"
        return int(bound.getsockname()[1])

    def __exit__(self, *_: object) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True  # type: ignore[attr-defined]
        self.thread.join(timeout=10)


@requires_browser
def test_the_browser_chat_surface_end_to_end(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    # A slowed provider gives the browser a window in which a request is genuinely active,
    # which is exactly what the queue and Stop behaviour are about.
    stack = asyncio.run(build_chat(tmp_path, delay=0.8))
    with _Server(build_app(stack.dependencies(), is_private=is_private_client)) as server:
        session = asyncio.run(_issue_session(stack))
        base = f"http://127.0.0.1:{server.port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 420, "height": 780})
            context.add_cookies(
                [
                    _cookie(SESSION_COOKIE, session["session"]),
                    _cookie(CSRF_COOKIE, session["csrf"]),
                ]
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    errors.append(message.text) if message.type == "error" else None
                ),
            )

            _walk_the_product(stack, page, base)

            assert errors == []
            browser.close()


def _walk_the_product(stack: ChatStack, page, base: str) -> None:
    """The workflow the phase promises, one browser step at a time."""
    page.goto(f"{base}/chat")
    page.wait_for_selector("#composer")

    # 1. Send a message and watch it be understood and answered.
    stack.harness.queue(direct_reply("今天只有一件小事。"))
    page.fill("#input", "我今天有什么事？")
    page.click("#send")
    page.wait_for_function(
        "() => document.querySelectorAll('#messages .message.assistant').length === 1",
        timeout=20000,
    )
    assert "今天只有一件小事。" in page.inner_text("#messages")

    # 2. HTML-like text is text: it is visible, and it did not become markup.
    stack.harness.queue(direct_reply(XSS_PAYLOAD))
    page.fill("#input", XSS_PAYLOAD)
    page.click("#send")
    page.wait_for_function(
        "() => document.querySelectorAll('#messages .message.assistant').length === 2",
        timeout=20000,
    )
    assert page.query_selector("#messages script") is None
    assert XSS_PAYLOAD in page.inner_text("#messages")

    # 3. Shift+Enter breaks a line instead of sending.
    page.click("#input")
    page.keyboard.type("第一行")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("第二行")
    assert "\n" in page.input_value("#input")
    page.fill("#input", "")

    # 4. A fact proposal becomes a card, and the card confirms exactly one fact.
    stack.harness.queue(
        plan(
            operation(
                "fact.propose",
                {
                    "key": "profile.office",
                    "value": "浏览器测试地点",
                    "correction_text": "记住我的办公室在浏览器测试地点",
                },
            )
        )
    )
    page.fill("#input", "记住我的办公室在浏览器测试地点")
    page.click("#send")
    page.wait_for_selector(".card", timeout=20000)
    assert "浏览器测试地点" in page.inner_text(".card")
    page.click(".card button.primary")
    page.wait_for_function(
        "() => document.querySelectorAll('.card').length === 0", timeout=20000
    )
    assert _confirmed_facts(stack) == 1

    # 5. Reload reconstructs durable state instead of replaying anything.
    messages_before = page.inner_text("#messages")
    page.reload()
    page.wait_for_selector("#messages .message", timeout=20000)
    assert page.inner_text("#messages") == messages_before
    assert page.query_selector(".card") is None

    # 6. While Tree is understanding a message, the activity line says so in product language.
    stack.harness.queue(direct_reply("慢一点的回答。"))
    page.fill("#input", "第一条消息")
    page.click("#send")
    page.wait_for_function(
        "() => !document.getElementById('activity').hidden", timeout=20000
    )
    assert "正在理解你的请求" in page.inner_text("#activity")

    # 7. A message sent while one is running is queued — visibly, and cancellably.
    stack.harness.queue(direct_reply("不该出现的回答。"))
    page.fill("#input", "第二条消息")
    page.click("#send")
    page.wait_for_selector(".queued", timeout=20000)
    assert "排队中" in page.inner_text("#queue")
    assert "第二条消息" in page.inner_text("#queue")
    page.click(".queued button")
    page.wait_for_function(
        "() => document.querySelectorAll('.queued').length === 0", timeout=20000
    )
    page.wait_for_function(
        "() => document.querySelectorAll('#messages .message.assistant').length === 4",
        timeout=20000,
    )
    assert "第二条消息" not in page.inner_text("#messages")


def _confirmed_facts(stack: ChatStack) -> int:
    import sqlite3

    connection = sqlite3.connect(str(stack.database.path))
    try:
        return connection.execute("SELECT COUNT(*) FROM confirmed_facts").fetchone()[0]
    finally:
        connection.close()


def _cookie(name: str, value: str) -> dict[str, object]:
    return {"name": name, "value": value, "url": "http://127.0.0.1"}


async def _issue_session(stack: ChatStack) -> dict[str, str]:
    """Mint a session the way `pw mobile pair` + `POST /api/pair` do, without a browser login."""
    issued = await stack.mobile.auth.create_pairing_token()
    session = await stack.mobile.auth.pair(issued.token)
    return {"session": session.session_token, "csrf": session.csrf_token}


__all__ = ["CHROMIUM", "requires_browser"]
