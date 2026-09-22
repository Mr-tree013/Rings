"""Deterministic confirmation cards for the browser (ADR-0041 §20-§29).

```text
durable state ──► build cards (pure, deterministic)
                      │
       card id + expected revision ──► settle exactly that target ──► existing controllers
```

A card is a *presentation of durable state*, never a new authority. It is rebuilt from the same
rows the terminal reads — the immutable `ActionRequest` payload, the pending operation, the
candidate, the confirmation group — so a card cannot show one thing and approve another. The
`expected_revision` it carries is derived from those rows, and the settlement path refuses a click
whose revision no longer matches (`StaleConversationCard`), which is what makes a stale card
harmless instead of dangerous.

The kind is a closed enum, and the dispatch at the bottom is a closed mapping over it: no
reflection, no dynamic import, no generic "approve this action" endpoint (§28).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from assistant.application.conversation_service import ConversationReply, ConversationService
from assistant.domain.conversation import (
    ConversationOperation,
    ConversationTurnStatus,
)
from assistant.domain.conversation_plan import (
    CalendarRecurringCreateWeeklyArguments,
    ConversationOperationType,
    PlanApplyProposalArguments,
)
from assistant.domain.conversation_review import EHALL_CERTIFICATE_ACTION_TYPE
from assistant.domain.errors import StaleConversationCard, UnknownConversationCard
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.planning_repository import PlanningRepository

CARD_ID_SEPARATOR = ":"
"""`kind:target`. The id is opaque to the browser and parsed only here."""

FIELD_LIMIT = 12
"""How many structured fields one card may carry. A card is a decision, not a page."""

_WEEKDAYS = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}


class ConfirmationCardKind(StrEnum):
    """The complete set of things a browser may be asked to confirm."""

    MAIL_SEND = "mail_send"
    EHALL_CERTIFICATE = "ehall_certificate"
    PLAN_APPLY = "plan_apply"
    RECURRING_SCHEDULE = "recurring_schedule"
    FACT_CONFIRMATION = "fact_confirmation"


@dataclass(frozen=True, slots=True)
class CardField:
    """One labelled value, rendered as text and never as markup."""

    label: str
    value: str

    def to_payload(self) -> dict[str, str]:
        """The JSON shape the browser receives."""
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True, slots=True)
class ConfirmationCard:
    """One deterministic proposal to confirm something that already exists durably."""

    id: str
    kind: ConfirmationCardKind
    title: str
    summary: str
    expected_revision: str
    created_at: str
    confirm_label: str
    cancel_label: str
    severity: str = "normal"
    fields: tuple[CardField, ...] = ()
    items: tuple[dict[str, str], ...] = field(default_factory=tuple)
    hint: str = "想修改？直接在输入框告诉 Tree 要改什么。"

    @property
    def target(self) -> str:
        """The durable identity this card settles."""
        return self.id.split(CARD_ID_SEPARATOR, 1)[1]

    def to_payload(self) -> dict[str, object]:
        """The JSON shape the browser receives. Nothing internal travels beyond the target id."""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "summary": self.summary,
            "expected_revision": self.expected_revision,
            "created_at": self.created_at,
            "confirm_label": self.confirm_label,
            "cancel_label": self.cancel_label,
            "severity": self.severity,
            "fields": [item.to_payload() for item in self.fields],
            "items": [dict(item) for item in self.items],
            "hint": self.hint,
        }


def card_id(kind: ConfirmationCardKind, target: UUID | str) -> str:
    """The opaque identity of one card."""
    return f"{kind.value}{CARD_ID_SEPARATOR}{target}"


def parse_card_id(identifier: str) -> tuple[ConfirmationCardKind, str]:
    """Split a card id into its kind and its durable target.

    Raises:
        UnknownConversationCard: the id names a kind this build does not settle.
    """
    kind_text, _, target = identifier.partition(CARD_ID_SEPARATOR)
    if not target:
        raise UnknownConversationCard(f"{identifier!r} is not a card id")
    try:
        return ConfirmationCardKind(kind_text), target
    except ValueError as exc:
        raise UnknownConversationCard(
            f"{kind_text!r} is not a card this build settles"
        ) from exc


class ConversationCardService:
    """Builds the cards of one thread, and settles exactly the target a card names."""

    def __init__(
        self,
        *,
        conversation: ConversationService,
        reviews: object,
        conversations: ConversationRepository,
        planning: PlanningRepository,
        commitments: CommitmentRepository,
        facts: object | None,
        clock: Clock,
    ) -> None:
        self._conversation = conversation
        self._reviews = reviews
        self._conversations = conversations
        self._planning = planning
        self._commitments = commitments
        self._facts = facts
        self._clock = clock

    # ------------------------------------------------------------------ building

    async def cards(self, thread_id: UUID) -> tuple[ConfirmationCard, ...]:
        """Every card that is live for one thread, in a stable order."""
        collected: list[ConfirmationCard] = []
        collected.extend(await self._mail_cards(thread_id))
        collected.extend(await self._group_cards(thread_id))
        collected.extend(await self._fact_cards())
        return tuple(collected)

    async def _mail_cards(self, thread_id: UUID) -> list[ConfirmationCard]:
        """One card per live reviewed external action, built from its own immutable payload.

        The dispatch is closed over the reviewed capabilities: an action type with no card design
        gets no card, because a card is a decision and a decision without a design would be a
        generic approval (ADR-0045 §19).
        """
        waiting = await self._reviews.waiting(thread_id)  # type: ignore[attr-defined]
        cards: list[ConfirmationCard] = []
        for review in waiting:
            payload = await self._reviews.payload_of(review)  # type: ignore[attr-defined]
            if payload is None:
                # The action behind the review is gone or unreadable. There is nothing honest to
                # show, and a card that cannot show the exact bytes must not offer to send them.
                continue
            if review.action_type == EHALL_CERTIFICATE_ACTION_TYPE:
                cards.append(_ehall_certificate_card(review, payload))
            else:
                cards.append(_mail_card(review, payload))
        return cards

    async def _group_cards(self, thread_id: UUID) -> list[ConfirmationCard]:
        pending = await self._conversations.operations_waiting_for_confirmation(thread_id)
        by_turn: dict[UUID, list[ConversationOperation]] = {}
        for operation in pending:
            by_turn.setdefault(operation.turn_id, []).append(operation)
        cards: list[ConfirmationCard] = []
        for turn_id, operations in by_turn.items():
            turn = await self._conversations.get_turn(turn_id)
            if turn is None or turn.status is not ConversationTurnStatus.WAITING_CONFIRMATION:
                # A group whose turn is no longer waiting is not live, whatever its rows say.
                continue
            recurring = [
                operation
                for operation in operations
                if operation.operation_type
                is ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY
            ]
            if recurring:
                cards.append(_recurring_card(turn_id, recurring))
                continue
            for operation in operations:
                card = await self._single_operation_card(turn_id, operation)
                if card is not None:
                    cards.append(card)
        return cards

    async def _single_operation_card(
        self, turn_id: UUID, operation: ConversationOperation
    ) -> ConfirmationCard | None:
        del turn_id
        if operation.operation_type is not ConversationOperationType.PLAN_APPLY_PROPOSAL:
            # A confirmation kind with no card design would be a generic approval, and this build
            # deliberately has none.
            return None
        arguments = operation.arguments
        if not isinstance(arguments, PlanApplyProposalArguments):
            return None  # pragma: no cover - the repository decodes by operation type
        return await self._plan_card(operation, arguments)

    async def _plan_card(
        self,
        operation: ConversationOperation,
        arguments: PlanApplyProposalArguments,
    ) -> ConfirmationCard | None:
        if arguments.proposal_id is None:
            # "apply the latest pending proposal" cannot be shown exactly, so it is not carded.
            return None
        try:
            proposal_id = UUID(arguments.proposal_id)
        except ValueError:  # pragma: no cover - the operation validator guarantees a UUID
            return None
        detail = await self._planning.get_proposal_detail(proposal_id)
        if detail is None:
            return None
        titles = await self._task_titles()
        items: list[dict[str, str]] = []
        for block in detail.blocks[:FIELD_LIMIT]:
            items.append(
                {
                    "starts_at": block.starts_at.isoformat(),
                    "ends_at": block.ends_at.isoformat(),
                    "minutes": str(block.duration_minutes),
                    "title": titles.get(block.task_id, "（未知任务）"),
                }
            )
        return ConfirmationCard(
            id=card_id(ConfirmationCardKind.PLAN_APPLY, operation.id),
            kind=ConfirmationCardKind.PLAN_APPLY,
            title="周计划",
            summary=f"这份提案安排了 {len(detail.blocks)} 个时间块。",
            expected_revision=_operation_revision(operation),
            created_at=operation.created_at.isoformat(),
            confirm_label="应用计划",
            cancel_label="取消",
            items=tuple(items),
        )

    async def _task_titles(self) -> dict[UUID, str]:
        tasks = await self._commitments.list_tasks(statuses=None)
        return {task.id: task.title for task in tasks}

    async def _fact_cards(self) -> list[ConfirmationCard]:
        if self._facts is None:
            return []
        waiting = await self._facts.pending()  # type: ignore[attr-defined]
        cards: list[ConfirmationCard] = []
        for review in waiting:
            candidate = review.candidate
            cards.append(
                ConfirmationCard(
                    id=card_id(ConfirmationCardKind.FACT_CONFIRMATION, candidate.id),
                    kind=ConfirmationCardKind.FACT_CONFIRMATION,
                    title="长期信息",
                    summary=f"要记住「{candidate.fact_key}」吗？",
                    expected_revision=_fact_revision(candidate.id, candidate.status.value),
                    created_at=candidate.created_at.isoformat(),
                    confirm_label="确认记住",
                    cancel_label="不要记",
                    fields=(
                        CardField(label="项目", value=candidate.fact_key),
                        CardField(label="内容", value=candidate.value),
                        CardField(label="来自", value=review.correction_text),
                    ),
                    hint="想改？直接告诉 Tree 新的说法，我会重新提议。",
                )
            )
        return cards

    # ------------------------------------------------------------------ settling

    async def settle(
        self,
        thread_id: UUID,
        card_identifier: str,
        *,
        expected_revision: str,
        confirm: bool,
    ) -> ConversationReply:
        """Settle exactly the target one card names, or refuse because it moved on.

        Raises:
            UnknownConversationCard: the id names no kind this build settles.
            StaleConversationCard: the durable state no longer matches the card.
        """
        kind, target = parse_card_id(card_identifier)
        live = await self._revision_of(thread_id, card_identifier)
        if live is None or live != expected_revision:
            raise StaleConversationCard("this card no longer describes the current state")
        if kind is ConfirmationCardKind.MAIL_SEND:
            return await self._conversation.settle_external_card(
                thread_id, review_id=_uuid(target), confirm=confirm
            )
        if kind is ConfirmationCardKind.EHALL_CERTIFICATE:
            # The same deterministic settlement the terminal phrase reaches; only the card kind
            # differs, and the recorded phrase is the one a terminal user would have typed
            # (ADR-0045 §18-§19).
            return await self._conversation.settle_external_card(
                thread_id, review_id=_uuid(target), confirm=confirm
            )
        if kind is ConfirmationCardKind.FACT_CONFIRMATION:
            return await self._conversation.settle_fact_card(
                thread_id, candidate_id=target, confirm=confirm
            )
        operation_id = _uuid(target) if kind is ConfirmationCardKind.PLAN_APPLY else None
        turn_id = (
            await self._turn_of_operation(operation_id)
            if operation_id is not None
            else _uuid(target)
        )
        return await self._conversation.settle_group_card(
            thread_id, turn_id=turn_id, confirm=confirm
        )

    async def _turn_of_operation(self, operation_id: UUID) -> UUID:
        operation = await self._conversations.get_operation(operation_id)
        if operation is None:
            raise StaleConversationCard("this confirmation is gone")
        return operation.turn_id

    async def _revision_of(self, thread_id: UUID, card_identifier: str) -> str | None:
        """The revision the durable state currently has for one card, or `None` if it is gone."""
        for card in await self.cards(thread_id):
            if card.id == card_identifier:
                return card.expected_revision
        return None


def _mail_card(review: object, payload: dict[str, object]) -> ConfirmationCard:
    """The exact outbound message, rendered from the immutable action payload (ADR-0034 §7)."""
    account = str(payload.get("account_id") or "")
    sender = str(payload.get("from_address") or "")
    raw = payload.get("to_addresses")
    recipients = raw if isinstance(raw, list) else []
    to_line = "、".join(str(item) for item in recipients) if recipients else "（无收件人）"
    subject = str(payload.get("subject") or "")
    body = str(payload.get("body_text") or "")
    message_id = str(payload.get("rfc_message_id") or "")
    fields = [
        CardField(label="发件账号", value=f"{account}（{sender}）" if sender else account),
        CardField(label="收件人", value=to_line),
        CardField(label="主题", value=subject),
    ]
    if message_id:
        fields.append(CardField(label="Message-ID", value=message_id))
    return ConfirmationCard(
        id=card_id(ConfirmationCardKind.MAIL_SEND, review.id),  # type: ignore[attr-defined]
        kind=ConfirmationCardKind.MAIL_SEND,
        title="邮件发送预览",
        summary=f"发给 {to_line}。下面就是实际会发出的内容。",
        expected_revision=_review_revision(
            str(review.action_fingerprint),  # type: ignore[attr-defined]
            str(review.status),  # type: ignore[attr-defined]
            review.updated_at,  # type: ignore[attr-defined]
        ),
        created_at=review.created_at.isoformat(),  # type: ignore[attr-defined]
        confirm_label="确认发送",
        cancel_label="取消",
        severity="high",
        fields=tuple(fields[:FIELD_LIMIT]),
        items=({"body": body},),
        hint="想改？直接告诉 Tree 要改什么，我会重新准备一份给你确认。",
    )


def _recurring_card(turn_id: UUID, operations: list[ConversationOperation]) -> ConfirmationCard:
    """One card for one confirmation group of weekly rules (ADR-0036 supersession semantics)."""
    items: list[dict[str, str]] = []
    for operation in operations:
        arguments = operation.arguments
        if not isinstance(arguments, CalendarRecurringCreateWeeklyArguments):
            continue  # pragma: no cover - the repository decodes by operation type
        items.append(
            {
                "weekday": _WEEKDAYS.get(arguments.weekday, str(arguments.weekday)),
                "start": arguments.start_local_time,
                "end": arguments.end_local_time,
                "title": arguments.title,
                "timezone": arguments.timezone or "",
            }
        )
    revision = hashlib.sha256(
        "|".join(
            f"{operation.id}:{operation.operation_fingerprint}" for operation in operations
        ).encode("utf-8")
    ).hexdigest()
    return ConfirmationCard(
        id=card_id(ConfirmationCardKind.RECURRING_SCHEDULE, turn_id),
        kind=ConfirmationCardKind.RECURRING_SCHEDULE,
        title="固定安排",
        summary=f"这一组共 {len(items)} 条每周固定安排。",
        expected_revision=revision,
        created_at=operations[0].created_at.isoformat(),
        confirm_label="保存固定安排",
        cancel_label="取消",
        fields=(
            CardField(label="时间", value="按计划时区解读，保存后按此执行。"),
        ),
        items=tuple(items[:FIELD_LIMIT]),
        hint="想改？直接告诉 Tree 新的时间，我会重新问一次。",
    )


def _review_revision(fingerprint: str, status: str, updated_at: datetime) -> str:
    """The revision one reviewed external action currently has: fingerprint, state and update.

    Shared by both capabilities on purpose — it is derived from the review row itself, so a card
    for either kind goes stale the moment that row moves.
    """
    return f"{fingerprint}:{status}:{updated_at.isoformat()}"


def _ehall_certificate_card(review: object, payload: dict[str, object]) -> ConfirmationCard:
    """The exact certificate application, rendered from the immutable action payload.

    Every human-relevant submitted field is shown, with the page's own label. Nothing that is not
    submitted appears: the payload has no session, no cookie, no selector and no credential in it,
    and the consequence text is the fixed local one (ADR-0045 §12-§13).
    """
    service = str(payload.get("service_identity") or "")
    raw_fields = payload.get("fields")
    items: list[dict[str, str]] = []
    for item in raw_fields if isinstance(raw_fields, list) else []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "label": str(item.get("label") or item.get("key") or ""),
                "value": str(item.get("value") or ""),
            }
        )
    raw_materials = payload.get("required_materials")
    materials = [str(item) for item in raw_materials] if isinstance(raw_materials, list) else []
    fields = [CardField(label="服务", value=service)]
    if materials:
        fields.append(CardField(label="需要准备的材料", value="、".join(materials)))
    consequence = str(payload.get("consequence") or "")
    return ConfirmationCard(
        id=card_id(ConfirmationCardKind.EHALL_CERTIFICATE, review.id),  # type: ignore[attr-defined]
        kind=ConfirmationCardKind.EHALL_CERTIFICATE,
        title="证明申请提交预览",
        summary=f"{service}：下面就是实际会提交的内容。",
        expected_revision=_review_revision(
            str(review.action_fingerprint),  # type: ignore[attr-defined]
            str(review.status),  # type: ignore[attr-defined]
            review.updated_at,  # type: ignore[attr-defined]
        ),
        created_at=review.created_at.isoformat(),  # type: ignore[attr-defined]
        confirm_label="确认提交",
        cancel_label="取消",
        severity="high",
        fields=tuple(fields[:FIELD_LIMIT]),
        items=(*items[: FIELD_LIMIT - 1], {"consequence": consequence}),
        hint="想改？直接告诉 Tree 要改什么，我会重新准备一份给你确认。",
    )


def _operation_revision(operation: ConversationOperation) -> str:
    return (
        f"{operation.operation_fingerprint}:{operation.status.value}:"
        f"{operation.updated_at.isoformat()}"
    )


def _fact_revision(candidate_id: UUID, status: str) -> str:
    return f"{candidate_id}:{status}"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise StaleConversationCard(f"{value!r} is not a durable identity") from exc


__all__ = [
    "CARD_ID_SEPARATOR",
    "FIELD_LIMIT",
    "CardField",
    "ConfirmationCard",
    "ConfirmationCardKind",
    "ConversationCardService",
    "card_id",
    "parse_card_id",
]
