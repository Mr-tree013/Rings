"""What this build can actually do, read from the runtime instead of from prose (ADR-0035 §13-§19).

The Phase 10A prompt described the product in sentences, and within one phase those sentences were
already wrong: the conversation told users it could not send mail after Phase 10B had shipped
exact-review sending. A description that lives beside the code drifts; a description computed from
the registry and the loaded configuration cannot.

```text
CapabilitySnapshot = available operations (registry) + configuration (accounts, roots, timezone)
        │
        ├─► `system.capabilities`   what the user is told
        ├─► `/help`                 the same list, same words
        └─► model context           so a direct reply cannot contradict the build either
```

Four states are distinguished on purpose (§13): `AVAILABLE` (configured and offered),
`NOT_CONFIGURED` (offered, but this host has not set it up), `DISABLED` (a real, switchable
feature that is switched off), and `UNAVAILABLE` (not part of this product at all — arbitrary
outbound compose, eHall in a conversation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from assistant.domain.config import AssistantConfig
from assistant.domain.conversation_plan import ConversationOperationType


class CapabilityState(StrEnum):
    """Whether a capability can be used here, and why not when it cannot."""

    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CapabilityArea:
    """One user-level area and what this host can do with it."""

    name: str
    state: CapabilityState
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """The bounded representation used in the model context."""
        return {"state": self.state.value, **self.detail}


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """The whole picture, small enough to travel in every model request."""

    areas: tuple[CapabilityArea, ...]
    operation_types: tuple[str, ...]

    def area(self, name: str) -> CapabilityArea | None:
        """Return one area, or `None`."""
        return next((area for area in self.areas if area.name == name), None)

    def to_payload(self) -> dict[str, Any]:
        """The canonical JSON document placed in the model context."""
        return {area.name: area.to_payload() for area in self.areas}


def build_capability_snapshot(
    config: AssistantConfig | None,
    *,
    operation_types: tuple[ConversationOperationType, ...] | None = None,
) -> CapabilitySnapshot:
    """Read what this host can do from its configuration and the reviewed vocabulary."""
    operations = operation_types or tuple(ConversationOperationType)
    available = frozenset(operations)
    accounts = () if config is None else config.mail.accounts
    enabled_accounts = tuple(account for account in accounts if account.enabled)
    sending_accounts = tuple(
        account for account in enabled_accounts if account.smtp_host and account.from_address
    )
    roots = () if config is None else tuple(root for root in config.roots if root.enabled)
    planning = None if config is None else config.planning
    model_configured = config is not None and config.model is not None

    def state(*, offered: bool, configured: bool) -> CapabilityState:
        if not offered:
            return CapabilityState.UNAVAILABLE
        return CapabilityState.AVAILABLE if configured else CapabilityState.NOT_CONFIGURED

    areas = (
        CapabilityArea("tasks", CapabilityState.AVAILABLE, {"operations": 6}),
        CapabilityArea("calendar", CapabilityState.AVAILABLE, {"operations": 2}),
        CapabilityArea(
            "recurring_calendar",
            state(
                offered=(
                    ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY in available
                ),
                configured=planning is not None,
            ),
            {
                "operations": 4,
                "weekly_only": True,
                "one_weekday_per_rule": True,
                "planner_avoids_recurring": True,
                "unsupported": [
                    "odd/even weeks",
                    "every N weeks",
                    "monthly",
                    "yearly",
                    "holiday exceptions",
                ],
            },
        ),
        CapabilityArea("work", CapabilityState.AVAILABLE, {"operations": 1}),
        CapabilityArea("notifications", CapabilityState.AVAILABLE, {"operations": 2}),
        CapabilityArea(
            "planning",
            state(
                offered=ConversationOperationType.PLAN_PROPOSE_WEEK in available,
                configured=planning is not None,
            ),
            {
                "timezone": None if planning is None else planning.timezone,
                "proposals_need_confirmation": True,
            },
        ),
        CapabilityArea(
            "knowledge",
            state(
                offered=ConversationOperationType.KNOWLEDGE_ASK in available,
                configured=bool(roots),
            ),
            {"configured_roots": len(roots)},
        ),
        CapabilityArea(
            "mail_read",
            state(
                offered=ConversationOperationType.MAIL_LIST in available,
                configured=bool(enabled_accounts),
            ),
            {"configured_accounts": len(accounts), "enabled_accounts": len(enabled_accounts)},
        ),
        CapabilityArea(
            "mail_reply_draft",
            state(
                offered=ConversationOperationType.MAIL_REPLY_DRAFT in available,
                configured=bool(enabled_accounts) and model_configured,
            ),
            {"reply_only": True},
        ),
        CapabilityArea(
            "mail_send_after_confirmation",
            state(
                offered=ConversationOperationType.MAIL_PREPARE_REPLY_SEND in available,
                configured=bool(sending_accounts),
            ),
            {
                "exact_preview": True,
                "explicit_phrase_required": True,
                "configured_accounts": len(sending_accounts),
            },
        ),
        CapabilityArea(
            "mail_new_outbound_compose",
            state(
                offered=ConversationOperationType.MAIL_COMPOSE_NEW in available,
                configured=bool(sending_accounts),
            ),
            {
                "recipients": ["explicit_email", "contact", "self"],
                "exact_preview": True,
                "explicit_phrase_required": True,
                "configured_accounts": len(sending_accounts),
                "unsupported": [
                    "attachments",
                    "scheduled mail",
                    "automatic send",
                    "address-book lookup",
                ],
            },
        ),
        CapabilityArea(
            "contacts",
            state(
                offered=ConversationOperationType.CONTACT_CREATE in available,
                configured=True,
            ),
            {"operations": 4, "grants_authority": False},
        ),
        CapabilityArea("ehall", CapabilityState.UNAVAILABLE, {"reason": "not conversational"}),
    )
    return CapabilitySnapshot(
        areas=areas, operation_types=tuple(sorted(operation.value for operation in operations))
    )


__all__ = [
    "CapabilityArea",
    "CapabilitySnapshot",
    "CapabilityState",
    "build_capability_snapshot",
]
