"""The Tree chat HTTP surface: a thin interaction adapter over the conversation runtime.

```text
GET  /chat                                     the shell (static, authenticated by session)
GET  /api/chat/bootstrap                       threads + the home surface
GET  /api/chat/threads                         recent conversations
POST /api/chat/threads                         a new conversation (no model call)
GET  /api/chat/threads/{id}/snapshot           authoritative durable state
POST /api/chat/threads/{id}/messages           accept input (idempotent, queued)
POST /api/chat/requests/{id}/cancel            Stop, or refuse to lie about it
POST /api/chat/threads/{id}/confirmations/{c}/confirm|cancel
GET  /api/chat/threads/{id}/events             ephemeral SSE notifications
GET  /api/chat/attention                       the unified inbox, most urgent first
POST /api/chat/attention/{id}/acknowledge      settle one item: seen
POST /api/chat/attention/{id}/dismiss          settle one item: stop reminding
```

What makes this an adapter rather than a second application layer:

* every handler calls `ConversationChatService`, which calls the coordinator, the card service and
  the existing repositories — **no handler ever touches `ModelPort`, SQLite or an executor**;
* authorization is the existing session + CSRF pair, and there is no anonymous LAN path (§15);
* the stream is a *notification*: nothing is replayed, a reload fetches a snapshot, and a client
  that falls too far behind is told to resynchronise instead of being buffered for (§13, §42).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from assistant.application.conversation_chat import ConversationChatService
from assistant.application.conversation_event_broker import RESYNC_REQUIRED
from assistant.domain.attention import ATTENTION_LIMIT
from assistant.domain.errors import (
    AttentionItemNotFound,
    CannotCancelSafely,
    ConversationRequestNotFound,
    ConversationThreadNotFound,
    InvalidConversationRequest,
    InvalidConversationThread,
    StaleConversationCard,
    UnknownConversationCard,
)
from assistant.domain.mobile import MobileWebSession

HEARTBEAT_SECONDS = 15.0
"""How long a quiet stream stays silent before a comment line proves it is alive."""

MAX_BODY_BYTES = 16 * 1024
"""The whole request body is a client id and a bounded message; anything larger is a mistake."""

MAX_MESSAGE_CHARS = 4000
"""Same ceiling the conversation domain enforces, checked before the queue sees the text."""

STREAM_OPEN = "stream.open"
"""The first frame, so a client can tell "connected" from "still connecting"."""


def register_chat_routes(
    app: FastAPI,
    *,
    chat: Callable[[], ConversationChatService],
    attention: Callable[[], object] | None = None,
    attention_projector: Callable[[], object] | None = None,
    assets: object,
    require_session: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
    require_mutation: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
) -> None:
    """Register the chat surface, reusing the control plane's existing auth pair.

    `chat` and the two guards are callables rather than values because the app is built before the
    per-request session is known, and because a test injects its own stack.
    """

    @app.get("/chat")
    async def chat_page() -> FileResponse:
        return FileResponse(assets / "chat.html", media_type="text/html")  # type: ignore[operator]

    @app.get("/chat.js")
    async def chat_script() -> FileResponse:
        return FileResponse(
            assets / "chat.js",  # type: ignore[operator]
            media_type="application/javascript",
        )

    @app.get("/chat.css")
    async def chat_styles() -> FileResponse:
        return FileResponse(assets / "chat.css", media_type="text/css")  # type: ignore[operator]

    @app.get("/api/chat/bootstrap")
    async def bootstrap(request: Request) -> JSONResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        return JSONResponse(await chat().bootstrap())

    @app.get("/api/chat/threads")
    async def threads(request: Request) -> JSONResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        return JSONResponse({"threads": await chat().list_threads()})

    @app.post("/api/chat/threads")
    async def new_thread(request: Request) -> JSONResponse:
        session = await require_mutation(request)
        if isinstance(session, JSONResponse):
            return session
        return JSONResponse(await chat().create_thread(), status_code=201)

    @app.get("/api/chat/threads/{thread_id}/snapshot")
    async def snapshot(thread_id: str, request: Request) -> JSONResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        identifier = _thread_id(thread_id)
        if identifier is None:
            return _json_error(404, "no such conversation")
        try:
            payload = await chat().snapshot(identifier)
        except ConversationThreadNotFound as exc:
            return _json_error(404, str(exc))
        return JSONResponse(payload)

    @app.post("/api/chat/threads/{thread_id}/messages")
    async def send_message(thread_id: str, request: Request) -> JSONResponse:
        session = await require_mutation(request)
        if isinstance(session, JSONResponse):
            return session
        identifier = _thread_id(thread_id)
        if identifier is None:
            return _json_error(404, "no such conversation")
        body = await _json_body(request)
        if body is None:
            return _json_error(413, "this request is larger than the chat surface accepts")
        client_request_id = body.get("client_request_id")
        text = body.get("text")
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            return _json_error(400, "a client_request_id is required")
        if not isinstance(text, str) or not text.strip():
            return _json_error(400, "a message cannot be blank")
        if len(text) > MAX_MESSAGE_CHARS:
            return _json_error(400, "this message is longer than a chat turn accepts")
        try:
            payload = await chat().accept_message(
                identifier, client_request_id=client_request_id, text=text
            )
        except ConversationThreadNotFound as exc:
            return _json_error(404, str(exc))
        except InvalidConversationThread as exc:
            return _json_error(409, str(exc))
        except InvalidConversationRequest as exc:
            return _json_error(400, str(exc))
        return JSONResponse(payload, status_code=202)

    @app.post("/api/chat/requests/{request_id}/cancel")
    async def cancel_request(request_id: str, request: Request) -> JSONResponse:
        session = await require_mutation(request)
        if isinstance(session, JSONResponse):
            return session
        identifier = _uuid(request_id)
        if identifier is None:
            return _json_error(404, "no such request")
        try:
            payload = await chat().cancel_request(identifier)
        except ConversationRequestNotFound as exc:
            return _json_error(404, str(exc))
        except CannotCancelSafely:
            # The honest refusal: state or an external effect has already moved (§33).
            return JSONResponse(
                {
                    "error": "当前操作已经进入执行阶段，不能安全停止。",
                    "code": "CANNOT_CANCEL_SAFELY",
                },
                status_code=409,
            )
        return JSONResponse(payload)

    @app.post(
        "/api/chat/threads/{thread_id}/confirmations/{confirmation_id}/confirm"
    )
    async def confirm_card(thread_id: str, confirmation_id: str, request: Request) -> JSONResponse:
        return await _settle_card(
            request,
            thread_id=thread_id,
            confirmation_id=confirmation_id,
            confirm=True,
            chat=chat,
            require_mutation=require_mutation,
        )

    @app.post("/api/chat/threads/{thread_id}/confirmations/{confirmation_id}/cancel")
    async def cancel_card(thread_id: str, confirmation_id: str, request: Request) -> JSONResponse:
        return await _settle_card(
            request,
            thread_id=thread_id,
            confirmation_id=confirmation_id,
            confirm=False,
            chat=chat,
            require_mutation=require_mutation,
        )

    @app.get("/api/chat/threads/{thread_id}/events", response_model=None)
    async def events(thread_id: str, request: Request) -> JSONResponse | StreamingResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        identifier = _thread_id(thread_id)
        if identifier is None:
            return _json_error(404, "no such conversation")
        try:
            await chat().snapshot(identifier)
        except ConversationThreadNotFound as exc:
            return _json_error(404, str(exc))
        service = chat()
        return StreamingResponse(
            conversation_event_stream(
                service, identifier, is_disconnected=request.is_disconnected
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    if attention is None:
        return

    @app.get("/api/chat/attention")
    async def attention_inbox(request: Request) -> JSONResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        if attention_projector is not None:
            # Reconcile before reading, so a drawer opened right after a task went overdue is
            # current instead of up to one daemon interval behind. Idempotent and bounded.
            await attention_projector().refresh()  # type: ignore[attr-defined]
        summary = await attention().list_live(limit=ATTENTION_LIMIT)  # type: ignore[attr-defined]
        return JSONResponse(
            {
                "total": summary.total,
                "overflow": summary.overflow,
                "by_severity": dict(summary.by_severity),
                "items": [item.to_payload() for item in summary.items],
            }
        )

    @app.post("/api/chat/attention/{item_id}/acknowledge")
    async def acknowledge_attention(item_id: str, request: Request) -> JSONResponse:
        return await _settle_attention(
            request,
            item_id=item_id,
            dismiss=False,
            attention=attention,
            require_mutation=require_mutation,
        )

    @app.post("/api/chat/attention/{item_id}/dismiss")
    async def dismiss_attention(item_id: str, request: Request) -> JSONResponse:
        return await _settle_attention(
            request,
            item_id=item_id,
            dismiss=True,
            attention=attention,
            require_mutation=require_mutation,
        )


async def conversation_event_stream(
    chat: ConversationChatService,
    thread_id: UUID,
    *,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """The SSE frames for one thread: bounded, non-authoritative, and never a replay log.

    Everything here is deliberately disposable. A subscriber that stops reading overflows and is
    told to resynchronise; a reconnect starts from a fresh snapshot; and no frame is ever written
    down. Progress frames carry the coarse stage the application chose, and the assistant frame
    carries the reply the user will also find in durable history.
    """
    subscription = chat.broker.subscribe(thread_id)
    try:
        yield _frame(STREAM_OPEN, {"thread_id": str(thread_id)}, event_id=0)
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            if subscription.overflowed:
                yield _frame(RESYNC_REQUIRED, {"reason": "subscriber_too_slow"}, event_id=None)
                return
            event = await subscription.next_event(timeout=heartbeat)
            if event is None:
                yield ": heartbeat\n\n"
                continue
            yield _frame(event.name, event.data, event_id=event.id)
    finally:
        # Always, however the stream ends: a closed tab must not leave a queue behind.
        chat.broker.unsubscribe(subscription)


async def _settle_card(
    request: Request,
    *,
    thread_id: str,
    confirmation_id: str,
    confirm: bool,
    chat: Callable[[], ConversationChatService],
    require_mutation: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
) -> JSONResponse:
    session = await require_mutation(request)
    if isinstance(session, JSONResponse):
        return session
    identifier = _thread_id(thread_id)
    if identifier is None:
        return _json_error(404, "no such conversation")
    body = await _json_body(request)
    if body is None:
        return _json_error(413, "this request is larger than the chat surface accepts")
    revision = body.get("expected_revision")
    if not isinstance(revision, str) or not revision:
        return _json_error(400, "an expected_revision is required")
    try:
        payload = await chat().settle_card(
            identifier,
            confirmation_id,
            expected_revision=revision,
            confirm=confirm,
        )
    except ConversationThreadNotFound as exc:
        return _json_error(404, str(exc))
    except UnknownConversationCard as exc:
        return _json_error(404, str(exc))
    except StaleConversationCard:
        # Nothing was applied, approved or executed. The browser reloads and shows what is true.
        return JSONResponse(
            {"error": "这项确认已经过期，我已刷新最新状态。", "code": "STALE_CONFIRMATION"},
            status_code=409,
        )
    except InvalidConversationThread as exc:
        return _json_error(409, str(exc))
    return JSONResponse(payload)


async def _settle_attention(
    request: Request,
    *,
    item_id: str,
    dismiss: bool,
    attention: Callable[[], object],
    require_mutation: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
) -> JSONResponse:
    """Settle exactly one attention item. It never touches the thing the item points at."""
    session = await require_mutation(request)
    if isinstance(session, JSONResponse):
        return session
    identifier = _uuid(item_id)
    if identifier is None:
        return _json_error(404, "no such attention item")
    service = attention()
    try:
        settle = service.dismiss if dismiss else service.acknowledge  # type: ignore[attr-defined]
        item = await settle(identifier)
    except AttentionItemNotFound:
        return JSONResponse(
            {"error": "这条提醒已经不在你的列表里了。", "code": "NOT_FOUND"}, status_code=404
        )
    return JSONResponse({"item": item.to_payload()})


def _frame(name: str, data: dict[str, object], *, event_id: int | None) -> str:
    """One SSE frame. `data` is canonical JSON, and the name is from a closed vocabulary."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n\n"


async def _json_body(request: Request) -> dict[str, object] | None:
    """Read a small JSON object, or `None` when the body is over the ceiling."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _thread_id(value: str) -> UUID | None:
    return _uuid(value)


def _uuid(value: str) -> UUID | None:
    with contextlib.suppress(ValueError):
        return UUID(value)
    return None


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


__all__ = [
    "HEARTBEAT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_MESSAGE_CHARS",
    "STREAM_OPEN",
    "conversation_event_stream",
    "register_chat_routes",
]
