"""Pairing, sessions and CSRF for the same-LAN control plane (ADR-0026).

```text
pw mobile pair                     phone: POST /pair {code}
      │                                  │
create a 256-bit code              hash it, consume it, mint two more
store only its hash                store only their hashes
print it once                      return them once, in a cookie + a header
```

Three rules hold the whole thing together:

- **the database never holds a usable secret.** Pairing, session and CSRF values are stored as
  SHA-256, so the plaintext exists only in the response that created it;
- **a pairing code is one-time and short-lived.** Ten minutes, consumed by a compare-and-set, so a
  code that leaked out of a screen recording is worth less than the effort of finding it;
- **a mutation needs all three of the session cookie, the CSRF header and the CSRF cookie.** A
  cross-site form can carry a cookie but cannot read one, so it cannot produce the header.

The randomness comes from an injected factory: this module may not import a system RNG, for the
same reason the approval service may not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    InvalidMobileSecurity,
    MobileCsrfRejected,
    MobileDisabled,
    MobilePairingTokenInvalid,
    MobileSessionInvalid,
    MobileSessionNotFound,
)
from assistant.domain.mobile import (
    MobilePairingIssued,
    MobilePairingToken,
    MobileSessionId,
    MobileSessionIssued,
    MobileWebSession,
    hash_mobile_token,
    pairing_ttl,
    session_ttl,
)
from assistant.ports.clock import Clock
from assistant.ports.mobile_session_repository import MobileSessionRepository

LOGGER = logging.getLogger("assistant.mobile")


@dataclass(frozen=True, slots=True)
class MobileStatus:
    """What the mobile control plane looks like locally. Read-only, and honest about reach."""

    enabled: bool
    bind: str
    port: int
    active_sessions: int
    total_sessions: int


class MobileAuthService:
    """Issues pairing codes and manages the sessions they create."""

    def __init__(
        self,
        repository: MobileSessionRepository,
        clock: Clock,
        *,
        token_factory: Callable[[], str],
        enabled: bool = True,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._token_factory = token_factory
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether this host has switched the control plane on."""
        return self._enabled

    async def create_pairing_token(self) -> MobilePairingIssued:
        """Mint one single-use code and return it exactly once.

        Raises:
            MobileDisabled: the host has not enabled the control plane.
            InvalidMobileSecurity: the token factory returned something unusable.
        """
        self._require_enabled()
        token = self._token_factory()
        now = self._clock.now()
        stored = await self._repository.add_pairing_token(
            MobilePairingToken(
                token_hash=hash_mobile_token(token),
                created_at=now,
                expires_at=now + pairing_ttl(),
            )
        )
        return MobilePairingIssued(pairing=stored, token=token)

    async def pair(self, token: str) -> MobileSessionIssued:
        """Redeem a pairing code for a session, or refuse without saying why.

        Raises:
            MobileDisabled: the host has not enabled the control plane.
            MobilePairingTokenInvalid: the code is unknown, expired or already used.
        """
        self._require_enabled()
        try:
            token_hash = hash_mobile_token(token)
        except InvalidMobileSecurity as exc:
            raise MobilePairingTokenInvalid(
                "the pairing code is not valid for this host"
            ) from exc
        session_token = self._token_factory()
        csrf_token = self._token_factory()
        now = self._clock.now()
        session = await self._repository.consume_pairing_and_add_session(
            token_hash=token_hash,
            session=MobileWebSession(
                session_hash=hash_mobile_token(session_token),
                csrf_hash=hash_mobile_token(csrf_token),
                created_at=now,
                expires_at=now + session_ttl(),
                last_seen_at=now,
            ),
            now=now,
        )
        LOGGER.info("mobile session paired")
        return MobileSessionIssued(
            session=session, session_token=session_token, csrf_token=csrf_token
        )

    async def authenticate(self, session_token: str | None) -> MobileWebSession:
        """Return the session a cookie names, or refuse.

        Raises:
            MobileSessionInvalid: no cookie, an unknown token, an expired session or a revoked one.
        """
        self._require_enabled()
        if not session_token:
            raise MobileSessionInvalid("no mobile session cookie was presented")
        try:
            session_hash = hash_mobile_token(session_token)
        except InvalidMobileSecurity as exc:
            raise MobileSessionInvalid("the mobile session is not valid") from exc
        session = await self._repository.get_session_by_hash(session_hash)
        if session is None or not session.is_active_at(self._clock.now()):
            raise MobileSessionInvalid("the mobile session is not valid")
        touched = await self._repository.touch_session(
            session.id, at=self._clock.now()
        )
        return touched if touched is not None else session

    def verify_csrf(
        self,
        session: MobileWebSession,
        *,
        header_token: str | None,
        cookie_token: str | None,
    ) -> None:
        """Require a mutation to carry the session's CSRF token, twice.

        Both the header and the cookie must match the stored hash. A cross-site request can send
        the cookie but cannot read it, so it cannot set the header — which is the whole defence.

        Raises:
            MobileCsrfRejected: either token is missing or does not match this session.
        """
        if not header_token or not cookie_token:
            raise MobileCsrfRejected(
                "a state-changing request needs both the CSRF header and its cookie"
            )
        if header_token != cookie_token:
            raise MobileCsrfRejected("the CSRF header and cookie do not match")
        if not session.matches_csrf(header_token):
            raise MobileCsrfRejected("the CSRF token does not belong to this session")

    async def logout(self, session: MobileWebSession) -> MobileWebSession:
        """Revoke the session a browser is using."""
        return await self._repository.revoke_session(
            session.id, at=self._clock.now()
        )

    async def list_sessions(self, *, include_inactive: bool = True) -> list[MobileWebSession]:
        """List sessions, newest first."""
        return await self._repository.list_sessions(include_inactive=include_inactive)

    async def revoke_session(self, reference: MobileSessionId | str) -> MobileWebSession:
        """Revoke one session by id or unique prefix.

        Raises:
            MobileSessionNotFound: no such session.
            AmbiguousId: a prefix matched several sessions.
        """
        session_id = (
            reference
            if not isinstance(reference, str)
            else await self._resolve(reference)
        )
        return await self._repository.revoke_session(
            session_id, at=self._clock.now()
        )

    async def status(self, *, bind: str, port: int, enabled: bool) -> MobileStatus:
        """Describe the control plane without starting it."""
        sessions = await self._repository.list_sessions(include_inactive=True)
        now = self._clock.now()
        return MobileStatus(
            enabled=enabled,
            bind=bind,
            port=port,
            active_sessions=sum(1 for item in sessions if item.is_active_at(now)),
            total_sessions=len(sessions),
        )

    async def _resolve(self, reference: str) -> MobileSessionId:
        text = reference.strip().lower()
        if not text:
            raise MobileSessionNotFound(reference)
        try:
            candidate = UUID(text)
        except ValueError:
            candidate = None
        sessions = await self._repository.list_sessions(include_inactive=True)
        if candidate is not None:
            if all(item.id != candidate for item in sessions):
                raise MobileSessionNotFound(candidate)
            return candidate
        matching = [item.id for item in sessions if str(item.id).startswith(text)]
        if not matching:
            raise MobileSessionNotFound(reference)
        if len(matching) > 1:
            raise AmbiguousId(reference, len(matching))
        return matching[0]

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise MobileDisabled(
                "the mobile control plane is disabled; set [mobile] enabled = true to use it"
            )


__all__ = ["MobileAuthService", "MobileStatus"]
