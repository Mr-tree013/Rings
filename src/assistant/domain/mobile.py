"""The mobile control plane's security model: pairing tokens and web sessions (ADR-0026).

Two values, and the same rule for both: **the plaintext exists only in the response that created
it.** What is durable is a SHA-256, so a database copy, a log file or a crash dump is not a key to
the assistant. A pairing token is single-use and expires in ten minutes; a session is revocable,
expires in thirty days, and carries the hash of the CSRF token that must accompany every mutation.

A session is a *browser* capability, not an authority: it can read state, edit a draft, and create
an approval. It cannot execute anything, and there is no value in this module that could express
"run the action".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidMobileSecurity

MobilePairingTokenId = UUID
MobileSessionId = UUID

PAIRING_TTL_SECONDS = 600
"""How long a one-time pairing code stays usable (10 minutes)."""

SESSION_TTL_SECONDS = 30 * 24 * 3600
"""How long a paired browser stays signed in (30 days)."""

MIN_TOKEN_CHARS = 32
"""A token shorter than this is a configuration error, not a weak credential to accept."""


def new_pairing_token_id() -> MobilePairingTokenId:
    """Generate a fresh pairing record identity."""
    return uuid4()


def new_mobile_session_id() -> MobileSessionId:
    """Generate a fresh session record identity."""
    return uuid4()


def pairing_ttl() -> timedelta:
    """The fixed lifetime of a pairing code."""
    return timedelta(seconds=PAIRING_TTL_SECONDS)


def session_ttl() -> timedelta:
    """The fixed lifetime of a web session."""
    return timedelta(seconds=SESSION_TTL_SECONDS)


def hash_mobile_token(token: str) -> str:
    """The SHA-256 of a pairing, session or CSRF token, as 64 lowercase hex characters.

    Hashes are the only representation the system stores, so comparison always happens on hashes
    and the plaintext never has to be recovered from anywhere.
    """
    if not isinstance(token, str) or len(token.strip()) < MIN_TOKEN_CHARS:
        raise InvalidMobileSecurity(
            f"a mobile token must be at least {MIN_TOKEN_CHARS} characters"
        )
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidMobileSecurity(f"{field_name} must be timezone-aware")


def _require_optional_aware(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def _require_hash(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise InvalidMobileSecurity(f"{field_name} must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class MobilePairingToken:
    """One single-use, short-lived code that pairs a phone with this host."""

    token_hash: str
    created_at: datetime
    expires_at: datetime
    id: MobilePairingTokenId = field(default_factory=new_pairing_token_id)
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_hash(self.token_hash, "a pairing token hash")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        _require_optional_aware(self.consumed_at, "consumed_at")
        if self.expires_at <= self.created_at:
            raise InvalidMobileSecurity("a pairing token must expire after it was created")

    def is_expired(self, at: datetime) -> bool:
        """Whether the code can no longer be redeemed."""
        _require_aware(at, "at")
        return at >= self.expires_at

    def is_consumed(self) -> bool:
        """Whether the code has already been used."""
        return self.consumed_at is not None

    def is_usable_at(self, at: datetime) -> bool:
        """Whether this code could pair a phone right now."""
        return not (self.is_consumed() or self.is_expired(at))


@dataclass(frozen=True, slots=True)
class MobileWebSession:
    """One paired browser: a revocable capability with a CSRF companion."""

    session_hash: str
    csrf_hash: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    id: MobileSessionId = field(default_factory=new_mobile_session_id)
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_hash(self.session_hash, "a session token hash")
        _require_hash(self.csrf_hash, "a CSRF token hash")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        _require_aware(self.last_seen_at, "last_seen_at")
        _require_optional_aware(self.revoked_at, "revoked_at")
        if self.expires_at <= self.created_at:
            raise InvalidMobileSecurity("a session must expire after it was created")

    def is_expired(self, at: datetime) -> bool:
        """Whether the session has aged out."""
        _require_aware(at, "at")
        return at >= self.expires_at

    def is_revoked(self) -> bool:
        """Whether the session was cut off on purpose."""
        return self.revoked_at is not None

    def is_active_at(self, at: datetime) -> bool:
        """Whether this session may be used right now."""
        return not (self.is_revoked() or self.is_expired(at))

    def matches_csrf(self, token: str) -> bool:
        """Whether a presented CSRF token is this session's, compared on hashes."""
        try:
            presented = hash_mobile_token(token)
        except InvalidMobileSecurity:
            return False
        return presented == self.csrf_hash


@dataclass(frozen=True, slots=True)
class MobilePairingIssued:
    """A stored pairing record plus the one-and-only sight of its plaintext code."""

    pairing: MobilePairingToken
    token: str

    def __post_init__(self) -> None:
        if not self.token:
            raise InvalidMobileSecurity("an issued pairing must carry its token")


@dataclass(frozen=True, slots=True)
class MobileSessionIssued:
    """A stored session plus the plaintext session and CSRF tokens the browser now holds."""

    session: MobileWebSession
    session_token: str
    csrf_token: str


class MobileBindMode(StrEnum):
    """How far the web server reaches."""

    LOOPBACK = "loopback"
    LAN = "lan"

    @property
    def host(self) -> str:
        """The address the server binds.

        `lan` binds every interface, and the *private-client* middleware is then what keeps the
        control plane on the trusted network — the bind address is not the boundary.
        """
        return "127.0.0.1" if self is MobileBindMode.LOOPBACK else "0.0.0.0"


__all__ = [
    "MIN_TOKEN_CHARS",
    "PAIRING_TTL_SECONDS",
    "SESSION_TTL_SECONDS",
    "MobileBindMode",
    "MobilePairingIssued",
    "MobilePairingToken",
    "MobilePairingTokenId",
    "MobileSessionId",
    "MobileSessionIssued",
    "MobileWebSession",
    "hash_mobile_token",
    "new_mobile_session_id",
    "new_pairing_token_id",
    "pairing_ttl",
    "session_ttl",
]
