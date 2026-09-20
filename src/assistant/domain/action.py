"""ActionRequest: one exact, immutable, prepared external side effect (ADR-0023).

This module is where "what exactly was approved" becomes a value. The payload is canonicalised
once, at preparation time, and its SHA-256 *is* the action's identity: everything downstream —
the challenge, the approval, the execution — compares fingerprints, never prose.

Two consequences follow from that and are enforced here rather than promised elsewhere:

- **the payload cannot change.** There is no edit method, and construction re-derives the
  fingerprint from the canonical bytes, so a row whose payload and fingerprint disagree cannot
  even be loaded. Changing the content means preparing a *new* action, which means a new
  approval;
- **naming a type is not a capability.** `mail.send` and `ehall.submit-certificate` are valid
  `ActionType` values in this phase and have no executor. The type vocabulary is deliberately
  open, and the authority to act lives in a registry that Phase 6A leaves empty.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from assistant.domain.case import CaseId
from assistant.domain.errors import InvalidActionPayload, InvalidActionRequest

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
"""The JSON values a payload may be built from, after canonicalisation."""

ActionRequestId = UUID
"""Stable identity of one prepared action."""

ACTION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$")
ACTION_TYPE_MAX_LENGTH = 128
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def new_action_request_id() -> ActionRequestId:
    """Generate a fresh action identity."""
    return uuid4()


class ActionRequestStatus(StrEnum):
    """Lifecycle of a prepared action. There is no path back from a terminal state."""

    PREPARED = "prepared"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActionType:
    """A validated namespaced identifier such as `mail.send`.

    Being valid says nothing about whether anything can perform it: the executor set is a
    separate, deliberately empty registry.
    """

    value: str

    def __post_init__(self) -> None:
        candidate = self.value.strip()
        if len(candidate) > ACTION_TYPE_MAX_LENGTH:
            raise InvalidActionRequest(
                f"action type must be at most {ACTION_TYPE_MAX_LENGTH} characters"
            )
        if not ACTION_TYPE_PATTERN.match(candidate):
            raise InvalidActionRequest(
                f"action type must be a namespaced identifier matching "
                f"{ACTION_TYPE_PATTERN.pattern} (got {self.value!r})"
            )
        object.__setattr__(self, "value", candidate)

    @property
    def namespace(self) -> str:
        """The part before the first dot, e.g. `mail`."""
        return self.value.split(".", 1)[0]

    def __str__(self) -> str:
        return self.value


def canonical_action_payload(payload: object) -> str:
    """Canonical JSON text for one action payload, or raise `InvalidActionPayload`.

    Only JSON values are accepted: `null`, booleans, integers, finite floats, strings, sequences
    of those and mappings with string keys. `NaN`, infinities, `bytes`, `datetime` objects and
    arbitrary Python objects are refused rather than coerced — a payload that cannot be written
    down exactly is not an action this project is willing to approve.
    """
    normalised = _normalise(payload, path="payload")
    return json.dumps(
        normalised,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def action_payload_fingerprint(payload: object) -> str:
    """The SHA-256 of one payload's canonical JSON text, as 64 lowercase hex characters."""
    return hashlib.sha256(canonical_action_payload(payload).encode("utf-8")).hexdigest()


def fingerprint_of_canonical_json(payload_json: str) -> str:
    """Re-hash stored canonical text. This is what an execution re-validates."""
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _normalise(value: object, *, path: str) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidActionPayload(f"{path} must be a finite number, not {value!r}")
        return value
    if isinstance(value, Mapping):
        normalised: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidActionPayload(
                    f"{path} must use string keys, not {type(key).__name__}"
                )
            normalised[key] = _normalise(item, path=f"{path}.{key}")
        return normalised
    if isinstance(value, (list, tuple)):
        if isinstance(value, (bytes, bytearray, str)):
            raise InvalidActionPayload(f"{path} must be JSON data, not {type(value).__name__}")
        return [
            _normalise(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalise(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise InvalidActionPayload(
        f"{path} must be JSON data (null, bool, int, finite float, str, list, object), "
        f"not {type(value).__name__}"
    )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidActionRequest(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One prepared side effect: immutable content, one fingerprint, one lifecycle."""

    case_id: CaseId
    action_type: ActionType
    payload_json: str
    fingerprint: str
    created_at: datetime
    id: ActionRequestId = field(default_factory=new_action_request_id)
    status: ActionRequestStatus = ActionRequestStatus.PREPARED
    executed_at: datetime | None = None
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.payload_json.strip():
            raise InvalidActionRequest("an action request needs a payload")
        try:
            decoded: Any = json.loads(self.payload_json)
        except ValueError as exc:
            raise InvalidActionRequest(f"an action payload must be JSON: {exc}") from exc
        if canonical_action_payload(decoded) != self.payload_json:
            raise InvalidActionRequest(
                "an action payload must already be in canonical form; prepare it through "
                "ActionRequest.prepare"
            )
        if not FINGERPRINT_PATTERN.match(self.fingerprint):
            raise InvalidActionRequest(
                "an action fingerprint must be 64 lowercase hex characters"
            )
        if fingerprint_of_canonical_json(self.payload_json) != self.fingerprint:
            raise InvalidActionRequest(
                "the stored fingerprint does not match the stored payload"
            )
        _require_aware(self.created_at, "created_at")
        if self.status is ActionRequestStatus.PREPARED:
            if self.executed_at is not None or self.cancelled_at is not None:
                raise InvalidActionRequest(
                    "a PREPARED action must not carry executed_at or cancelled_at"
                )
        elif self.status is ActionRequestStatus.EXECUTED:
            if self.executed_at is None:
                raise InvalidActionRequest("an EXECUTED action must carry executed_at")
            if self.cancelled_at is not None:
                raise InvalidActionRequest("an EXECUTED action must not carry cancelled_at")
        else:
            if self.cancelled_at is None:
                raise InvalidActionRequest("a CANCELLED action must carry cancelled_at")
            if self.executed_at is not None:
                raise InvalidActionRequest("a CANCELLED action must not carry executed_at")

    @classmethod
    def prepare(
        cls,
        *,
        case_id: CaseId,
        action_type: ActionType | str,
        payload: object,
        at: datetime,
        action_id: ActionRequestId | None = None,
    ) -> ActionRequest:
        """Prepare one action: canonicalise the payload, then fingerprint it."""
        canonical = canonical_action_payload(payload)
        return cls(
            id=new_action_request_id() if action_id is None else action_id,
            case_id=case_id,
            action_type=action_type
            if isinstance(action_type, ActionType)
            else ActionType(action_type),
            payload_json=canonical,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            created_at=at,
        )

    @property
    def payload(self) -> JsonValue:
        """The payload, parsed back from the exact canonical text that was fingerprinted."""
        decoded: JsonValue = json.loads(self.payload_json)
        return decoded

    @property
    def is_prepared(self) -> bool:
        """Whether this action is still waiting to be executed."""
        return self.status is ActionRequestStatus.PREPARED

    def fingerprint_matches(self) -> bool:
        """Re-hash the payload and compare. Never trust the stored field alone."""
        return fingerprint_of_canonical_json(self.payload_json) == self.fingerprint

    def mark_executed(self, at: datetime) -> ActionRequest:
        """Move a PREPARED action to EXECUTED."""
        _require_aware(at, "at")
        if self.status is not ActionRequestStatus.PREPARED:
            raise InvalidActionRequest(
                f"only a PREPARED action can be executed, not a {self.status} one"
            )
        return replace(self, status=ActionRequestStatus.EXECUTED, executed_at=at)

    def cancel(self, at: datetime) -> ActionRequest:
        """Move a PREPARED action to CANCELLED."""
        _require_aware(at, "at")
        if self.status is not ActionRequestStatus.PREPARED:
            raise InvalidActionRequest(
                f"only a PREPARED action can be cancelled, not a {self.status} one"
            )
        return replace(self, status=ActionRequestStatus.CANCELLED, cancelled_at=at)


__all__ = [
    "ACTION_TYPE_MAX_LENGTH",
    "ACTION_TYPE_PATTERN",
    "FINGERPRINT_PATTERN",
    "ActionRequest",
    "ActionRequestId",
    "ActionRequestStatus",
    "ActionType",
    "JsonScalar",
    "JsonValue",
    "action_payload_fingerprint",
    "canonical_action_payload",
    "fingerprint_of_canonical_json",
    "new_action_request_id",
]
