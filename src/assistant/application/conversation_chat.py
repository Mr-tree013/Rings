"""The application surface the browser chat adapter speaks to (ADR-0041 §14-§19).

```text
HTTP handler ──► ConversationChatService ──► coordinator / cards / brief / repositories
```

This module exists so the HTTP layer can stay thin and, more importantly, so that it has *nothing*
to call but application services. It answers three questions:

* what does this thread look like right now, durably (a snapshot);
* accept this message (the coordinator owns what happens next);
* settle this card, by exact identity (the card service owns the closed dispatch).

Everything it returns is JSON-safe data with no internal vocabulary: no prompt, no provider
response, no schema error, no database exception text, no approval token, and no operation
payload beyond the exact outbound message a human is being asked to send.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from assistant.application.conversation_cards import ConfirmationCard, ConversationCardService
from assistant.application.conversation_event_broker import ConversationEventBroker
from assistant.application.conversation_request_coordinator import ConversationRequestCoordinator
from assistant.application.conversation_service import ConversationService
from assistant.domain.conversation import (
    ConversationMessageRole,
    ConversationThread,
    ConversationThreadStatus,
)
from assistant.domain.conversation_request import (
    ConversationRequest,
    ConversationRequestId,
    ConversationRequestStatus,
)
from assistant.domain.errors import (
    ConversationThreadNotFound,
    PlanningNotConfigured,
)
from assistant.ports.clock import Clock
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.conversation_request_repository import ConversationRequestRepository

MESSAGE_LIMIT = 200
"""How much history one snapshot carries. Older turns stay durable and out of the first page."""

REQUEST_LIMIT = 50
"""How many accepted requests of one thread a snapshot describes."""

THREAD_LIMIT = 30
"""How many conversations the sidebar offers (ADR-0041 §16)."""

DISPLAY_TITLE_CHARS = 40
"""A derived thread title is a label: the first user message, cut to something a sidebar fits."""

QUICK_ACTIONS = (
    ("查看今天", "我今天有什么事？"),
    ("看看任务", "我现在有哪些任务？"),
    ("规划这周", "帮我规划这周。"),
    ("查看需要处理的邮件", "有哪些邮件需要我处理？"),
)
"""The welcome surface's buttons. Each one submits an ordinary message through the same queue."""


@dataclass(frozen=True, slots=True)
class ChatHome:
    """The deterministic home surface: a welcome line, quick actions and a bounded brief."""

    greeting: str
    quick_actions: tuple[dict[str, str], ...]
    brief: dict[str, object] | None


class ConversationChatService:
    """The browser's read model and its two write paths, over existing services."""

    def __init__(
        self,
        *,
        conversation: ConversationService,
        coordinator: ConversationRequestCoordinator,
        cards: ConversationCardService,
        conversations: ConversationRepository,
        requests: ConversationRequestRepository,
        broker: ConversationEventBroker,
        brief: object | None,
        clock: Clock,
    ) -> None:
        self._conversation = conversation
        self._coordinator = coordinator
        self._cards = cards
        self._conversations = conversations
        self._requests = requests
        self._broker = broker
        self._brief = brief
        self._clock = clock

    @property
    def broker(self) -> ConversationEventBroker:
        """The ephemeral notification channel the SSE endpoint subscribes to."""
        return self._broker

    @property
    def coordinator(self) -> ConversationRequestCoordinator:
        """The durable queue's owner, so the daemon can run its recovery and drain its workers."""
        return self._coordinator

    # ------------------------------------------------------------------ reading

    async def bootstrap(self) -> dict[str, object]:
        """Everything the shell needs to open: threads, the thread to show, and the home surface."""
        threads = await self.list_threads()
        current = threads[0]["id"] if threads else None
        return {
            "threads": threads,
            "current_thread_id": current,
            "home": await self.home(thread_id=None if current is None else UUID(str(current))),
        }

    async def list_threads(self) -> list[dict[str, object]]:
        """A bounded recent list, newest first, with a deterministic display title."""
        threads = await self._conversations.list_threads(limit=THREAD_LIMIT)
        return [await self._thread_payload(thread) for thread in threads]

    async def create_thread(self) -> dict[str, object]:
        """Start a new conversation through the existing service. No model call happens here."""
        thread = await self._conversation.start_thread()
        return await self._thread_payload(thread)

    async def snapshot(self, thread_id: UUID) -> dict[str, object]:
        """The authoritative durable state of one conversation (ADR-0041 §18).

        Raises:
            ConversationThreadNotFound: no such thread.
        """
        thread = await self._require_thread(thread_id)
        messages = await self._conversations.list_messages(thread_id, limit=MESSAGE_LIMIT)
        requests = await self._requests.list_for_thread(thread_id, limit=REQUEST_LIMIT)
        cards = await self._cards.cards(thread_id)
        active = next(
            (
                request
                for request in requests
                if request.status is ConversationRequestStatus.PROCESSING
            ),
            None,
        )
        return {
            "thread": await self._thread_payload(thread),
            "messages": [
                {
                    "id": str(message.id),
                    "role": message.role.value,
                    "text": message.text,
                    "created_at": message.created_at.isoformat(),
                }
                for message in messages
                if message.role in (ConversationMessageRole.USER, ConversationMessageRole.ASSISTANT)
            ],
            "requests": _request_payloads(requests),
            "active": None if active is None else _request_payload(active, position=0),
            "pending": [
                _request_payload(request, position=position)
                for position, request in enumerate(
                    [
                        item
                        for item in reversed(requests)
                        if item.status is ConversationRequestStatus.QUEUED
                    ],
                    start=1,
                )
            ],
            "cards": [card.to_payload() for card in cards],
            "home": await self.home(thread_id=thread_id),
        }

    async def home(self, *, thread_id: UUID | None) -> dict[str, object]:
        """The welcome surface: deterministic, read-only, and never a conversation turn."""
        del thread_id
        brief = await self._brief_payload()
        home = ChatHome(
            greeting="你好，我是 Tree。今天想处理什么？",
            quick_actions=tuple({"label": label, "text": text} for label, text in QUICK_ACTIONS),
            brief=brief,
        )
        return {
            "greeting": home.greeting,
            "quick_actions": list(home.quick_actions),
            "brief": home.brief,
        }

    async def _brief_payload(self) -> dict[str, object] | None:
        """A compact view of today, or `None` when this host cannot answer yet.

        It is the existing deterministic brief, read once and flattened into lines — not a second
        rendering stack, and not a conversation turn (ADR-0041 §44-§47).
        """
        if self._brief is None:
            return None
        try:
            brief = await self._brief.build()  # type: ignore[attr-defined]
        except PlanningNotConfigured:
            return None
        except Exception:
            return None
        lines: list[dict[str, str]] = []
        for group, entries in (
            ("今日安排", brief.schedule),
            ("任务", brief.tasks),
            ("需要处理", brief.attention),
        ):
            for entry in entries[:3]:
                detail = "" if entry.detail is None else f"（{entry.detail}）"
                lines.append({"group": group, "text": f"{entry.label}{detail}"})
        return {
            "available": True,
            "local_date": brief.local_date.isoformat(),
            "timezone": brief.timezone,
            "is_empty": brief.is_empty,
            "lines": lines,
        }

    # ------------------------------------------------------------------ writing

    async def accept_message(
        self, thread_id: UUID, *, client_request_id: str, text: str
    ) -> dict[str, object]:
        """Accept one message into the durable queue. Nothing runs inside this call."""
        await self._require_thread(thread_id)
        request, created = await self._coordinator.accept(
            thread_id, client_request_id=client_request_id, text=text
        )
        payload = _request_payload(request, position=await self._queue_position(request))
        payload["duplicate"] = not created
        return payload

    async def _queue_position(self, request: ConversationRequest) -> int | None:
        """Where this request is in its thread's queue: 1 is next, `None` when it is not waiting.

        The position is derived from the durable order, so two tabs cannot disagree about it, and
        it is recomputed on every read rather than stored.
        """
        if request.status is not ConversationRequestStatus.QUEUED:
            return None
        waiting = [
            item
            for item in reversed(
                await self._requests.list_for_thread(request.thread_id, limit=REQUEST_LIMIT)
            )
            if item.status is ConversationRequestStatus.QUEUED
        ]
        for position, item in enumerate(waiting, start=1):
            if item.id == request.id:
                return position
        return None

    async def cancel_request(self, request_id: ConversationRequestId) -> dict[str, object]:
        """Stop one request, or raise `CannotCancelSafely` because it is past a safe boundary."""
        request = await self._coordinator.cancel(request_id)
        return _request_payload(request, position=None)

    async def settle_card(
        self,
        thread_id: UUID,
        card_identifier: str,
        *,
        expected_revision: str,
        confirm: bool,
    ) -> dict[str, object]:
        """Settle exactly one card, and tell every open browser that state moved."""
        await self._require_thread(thread_id)
        reply = await self._cards.settle(
            thread_id,
            card_identifier,
            expected_revision=expected_revision,
            confirm=confirm,
        )
        self._broker.publish(thread_id, "confirmation.updated", {"card_id": card_identifier})
        self._broker.publish(
            thread_id,
            "assistant.message",
            {
                "turn_id": reply.turn_id,
                "text": reply.text,
                "status": reply.status.value,
                "waiting_for_confirmation": reply.waiting_for_confirmation,
                "error_code": None if reply.error_code is None else reply.error_code.value,
            },
        )
        self._broker.publish(thread_id, "thread.updated", {"thread_id": str(thread_id)})
        return {
            "card_id": card_identifier,
            "text": reply.text,
            "status": reply.status.value,
            "waiting_for_confirmation": reply.waiting_for_confirmation,
        }

    # ------------------------------------------------------------------ internals

    async def _require_thread(self, thread_id: UUID) -> ConversationThread:
        thread = await self._conversations.get_thread(thread_id)
        if thread is None:
            raise ConversationThreadNotFound(thread_id)
        return thread

    async def _thread_payload(self, thread: ConversationThread) -> dict[str, object]:
        return {
            "id": str(thread.id),
            "title": thread.title,
            "display_title": await self._display_title(thread),
            "status": thread.status.value,
            "active": thread.status is ConversationThreadStatus.ACTIVE,
            "created_at": thread.created_at.isoformat(),
            "updated_at": thread.updated_at.isoformat(),
        }

    async def _display_title(self, thread: ConversationThread) -> str:
        """A durable title if there is one, otherwise the first user message, truncated."""
        if thread.title:
            return thread.title
        for message in await self._conversations.list_messages(thread.id, limit=MESSAGE_LIMIT):
            if message.role is ConversationMessageRole.USER:
                return _truncate(message.text)
        return "新的对话"


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= DISPLAY_TITLE_CHARS:
        return collapsed
    return f"{collapsed[: DISPLAY_TITLE_CHARS - 1]}…"


def _request_payload(request: ConversationRequest, *, position: int | None) -> dict[str, object]:
    """One accepted request, as the browser is allowed to see it.

    The submitted text is carried only while it is not yet a durable conversation message; once the
    turn exists the text lives in the history and is not repeated here (§31).
    """
    return {
        "id": str(request.id),
        "thread_id": str(request.thread_id),
        "status": request.status.value,
        "stage": None if request.stage is None else request.stage.value,
        "text": request.input_text,
        "queue_position": position,
        "can_cancel": request.can_cancel,
        "error_code": request.error_code,
        "created_at": request.created_at.isoformat(),
        "started_at": _iso(request.started_at),
        "finished_at": _iso(request.finished_at),
    }


def _request_payloads(requests: list[ConversationRequest]) -> list[dict[str, object]]:
    """Requests in the order a person reads them: oldest first, as one timeline."""
    return [_request_payload(request, position=None) for request in reversed(requests)]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def cards_to_payload(cards: tuple[ConfirmationCard, ...]) -> list[dict[str, object]]:
    """The JSON shape of a card list, used by the snapshot and by tests."""
    return [card.to_payload() for card in cards]


__all__ = [
    "DISPLAY_TITLE_CHARS",
    "MESSAGE_LIMIT",
    "QUICK_ACTIONS",
    "REQUEST_LIMIT",
    "THREAD_LIMIT",
    "ChatHome",
    "ConversationChatService",
    "cards_to_payload",
]
