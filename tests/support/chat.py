"""A real Tree chat surface on a real database, with only the provider and SMTP replaced.

The chat stack is built through `bootstrap.conversation_chat_service`, so the tests exercise the
same composition the daemon builds: one conversation runtime, one durable request queue, one
notification broker, and the card service on top of them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.adapters.web.app import CSRF_COOKIE, SESSION_COOKIE, build_app
from assistant.adapters.web.server import is_private_client
from assistant.application.conversation_chat import ConversationChatService
from assistant.domain.action import ActionType
from assistant.domain.model import ModelRequest, ModelResponse
from tests.support.conversation import ConversationHarness, build_harness
from tests.support.mobile import MobileStack, ScriptedTokenFactory

WAIT_SECONDS = 5.0
"""How long a test will wait for a background conversation worker before failing loudly."""


class DelayedModelAdapter:
    """A `ModelPort` that waits before answering, so a test can act while a turn is understood."""

    def __init__(self, inner: FakeModelAdapter, *, delay: float = 0.0) -> None:
        self._inner = inner
        self.delay = delay
        self.arrived = 0

    @property
    def model(self) -> str:
        """The wrapped adapter's model name."""
        return self._inner.model

    @property
    def requests(self) -> list:
        """Every request the scripted adapter was given, for "no model call" assertions."""
        return self._inner.requests

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Wait, then hand the request to the scripted adapter."""
        self.arrived += 1
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        return await self._inner.complete(request)


@dataclass
class ChatStack:
    """Everything a chat-surface test needs: real services, a scripted provider, an ASGI client."""

    harness: ConversationHarness
    tokens: ScriptedTokenFactory = field(default_factory=ScriptedTokenFactory)
    model: DelayedModelAdapter | None = None
    chat: ConversationChatService | None = None
    client_instance: TestClient | None = None

    @property
    def database(self):
        """The runtime database under test."""
        return self.harness.database

    @property
    def clock(self):
        """The fixed clock under test."""
        return self.harness.clock

    def wire(self, *, delay: float = 0.0) -> ChatStack:
        """Build the chat surface over the harness's runtime, sharing its scripted provider."""
        self.model = DelayedModelAdapter(self.harness.model, delay=delay)
        self.chat = bootstrap.conversation_chat_service(
            self.harness.database,
            self.harness.clock,
            self.harness.config,
            model=self.model,
            executors={ActionType("mail.send"): self.harness.executor},
        )
        return self

    @property
    def surface(self) -> ConversationChatService:
        """The chat service, once `wire` has built it."""
        assert self.chat is not None, "call wire() first"
        return self.chat

    @property
    def coordinator(self):
        """The durable queue's coordinator."""
        return self.surface.coordinator

    @property
    def mobile(self) -> MobileStack:
        """The control-plane stack, for the non-chat dependencies of the same app."""
        return MobileStack(self.harness.database, self.harness.clock, self.tokens)

    def dependencies(self):
        """The web dependencies, with the chat surface mounted."""
        base = self.mobile.dependencies()
        return type(base)(
            auth=base.auth,
            tasks=base.tasks,
            cases=base.cases,
            drafts=base.drafts,
            actions=base.actions,
            approvals=base.approvals,
            notifications=base.notifications,
            deadlines=base.deadlines,
            clock=base.clock,
            chat=self.surface,
        )

    def client(self, *, is_private: Callable[[str], bool] | None = None) -> TestClient:
        """An ASGI client over the real app.

        Use it as a context manager: entering it starts one persistent event loop for the whole
        block, which is what lets a queued conversation worker keep running between requests. A
        client that is not entered gets a fresh loop per request, and background work would stop
        at the first await — the same trap a real deployment never has, because Uvicorn owns one
        loop for the process lifetime.
        """
        self.client_instance = TestClient(
            build_app(self.dependencies(), is_private=is_private or is_private_client),
            client=("127.0.0.1", 41100),
        )
        return self.client_instance

    async def pair(self, client: TestClient) -> dict[str, str]:
        """Pair this browser, exactly as `pw mobile pair` plus `POST /api/pair` would.

        The pairing code is minted by the real service and redeemed through the real endpoint, so
        the session and CSRF values under test are the ones the product issues.
        """
        issued = await self.mobile.auth.create_pairing_token()
        response = client.post("/api/pair", json={"token": issued.token})
        assert response.status_code == 200, response.text
        return {
            "session": client.cookies.get(SESSION_COOKIE),
            "csrf": client.cookies.get(CSRF_COOKIE),
        }

    def headers(self, csrf: str) -> dict[str, str]:
        """The CSRF header a mutation must carry."""
        return MobileStack.csrf_headers(csrf)

    # ------------------------------------------------------------------ helpers

    def wait_until(self, predicate: Callable[[], bool], *, timeout: float = WAIT_SECONDS) -> bool:
        """Poll `predicate` until it holds, letting the background workers make progress."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    async def wait_until_async(
        self, predicate: Callable[[], Awaitable[bool]], *, timeout: float = WAIT_SECONDS
    ) -> bool:
        """An async version, for the tests whose assertions have to read the database."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await predicate():
                return True
            await asyncio.sleep(0.005)
        return await predicate()

    def requests_repository(self):
        """The durable queue, as the composition root builds it."""
        return bootstrap.conversation_request_repository(self.database)

    async def request_row(self, request_id: str):
        """One durable request row, read straight from the store."""
        return await self.requests_repository().get(UUID(request_id))

    async def requests_for(self, thread_id: str):
        """Every request row of one thread, newest first."""
        return await self.requests_repository().list_for_thread(UUID(thread_id))


async def build_chat(tmp_path: Path, *, delay: float = 0.0, **kwargs: Any) -> ChatStack:
    """Build the harness and wire the chat surface over it.

    `delay` makes the scripted provider slow, so a test can act while a turn is still being
    understood — the one window in which Stop is both possible and safe.
    """
    harness = await build_harness(tmp_path, **kwargs)
    return ChatStack(harness=harness).wire(delay=delay)


__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "WAIT_SECONDS",
    "ChatStack",
    "DelayedModelAdapter",
    "build_chat",
]
