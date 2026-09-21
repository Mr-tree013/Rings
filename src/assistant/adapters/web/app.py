"""The mobile HTTP surface: a narrow, authenticated, same-LAN control plane (ADR-0026).

```text
GET  /                      the dashboard shell (static)
GET  /pair, /approve/<id>   static shells; the token travels in a URL *fragment*
GET  /api/me, /api/dashboard, /api/tasks, /api/cases, /api/notifications,
     /api/mail/drafts[/{id}], /api/actions[/{id}]
POST /api/pair, /api/logout, /api/tasks, /api/tasks/{id}/complete, /api/notifications/{id}/read
PATCH /api/mail/drafts/{id}
POST /api/actions/{id}/challenge, /api/actions/{id}/approve
POST /api/approval-link/preview, /api/approval-link/approve     (token as capability)
```

What is deliberately absent matters as much as what is present: there is **no execute endpoint**, no
endpoint that creates an arbitrary action, no endpoint that reaches SMTP or eHall, and no route that
calls a model. `/docs`, `/redoc` and `/openapi.json` are disabled, so the surface is exactly the
list above.

Every response carries a restrictive CSP, every mutating route requires the session cookie *plus*
the CSRF header *and* cookie, and all user-controlled text reaches the page through `textContent`
— never through `innerHTML`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from assistant.adapters.web.chat import register_chat_routes
from assistant.application.action_service import ActionService, ApprovalState
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.conversation_chat import ConversationChatService
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mobile_auth import MobileAuthService
from assistant.application.task_service import CreateTask, TaskService
from assistant.domain.errors import (
    ActionRequestNotFound,
    AmbiguousId,
    ApprovalAlreadyOutstanding,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalChallengeNotFound,
    ApprovalUnavailable,
    DomainError,
    InvalidApprovalToken,
    InvalidMailDraft,
    InvalidTask,
    MailDraftNotFound,
    MobileCsrfRejected,
    MobileDisabled,
    MobilePairingTokenInvalid,
    MobileSessionInvalid,
    StaleMailDraftUpdate,
    TaskNotFound,
)
from assistant.domain.mobile import MobileWebSession
from assistant.domain.task import TaskPriority

STATIC_DIR = Path(__file__).resolve().parent / "static"

SESSION_COOKIE = "ga_mobile_session"
CSRF_COOKIE = "ga_mobile_csrf"
CSRF_HEADER = "x-csrf-token"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True, slots=True)
class WebDependencies:
    """The application services the control plane may speak to.

    Read the list as a permission list: tasks, cases, drafts, actions, approvals, notifications and
    auth — and nothing that could execute, send or submit.
    """

    auth: MobileAuthService
    tasks: TaskService
    cases: CaseService
    drafts: MailDraftService
    actions: ActionService
    approvals: ApprovalService
    notifications: Any
    deadlines: Any
    """`async (tasks) -> {task_id: Deadline}`: the read model the CLI already uses."""
    clock: Any
    chat: ConversationChatService | None = None
    """The Tree chat surface, when this host has a conversation runtime to offer.

    `None` means the routes are not registered at all: a browser shell that could not answer
    anything would be a worse promise than an honest absence.
    """

    attention: Any = None
    """`AttentionService`: read and settle the unified inbox.

    It can list, acknowledge and dismiss. It cannot execute, approve, send or submit anything, and
    there is no route below that would let it (ADR-0042 §21).
    """


class _PrivateClientMiddleware(BaseHTTPMiddleware):
    """Refuse anything that is not loopback or a private address.

    The decision uses the *socket peer* only. `X-Forwarded-For`, `Forwarded` and `X-Real-IP` are
    never consulted: on a LAN, a client that can set a header can claim anything.
    """

    def __init__(self, app: Any, *, is_private: Callable[[str], bool]) -> None:
        super().__init__(app)
        self._is_private = is_private

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        peer = request.client.host if request.client is not None else ""
        if not self._is_private(peer):
            return _json_error(
                403, "the mobile control plane is only reachable from the local network"
            )
        return await call_next(request)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the security headers to every response, including error responses."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


def build_app(
    dependencies: WebDependencies,
    *,
    is_private: Callable[[str], bool],
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the ASGI app. The caller supplies the services and the private-client test."""
    app = FastAPI(
        title="growing-assistant mobile",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_PrivateClientMiddleware, is_private=is_private)
    assets = static_dir or STATIC_DIR
    _register_static_routes(app, assets)
    _register_session_routes(app, dependencies)
    _register_read_routes(app, dependencies)
    _register_write_routes(app, dependencies)
    _register_approval_routes(app, dependencies)
    _register_approval_link_routes(app, dependencies)
    if dependencies.chat is not None:
        _register_chat_routes(app, dependencies, assets)
    return app


def _register_chat_routes(
    app: FastAPI, dependencies: WebDependencies, assets: Path
) -> None:
    """Mount the Tree chat surface behind the control plane's existing session and CSRF pair."""
    chat = dependencies.chat
    assert chat is not None  # the caller only reaches here when it exists

    async def _session(request: Request) -> MobileWebSession | JSONResponse:
        return await _require_session(dependencies, request)

    async def _mutation(request: Request) -> MobileWebSession | JSONResponse:
        return await _require_mutation(dependencies, request)

    register_chat_routes(
        app,
        chat=lambda: chat,
        attention=(
            None
            if dependencies.attention is None
            else lambda: dependencies.attention
        ),
        assets=assets,
        require_session=_session,
        require_mutation=_mutation,
    )


def _register_static_routes(app: FastAPI, assets: Path) -> None:
    """The four self-contained assets. No CDN, no third-party script, no build step."""

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(assets / "index.html", media_type="text/html")

    @app.get("/pair")
    async def pair_page() -> FileResponse:
        return FileResponse(assets / "pair.html", media_type="text/html")

    @app.get("/approve/{action_id}")
    async def approve_page(action_id: str) -> FileResponse:
        del action_id
        return FileResponse(assets / "approve.html", media_type="text/html")

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(assets / "app.js", media_type="application/javascript")

    @app.get("/styles.css")
    async def styles() -> FileResponse:
        return FileResponse(assets / "styles.css", media_type="text/css")


def _register_session_routes(app: FastAPI, deps: WebDependencies) -> None:
    @app.post("/api/pair")
    async def pair(request: Request) -> JSONResponse:
        """Redeem a one-time code for a session cookie and a CSRF cookie."""
        body = await _json_body(request)
        code = body.get("token")
        if not isinstance(code, str):
            return _json_error(400, "a pairing code is required")
        try:
            issued = await deps.auth.pair(code)
        except (MobileDisabled, MobilePairingTokenInvalid) as exc:
            return _json_error(403, str(exc))
        response = JSONResponse({"paired": True})
        _set_cookie(response, SESSION_COOKIE, issued.session_token)
        _set_cookie(response, CSRF_COOKIE, issued.csrf_token)
        return response

    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        return JSONResponse(
            {
                "authenticated": True,
                "created_at": session.created_at.isoformat(),
                "expires_at": session.expires_at.isoformat(),
            }
        )

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        await deps.auth.logout(session)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response


def _register_read_routes(app: FastAPI, deps: WebDependencies) -> None:
    """Read-only views. None of them mutates anything, and none calls a model."""

    @app.get("/api/dashboard")
    async def dashboard(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        return JSONResponse(await _dashboard_payload(deps))

    @app.get("/api/tasks")
    async def tasks(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        listed = await deps.tasks.list_tasks(include_terminal=False)
        deadlines = await deps.deadlines(listed)
        return JSONResponse(
            {
                "tasks": [
                    _task_payload(task, deadlines.get(task.id)) for task in listed[:50]
                ]
            }
        )

    @app.get("/api/cases")
    async def cases(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        listed = await deps.cases.list_cases(statuses=None, limit=50)
        return JSONResponse(
            {
                "cases": [
                    {
                        "id": str(case.id),
                        "title": case.title,
                        "status": case.status.value,
                        "updated_at": case.updated_at.isoformat(),
                    }
                    for case in listed
                ]
            }
        )

    @app.get("/api/notifications")
    async def notifications(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        listed = await deps.notifications.list_notifications(limit=50)
        return JSONResponse(
            {"notifications": [_notification_payload(item) for item in listed]}
        )

    @app.get("/api/mail/drafts")
    async def drafts(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        listed = await deps.drafts.list_drafts(limit=20)
        return JSONResponse(
            {
                "drafts": [
                    {
                        "id": str(entry.draft.id),
                        "subject": entry.draft.subject,
                        "version": entry.draft.version,
                        "origin": entry.draft.origin.value,
                        "needs_user_input": list(entry.draft.needs_user_input),
                        "source_count": entry.source_count,
                    }
                    for entry in listed
                ]
            }
        )

    @app.get("/api/mail/drafts/{reference}")
    async def draft_detail(reference: str, request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        try:
            result = await deps.drafts.get_draft(reference)
        except (MailDraftNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        return JSONResponse(_draft_payload(result.draft))

    @app.get("/api/actions")
    async def actions(request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        listed = await deps.actions.list_overviews(limit=50)
        return JSONResponse({"actions": [_action_summary(item) for item in listed]})

    @app.get("/api/actions/{reference}")
    async def action_detail(reference: str, request: Request) -> JSONResponse:
        session = await _require_session(deps, request)
        if isinstance(session, JSONResponse):
            return session
        try:
            overview = await deps.actions.overview(reference)
        except (ActionRequestNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        return JSONResponse(_action_detail(overview))


def _register_write_routes(app: FastAPI, deps: WebDependencies) -> None:
    """The only mutations the control plane has, each routed through an application service."""

    @app.post("/api/tasks")
    async def create_task(request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        body = await _json_body(request)
        try:
            command = _task_command(body)
        except ValueError as exc:
            return _json_error(400, str(exc))
        try:
            created = await deps.tasks.create_task(command)
        except InvalidTask as exc:
            return _json_error(400, str(exc))
        deadlines = await deps.deadlines([created])
        return JSONResponse(
            _task_payload(created, deadlines.get(created.id)), status_code=201
        )

    @app.post("/api/tasks/{reference}/complete")
    async def complete_task(reference: str, request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        try:
            task_id = await deps.tasks.resolve_task_id(reference)
            result = await deps.tasks.complete_task(task_id)
        except (TaskNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        except DomainError as exc:
            return _json_error(409, str(exc))
        return JSONResponse(
            {"id": str(result.task.id), "status": result.task.status.value}
        )

    @app.post("/api/notifications/{reference}/read")
    async def read_notification(reference: str, request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        try:
            notification_id = await deps.notifications.resolve_notification_id(reference)
        except (DomainError, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        updated = await deps.notifications.mark_notification_read(
            notification_id, at=deps.clock.now()
        )
        return JSONResponse(_notification_payload(updated))

    @app.patch("/api/mail/drafts/{reference}")
    async def edit_draft(reference: str, request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        body = await _json_body(request)
        subject = body.get("subject")
        text = body.get("body")
        expected_version = body.get("expected_version")
        if subject is not None and not isinstance(subject, str):
            return _json_error(400, "subject must be a string")
        if text is not None and not isinstance(text, str):
            return _json_error(400, "body must be a string")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            return _json_error(400, "expected_version is required")
        try:
            current = await deps.drafts.get_draft(reference)
        except (MailDraftNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        if current.draft.version != expected_version:
            return _stale_draft(current.draft.version)
        try:
            edited = await deps.drafts.edit_draft(reference, subject=subject, body=text)
        except StaleMailDraftUpdate:
            latest = await deps.drafts.get_draft(reference)
            return _stale_draft(latest.draft.version)
        except (InvalidMailDraft, ValueError) as exc:
            return _json_error(400, str(exc))
        return JSONResponse(_draft_payload(edited))


def _register_approval_routes(app: FastAPI, deps: WebDependencies) -> None:
    """Review and approve. This is where the web surface stops."""

    @app.post("/api/actions/{reference}/challenge")
    async def challenge(reference: str, request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        try:
            action = await deps.actions.require_action(reference)
            issued = await deps.approvals.create_challenge(action.id)
        except (ActionRequestNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        except DomainError as exc:
            return _json_error(409, str(exc))
        return JSONResponse(
            {
                "action_id": str(action.id),
                "fingerprint": action.fingerprint,
                "token": issued.token,
                "expires_at": issued.challenge.expires_at.isoformat(),
            }
        )

    @app.post("/api/actions/{reference}/approve")
    async def approve(reference: str, request: Request) -> JSONResponse:
        session = await _require_mutation(deps, request)
        if isinstance(session, JSONResponse):
            return session
        body = await _json_body(request)
        token = body.get("token")
        if not isinstance(token, str):
            return _json_error(400, "an approval token is required")
        try:
            action = await deps.actions.require_action(reference)
            record = await deps.approvals.approve(action.id, token)
        except (ActionRequestNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        except _APPROVAL_REFUSALS as exc:
            return _json_error(409, str(exc))
        return JSONResponse(_approved_payload(action.id, record))


def _register_approval_link_routes(app: FastAPI, deps: WebDependencies) -> None:
    """The sessionless approval link: the token *is* the capability, and it is narrowly scoped."""

    @app.post("/api/approval-link/preview")
    async def preview(request: Request) -> JSONResponse:
        action_id, token = await _approval_link_body(request)
        if action_id is None or token is None:
            return _json_error(400, "an action id and a token are required")
        try:
            overview = await deps.actions.overview(action_id)
        except (ActionRequestNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        challenge = await deps.actions.latest_challenge(overview.action.id)
        if challenge is None or challenge.is_consumed() or challenge.is_expired(
            deps.clock.now()
        ):
            return _json_error(403, "this approval link is no longer valid")
        if not challenge.matches_token(token):
            # A wrong token learns nothing beyond "no": previewing does not consume anything.
            return _json_error(403, "this approval link is not valid for this action")
        return JSONResponse(
            {
                "action": _action_detail(overview),
                "expires_at": challenge.expires_at.isoformat(),
            }
        )

    @app.post("/api/approval-link/approve")
    async def approve_link(request: Request) -> JSONResponse:
        action_id, token = await _approval_link_body(request)
        if action_id is None or token is None:
            return _json_error(400, "an action id and a token are required")
        try:
            action = await deps.actions.require_action(action_id)
            record = await deps.approvals.approve(action.id, token)
        except (ActionRequestNotFound, AmbiguousId) as exc:
            return _json_error(404, str(exc))
        except _APPROVAL_REFUSALS as exc:
            return _json_error(403, str(exc))
        return JSONResponse(_approved_payload(action.id, record))


_APPROVAL_REFUSALS = (
    InvalidApprovalToken,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalChallengeNotFound,
    ApprovalAlreadyOutstanding,
    ApprovalUnavailable,
)


# ------------------------------------------------------------------------ helpers


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _stale_draft(current_version: int) -> JSONResponse:
    """Report an optimistic-concurrency conflict with the version the browser should reload."""
    return JSONResponse(
        {
            "error": "the draft changed since you loaded it",
            "current_version": current_version,
        },
        status_code=409,
    )


def _approved_payload(action_id: Any, record: Any) -> dict[str, Any]:
    """Approving is where the web surface ends: `executed` is always false."""
    return {
        "approved": True,
        "action_id": str(action_id),
        "fingerprint": record.action_fingerprint,
        "expires_at": record.expires_at.isoformat(),
        "executed": False,
    }


def _set_cookie(response: Response, name: str, value: str) -> None:
    """Set a cookie with the attributes this deployment can honestly claim.

    `HttpOnly` for the session and `SameSite=Strict` for both, always. `Secure` is *not* set: V1 is
    same-LAN HTTP, and claiming transport protection the server does not have would be worse than
    the honest limitation.
    """
    response.set_cookie(
        name,
        value,
        httponly=name == SESSION_COOKIE,
        samesite="strict",
        path="/",
    )


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _approval_link_body(request: Request) -> tuple[str | None, str | None]:
    body = await _json_body(request)
    action_id = body.get("action_id")
    token = body.get("token")
    if not isinstance(action_id, str) or not isinstance(token, str):
        return None, None
    return action_id, token


async def _require_session(
    deps: WebDependencies, request: Request
) -> MobileWebSession | JSONResponse:
    """Authenticate a read, or return the error response."""
    try:
        return await deps.auth.authenticate(request.cookies.get(SESSION_COOKIE))
    except (MobileDisabled, MobileSessionInvalid) as exc:
        return _json_error(401, str(exc))


async def _require_mutation(
    deps: WebDependencies, request: Request
) -> MobileWebSession | JSONResponse:
    """Authenticate a mutation *and* require the CSRF pair."""
    session = await _require_session(deps, request)
    if isinstance(session, JSONResponse):
        return session
    try:
        deps.auth.verify_csrf(
            session,
            header_token=request.headers.get(CSRF_HEADER),
            cookie_token=request.cookies.get(CSRF_COOKIE),
        )
    except MobileCsrfRejected as exc:
        return _json_error(403, str(exc))
    return session


def _task_payload(task: Any, deadline: Any) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "estimated_minutes": task.estimated_minutes,
        "deadline": None if deadline is None else deadline.due_at.isoformat(),
    }


def _notification_payload(notification: Any) -> dict[str, Any]:
    return {
        "id": str(notification.id),
        "kind": notification.kind.value,
        "title": notification.title,
        "body": notification.body,
        "status": notification.status.value,
        "created_at": notification.created_at.isoformat(),
    }


def _draft_payload(draft: Any) -> dict[str, Any]:
    return {
        "id": str(draft.id),
        "account_id": draft.account_id,
        "to_addresses": list(draft.to_addresses),
        "subject": draft.subject,
        "body_text": draft.body_text,
        "version": draft.version,
        "origin": draft.origin.value,
        "needs_user_input": list(draft.needs_user_input),
        "needs_user_input_acknowledged": draft.needs_user_input_acknowledged_at is not None,
    }


def _action_summary(overview: Any) -> dict[str, Any]:
    return {
        "id": str(overview.action.id),
        "case_id": str(overview.action.case_id),
        "type": overview.action.action_type.value,
        "status": overview.action.status.value,
        "fingerprint": overview.action.fingerprint,
        "approval": overview.approval_state.value,
        "execution": overview.execution_state,
        "created_at": overview.action.created_at.isoformat(),
    }


def _action_detail(overview: Any) -> dict[str, Any]:
    """The exact action, with its canonical payload. Nothing here is model-generated."""
    detail = _action_summary(overview)
    detail["payload"] = overview.action.payload
    detail["payload_json"] = overview.action.payload_json
    detail["approval_expires_at"] = (
        None if overview.approval is None else overview.approval.expires_at.isoformat()
    )
    return detail


async def _dashboard_payload(deps: WebDependencies) -> dict[str, Any]:
    """One screen of progress, derived entirely from existing read models."""
    now = deps.clock.now()
    tasks = await deps.tasks.list_tasks(include_terminal=False)
    deadlines = await deps.deadlines(tasks)
    cases = await deps.cases.list_cases(statuses=None, limit=20)
    notifications = await deps.notifications.list_notifications(limit=20)
    drafts = await deps.drafts.list_drafts(limit=5)
    actions = await deps.actions.list_overviews(limit=50)
    open_cases = [case for case in cases if case.is_open]
    unread = [item for item in notifications if item.status.value == "unread"]
    approved = [item for item in actions if item.approval_state is ApprovalState.VALID]
    unresolved = [
        item
        for item in actions
        if item.execution is not None
        and item.execution.status.value in ("unknown", "running")
    ]
    return {
        "now": now.isoformat(),
        "open_tasks": [
            _task_payload(task, deadlines.get(task.id)) for task in tasks[:20]
        ],
        "open_task_count": len(tasks),
        "open_cases": [
            {"id": str(case.id), "title": case.title, "status": case.status.value}
            for case in open_cases
        ],
        "unread_notification_count": len(unread),
        "notifications": [_notification_payload(item) for item in notifications[:5]],
        "prepared_actions": [
            _action_summary(item)
            for item in actions
            if item.approval_state is ApprovalState.NONE
        ][:5],
        "approved_action_count": len(approved),
        "unresolved_action_count": len(unresolved),
        "recent_drafts": [
            {
                "id": str(entry.draft.id),
                "subject": entry.draft.subject,
                "version": entry.draft.version,
            }
            for entry in drafts
        ],
    }


def _task_command(body: dict[str, Any]) -> CreateTask:
    """Build a typed task command from a JSON body. There is no natural language here."""
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("a task needs a title")
    priority = body.get("priority", TaskPriority.NORMAL.value)
    try:
        parsed_priority = TaskPriority(str(priority))
    except ValueError as exc:
        raise ValueError(f"unknown priority {priority!r}") from exc
    estimated = body.get("estimated_minutes")
    if estimated is not None and (
        not isinstance(estimated, int) or isinstance(estimated, bool)
    ):
        raise ValueError("estimated_minutes must be an integer or null")
    deadline = _deadline(body.get("deadline"))
    return CreateTask(
        title=title.strip(),
        priority=parsed_priority,
        estimated_minutes=estimated,
        due_at=deadline,
    )


def _deadline(raw: Any) -> datetime | None:
    """Parse an explicit, timezone-aware deadline. Local time is never assumed."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("deadline must be an ISO 8601 string or null")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("deadline must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline must include a timezone offset")
    return parsed


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SECURITY_HEADERS",
    "SESSION_COOKIE",
    "STATIC_DIR",
    "WebDependencies",
    "build_app",
]
