"""The mobile workflow end to end: pair, read, create, complete, edit, approve (ADR-0026).

Real SQLite, the real application services, the real ASGI app. What is under test is the promise
the control plane makes: every mutation reaches an existing service, approval is the Phase 6A
approval, and **nothing executes** — no SMTP call, no eHall submit, no execution run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from assistant.domain.case import Case
from assistant.domain.mail_draft import MailDraft
from assistant.domain.task import Task
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_drafts import build_draft
from tests.support.mobile import MobileStack, ScriptedTokenFactory

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def stack(database: Database, clock: FakeClock) -> MobileStack:
    return MobileStack(database, clock, ScriptedTokenFactory())


async def _reply_target(stores: MobileStack) -> UUID:
    """A stored inbound message, so a draft has a real reply target to point at."""
    from tests.support.mail_intelligence import MailStores, build_message

    message = await MailStores(stores.database, stores.clock).store(
        build_message(
            message_id_header="<original@example.edu>",
            from_address="ada@example.edu",
            subject="SE lab deadline",
            body_text="Could you submit the report by Friday?",
            at=NOW,
        )
    )
    return message.id


async def _seed(stores: MobileStack) -> tuple[Task, Case, MailDraft]:
    """One open task, one open case with a prepared action, and one draft."""
    task = Task(
        title="Write the SE lab report",
        estimated_minutes=120,
        created_at=NOW,
        updated_at=NOW,
    )
    await stores.commitments.add_task(task)
    case = Case(title="Register for the course", created_at=NOW, updated_at=NOW)
    await stores.cases_repository.add_case(case)
    await stores.cases.prepare_action(
        case.id, "mail.send", {"to": "ada@example.edu", "body": "Please register me."}
    )
    draft = build_draft(
        reply_to_message_id=await _reply_target(stores),
        subject="Re: SE lab deadline",
        body_text="Dear Ada, Friday works.",
        at=NOW,
    )
    await stores.drafts_repository.add_draft(draft)
    return task, case, draft


def test_the_mobile_workflow_from_pairing_to_approval(stack: MobileStack) -> None:
    task, case, draft = asyncio.run(_seed(stack))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    # 1. the dashboard shows real progress
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    body = dashboard.json()
    assert body["open_task_count"] == 1
    assert body["open_tasks"][0]["title"] == "Write the SE lab report"
    assert body["open_tasks"][0]["estimated_minutes"] == 120
    assert body["open_cases"][0]["title"] == "Register for the course"
    assert body["prepared_actions"][0]["type"] == "mail.send"
    assert body["recent_drafts"][0]["subject"] == "Re: SE lab deadline"

    # 2. create a task through TaskService
    created = client.post(
        "/api/tasks",
        json={
            "title": "Book a library room",
            "priority": "high",
            "estimated_minutes": 30,
            "deadline": "2026-09-30T23:59:00+08:00",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["priority"] == "high"
    assert created.json()["deadline"].startswith("2026-09-30")
    assert client.get("/api/dashboard").json()["open_task_count"] == 2

    # ...and complete one, with the CAS semantics TaskService already has
    completed = client.post(f"/api/tasks/{task.id}/complete", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert client.get("/api/dashboard").json()["open_task_count"] == 1

    # 3. read and edit the draft, with optimistic concurrency
    listed = client.get("/api/mail/drafts")
    assert listed.json()["drafts"][0]["version"] == 1
    edited = client.patch(
        f"/api/mail/drafts/{draft.id}",
        json={"body": "Dear Ada, Monday works better.", "expected_version": 1},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    stale = client.patch(
        f"/api/mail/drafts/{draft.id}",
        json={"body": "Something else.", "expected_version": 1},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["current_version"] == 2
    stored = asyncio.run(stack.drafts_repository.get_draft(draft.id))
    assert stored is not None and stored.version == 2

    # 4. review the exact action
    actions = client.get("/api/actions").json()["actions"]
    assert len(actions) == 1
    action_id = actions[0]["id"]
    detail = client.get(f"/api/actions/{action_id}")
    assert detail.status_code == 200
    assert detail.json()["type"] == "mail.send"
    assert detail.json()["payload"]["body"] == "Please register me."
    assert detail.json()["execution"] == "none"
    assert case.id == UUID(actions[0]["case_id"])

    # 5. challenge and approve — and stop there
    challenge = client.post(f"/api/actions/{action_id}/challenge", headers=headers)
    assert challenge.status_code == 200, challenge.text
    assert challenge.json()["fingerprint"] == actions[0]["fingerprint"]
    approved = client.post(
        f"/api/actions/{action_id}/approve",
        json={"token": challenge.json()["token"]},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved"] is True
    assert approved.json()["executed"] is False


def test_mobile_approval_creates_an_approval_and_executes_nothing(
    stack: MobileStack,
) -> None:
    asyncio.run(_seed(stack))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])
    action_id = client.get("/api/actions").json()["actions"][0]["id"]

    challenge = client.post(f"/api/actions/{action_id}/challenge", headers=headers)
    assert challenge.status_code == 200
    approved = client.post(
        f"/api/actions/{action_id}/approve",
        json={"token": challenge.json()["token"]},
        headers=headers,
    )

    assert approved.status_code == 200
    action = asyncio.run(stack.actions_repository.get_action(UUID(action_id)))
    assert action is not None
    from assistant.domain.action import ActionRequestStatus

    assert action.status is ActionRequestStatus.PREPARED  # not executed
    approvals = asyncio.run(stack.actions_repository.list_approvals(UUID(action_id)))
    assert len(approvals) == 1
    assert approvals[0].action_fingerprint == action.fingerprint
    assert asyncio.run(stack.actions_repository.count_executions(UUID(action_id))) == 0


def test_the_approval_flow_uses_the_existing_approval_service(
    stack: MobileStack,
) -> None:
    """There is no web-only approval model: the fingerprint binding is Phase 6A's."""
    asyncio.run(_seed(stack))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])
    action_id = client.get("/api/actions").json()["actions"][0]["id"]
    action = asyncio.run(stack.actions_repository.get_action(UUID(action_id)))
    assert action is not None

    challenge = client.post(f"/api/actions/{action_id}/challenge", headers=headers)
    wrong = client.post(
        f"/api/actions/{action_id}/approve",
        json={"token": "not-the-token-0123456789ABCDEF"},
        headers=headers,
    )
    reused = client.post(
        f"/api/actions/{action_id}/approve",
        json={"token": challenge.json()["token"]},
        headers=headers,
    )
    again = client.post(
        f"/api/actions/{action_id}/approve",
        json={"token": challenge.json()["token"]},
        headers=headers,
    )

    assert wrong.status_code == 409
    assert reused.status_code == 200
    assert again.status_code == 409  # a challenge is single use
    assert asyncio.run(stack.actions_repository.count_executions(action.id)) == 0


def test_a_completed_task_can_no_longer_be_completed(stack: MobileStack) -> None:
    task, _, _ = asyncio.run(_seed(stack))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    first = client.post(f"/api/tasks/{task.id}/complete", headers=headers)
    second = client.post(f"/api/tasks/{task.id}/complete", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 409  # the domain refuses an illegal transition


def test_editing_a_draft_that_has_questions_resets_acknowledgement(
    stack: MobileStack,
) -> None:
    """The mobile path cannot bypass the acknowledgement rule Phase 6C depends on."""
    draft = build_draft(
        reply_to_message_id=asyncio.run(_reply_target(stack)),
        subject="Re: deadline",
        needs_user_input=("Which course code?",),
        at=NOW,
    )
    acknowledged = draft.acknowledge_user_input(NOW)
    asyncio.run(stack.drafts_repository.add_draft(acknowledged))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    edited = client.patch(
        f"/api/mail/drafts/{draft.id}",
        json={"body": "A new body.", "expected_version": acknowledged.version},
        headers=headers,
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["needs_user_input_acknowledged"] is False
    assert edited.json()["needs_user_input"] == ["Which course code?"]


def test_notifications_can_be_listed_and_marked_read(stack: MobileStack) -> None:
    from assistant.domain.notification import Notification, NotificationKind

    notification = Notification(
        kind=NotificationKind.DEADLINE_REMINDER,
        title="Deadline approaching",
        body="Write the SE lab report",
        dedup_key="deadline-1",
        created_at=NOW,
    )
    asyncio.run(stack.scheduler_repository.create_notification_idempotent(notification))
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    listed = client.get("/api/notifications")
    assert listed.json()["notifications"][0]["status"] == "unread"
    marked = client.post(
        f"/api/notifications/{notification.id}/read", headers=headers
    )

    assert marked.status_code == 200, marked.text
    assert marked.json()["status"] == "read"
    assert client.get("/api/dashboard").json()["unread_notification_count"] == 0


# --------------------------------------------------------------- approval link


def test_an_approval_link_previews_and_approves_without_a_session(
    stack: MobileStack,
) -> None:
    asyncio.run(_seed(stack))
    client = stack.client()
    action_id = client.get("/api/me").status_code  # unauthenticated: 401
    assert action_id == 401
    actions = asyncio.run(stack.actions_repository.list_actions(limit=None))
    action = actions[0]
    issued = asyncio.run(stack.approvals.create_challenge(action.id))

    preview = client.post(
        "/api/approval-link/preview",
        json={"action_id": str(action.id), "token": issued.token},
    )
    approved = client.post(
        "/api/approval-link/approve",
        json={"action_id": str(action.id), "token": issued.token},
    )
    again = client.post(
        "/api/approval-link/approve",
        json={"action_id": str(action.id), "token": issued.token},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["action"]["fingerprint"] == action.fingerprint
    assert preview.json()["action"]["payload"]["body"] == "Please register me."
    assert approved.status_code == 200
    assert approved.json()["executed"] is False
    assert again.status_code == 403  # single use
    assert asyncio.run(stack.actions_repository.count_executions(action.id)) == 0


@pytest.mark.parametrize(
    "token", ["not-a-token-0123456789ABCDEFGH", "", "   "]
)
def test_a_wrong_approval_link_token_previews_nothing(
    stack: MobileStack, token: str
) -> None:
    asyncio.run(_seed(stack))
    client = stack.client()
    action = asyncio.run(stack.actions_repository.list_actions(limit=None))[0]
    asyncio.run(stack.approvals.create_challenge(action.id))

    response = client.post(
        "/api/approval-link/preview",
        json={"action_id": str(action.id), "token": token},
    )

    assert response.status_code == 403
    assert asyncio.run(stack.actions_repository.list_approvals(action.id)) == []


def test_an_expired_approval_link_previews_nothing(
    stack: MobileStack, clock: FakeClock
) -> None:
    from assistant.domain.approval import approval_ttl

    asyncio.run(_seed(stack))
    client = stack.client()
    action = asyncio.run(stack.actions_repository.list_actions(limit=None))[0]
    issued = asyncio.run(stack.approvals.create_challenge(action.id))
    clock.advance(approval_ttl().total_seconds())

    response = client.post(
        "/api/approval-link/preview",
        json={"action_id": str(action.id), "token": issued.token},
    )

    assert response.status_code == 403


def test_an_approval_link_for_another_action_is_refused(stack: MobileStack) -> None:
    """The token is scoped to one action: presenting it for a different one proves nothing."""
    asyncio.run(_seed(stack))
    client = stack.client()
    case = asyncio.run(stack.cases.create_case("Second errand"))
    other = asyncio.run(
        stack.cases.prepare_action(case.id, "mail.send", {"to": "b@example.edu"})
    )
    action = asyncio.run(stack.actions_repository.list_actions(limit=None))
    first = next(item for item in action if item.id != other.id)
    issued = asyncio.run(stack.approvals.create_challenge(first.id))

    response = client.post(
        "/api/approval-link/approve",
        json={"action_id": str(other.id), "token": issued.token},
    )

    assert response.status_code == 403
    assert asyncio.run(stack.actions_repository.list_approvals(other.id)) == []


def test_the_web_surface_has_no_execution_route(stack: MobileStack) -> None:
    """Every route is pinned, and none of them can execute, send or submit."""
    from assistant.adapters.web.app import build_app

    app = build_app(stack.dependencies(), is_private=lambda peer: True)
    routes = {route.path for route in app.routes}  # type: ignore[attr-defined]

    assert "/api/actions/{reference}/approve" in routes
    for forbidden in ("execute", "send", "submit", "retry", "resend", "draft/create"):
        assert not [path for path in routes if forbidden in path], forbidden
    # The API surface is pinned exactly: adding a route is a reviewed change, not a side effect.
    assert sorted(path for path in routes if path.startswith("/api/")) == [
        "/api/actions",
        "/api/actions/{reference}",
        "/api/actions/{reference}/approve",
        "/api/actions/{reference}/challenge",
        "/api/approval-link/approve",
        "/api/approval-link/preview",
        "/api/cases",
        "/api/dashboard",
        "/api/logout",
        "/api/mail/drafts",
        "/api/mail/drafts/{reference}",
        "/api/me",
        "/api/notifications",
        "/api/notifications/{reference}/read",
        "/api/pair",
        "/api/settings/planning",
        "/api/tasks",
        "/api/tasks/{reference}/complete",
    ]


# ------------------------------------------------------- mutations stay narrow


def test_a_draft_edit_cannot_touch_the_recipient(stack: MobileStack) -> None:
    """The phone may rewrite a sentence. Who the mail goes to is not its to change."""
    draft = build_draft(
        reply_to_message_id=asyncio.run(_reply_target(stack)),
        subject="Re: SE lab deadline",
        body_text="Dear Ada, Friday works.",
        at=NOW,
    )
    asyncio.run(stack.drafts_repository.add_draft(draft))
    client = stack.client()
    tokens = stack.pair(client)

    edited = client.patch(
        f"/api/mail/drafts/{draft.id}",
        json={
            "subject": "Re: SE lab deadline (again)",
            "body": "Dear Ada, Monday works.",
            "expected_version": draft.version,
            # None of these are part of the mobile edit vocabulary.
            "to_addresses": ["attacker@example.com"],
            "account_id": "somewhere-else",
            "sources": ["invented-source"],
            "origin": "manual",
            "needs_user_input": [],
        },
        headers=stack.csrf_headers(tokens["csrf"]),
    )

    assert edited.status_code == 200, edited.text
    stored = asyncio.run(stack.drafts_repository.get_draft(draft.id))
    assert stored is not None
    assert stored.to_addresses == draft.to_addresses
    assert stored.account_id == draft.account_id
    # The provenance is the edit's own doing, not the client's claim.
    assert stored.origin.value == "user_edited"
    assert stored.needs_user_input == draft.needs_user_input
    assert stored.subject == "Re: SE lab deadline (again)"
    assert stored.body_text == "Dear Ada, Monday works."


def test_task_creation_is_typed_and_not_a_sentence(stack: MobileStack) -> None:
    """There is no interpreter on the mobile path: a task is fields, and a field is checked."""
    client = stack.client()
    tokens = stack.pair(client)
    headers = stack.csrf_headers(tokens["csrf"])

    no_title = client.post(
        "/api/tasks",
        json={"text": "remind me to hand in the report on Friday"},
        headers=headers,
    )
    bad_priority = client.post(
        "/api/tasks", json={"title": "x", "priority": "whenever"}, headers=headers
    )
    naive_deadline = client.post(
        "/api/tasks",
        json={"title": "x", "deadline": "2026-09-30T23:59:00"},
        headers=headers,
    )
    bad_estimate = client.post(
        "/api/tasks", json={"title": "x", "estimated_minutes": "soon"}, headers=headers
    )

    assert no_title.status_code == 400
    assert bad_priority.status_code == 400
    assert naive_deadline.status_code == 400  # local time is never assumed
    assert bad_estimate.status_code == 400
    assert client.get("/api/tasks").json()["tasks"] == []
