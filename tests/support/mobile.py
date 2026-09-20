"""Helpers for the mobile control-plane tests: real stores, scripted tokens, an ASGI client.

The web layer is exercised through Starlette's own test client, so no socket is ever opened; the
"private client" decision is injected, which is also how the tests prove that a public peer is
refused whatever it claims in a header.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from assistant.adapters.web.app import CSRF_COOKIE, SESSION_COOKIE, WebDependencies, build_app
from assistant.adapters.web.server import is_private_client
from assistant.application.action_service import ActionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.mail_context import MailContextBuilder
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mobile_auth import MobileAuthService
from assistant.application.task_service import TaskService
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from assistant.store.mobile_sessions import SqliteMobileSessionRepository
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock

PAIRING_TOKEN = "PAIRING-TOKEN-FOR-TESTS-0123456789ABCDEF"
SESSION_TOKEN = "SESSION-TOKEN-FOR-TESTS-0123456789ABCDEF"
CSRF_TOKEN = "CSRF-TOKEN-FOR-TESTS-0123456789ABCDEFGH"
SECOND_PAIRING_TOKEN = "PAIRING-TOKEN-FOR-TESTS-9876543210FEDCBA"
SECOND_SESSION_TOKEN = "SESSION-TOKEN-FOR-TESTS-9876543210FEDCBA"
SECOND_CSRF_TOKEN = "CSRF-TOKEN-FOR-TESTS-9876543210HGFEDC"


class ScriptedTokenFactory:
    """A token factory that hands out a known sequence, then keeps producing unique values."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self._tokens = list(tokens or [PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN])
        self._counter = itertools.count(1)
        self.issued: list[str] = []

    def __call__(self) -> str:
        """Return the next scripted token, or a unique generated one."""
        token = (
            self._tokens.pop(0)
            if self._tokens
            else f"GENERATED-TOKEN-{next(self._counter):04d}-0123456789ABCDEF"
        )
        self.issued.append(token)
        return token


class _KnowledgeStub:
    """A knowledge source that is never called: the mobile path must not retrieve anything."""

    async def build(self, question: str, *, root_id: str | None = None, limit: int = 8):
        raise AssertionError("the mobile path must not retrieve knowledge")


@dataclass
class MobileStack:
    """Everything the mobile tests need, over one real database."""

    database: Database
    clock: FakeClock
    tokens: ScriptedTokenFactory

    def __post_init__(self) -> None:
        self.commitments = SqliteCommitmentRepository(self.database)
        self.cases_repository = SqliteCaseRepository(self.database)
        self.actions_repository = SqliteActionRepository(self.database)
        self.drafts_repository = SqliteMailDraftRepository(self.database)
        self.sessions_repository = SqliteMobileSessionRepository(self.database)
        self.scheduler_repository = SqliteSchedulerRepository(self.database)
        self.mail_repository = SqliteMailRepository(self.database)
        self.intelligence_repository = SqliteMailIntelligenceRepository(self.database)

    @property
    def auth(self) -> MobileAuthService:
        """The auth service under test."""
        return MobileAuthService(
            self.sessions_repository,
            self.clock,
            token_factory=self.tokens,
            enabled=True,
        )

    @property
    def tasks(self) -> TaskService:
        """Tasks, with no reminder offsets and no replan hook."""
        return TaskService(self.commitments, self.clock)

    @property
    def cases(self) -> CaseService:
        """Cases over the real stores."""
        return CaseService(self.cases_repository, self.actions_repository, self.clock)

    @property
    def drafts(self) -> MailDraftService:
        """Drafts, without a provider: reading and editing need none."""
        return MailDraftService(
            self.mail_repository,
            self.intelligence_repository,
            self.drafts_repository,
            MailContextBuilder(self.mail_repository, self.intelligence_repository),
            _KnowledgeStub(),  # type: ignore[arg-type]
            None,
            self.clock,
        )

    @property
    def actions(self) -> ActionService:
        """Read-only action views."""
        return ActionService(self.actions_repository, self.clock)

    @property
    def approvals(self) -> ApprovalService:
        """The approval service the web surface is allowed to reach."""
        return ApprovalService(
            self.actions_repository,
            self.clock,
            token_factory=self.tokens,
        )

    async def deadlines(self, tasks: Any) -> dict[Any, Any]:
        """The read model the dashboard uses for due dates."""
        identifiers = [task.id for task in tasks]
        if not identifiers:
            return {}
        return await self.commitments.list_deadlines(identifiers)

    def dependencies(self) -> WebDependencies:
        """The application services the control plane may speak to."""
        return WebDependencies(
            auth=self.auth,
            tasks=self.tasks,
            cases=self.cases,
            drafts=self.drafts,
            actions=self.actions,
            approvals=self.approvals,
            notifications=self.scheduler_repository,
            deadlines=self.deadlines,
            clock=self.clock,
        )

    def client(
        self, *, is_private: Callable[[str], bool] | None = None
    ) -> TestClient:
        """An ASGI test client over the real app, with an injectable private-client test."""
        app = build_app(
            self.dependencies(), is_private=is_private or is_private_client
        )
        return TestClient(app, client=("127.0.0.1", 41000))

    def issue_pairing(self) -> str:
        """Mint a pairing code the way `pw mobile pair` does, and return it once."""
        import asyncio

        issued = asyncio.run(self.auth.create_pairing_token())
        return issued.token

    def pair(self, client: TestClient) -> dict[str, str]:
        """Issue a code, redeem it through the API, and return the session's tokens.

        The pairing code is created first because that is the real flow: the host mints it, the
        phone posts it. Nothing here touches a network.
        """
        code = self.issue_pairing()
        response = client.post("/api/pair", json={"token": code})
        assert response.status_code == 200, response.text
        return {
            "session": client.cookies.get(SESSION_COOKIE),
            "csrf": client.cookies.get(CSRF_COOKIE),
        }

    @staticmethod
    def csrf_headers(csrf: str) -> dict[str, str]:
        """The header a browser must send alongside the cookie for a mutation."""
        return {"X-CSRF-Token": csrf}


__all__ = [
    "CSRF_TOKEN",
    "PAIRING_TOKEN",
    "SECOND_CSRF_TOKEN",
    "SECOND_PAIRING_TOKEN",
    "SECOND_SESSION_TOKEN",
    "SESSION_TOKEN",
    "MobileStack",
    "ScriptedTokenFactory",
]
