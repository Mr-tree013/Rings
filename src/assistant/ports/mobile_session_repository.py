"""MobileSessionRepository port: pairing tokens and web sessions (ADR-0026).

Every method takes or returns *hashes*, never plaintext. That is the port's whole design: the
repository cannot leak a token because it is never given one, and the application service is the
only place a plaintext value exists — for exactly as long as it takes to return it once.

The two interesting operations are atomic for the same reason Phase 6A's approval is:

- **redeeming a pairing code** must consume it and create the session together, or a crash could
  leave a code spent with no session (or a session with a code still usable);
- **revoking a session** is a compare-and-set on `revoked_at`, so two callers cannot both think
  they were the one who cut it off.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from assistant.domain.mobile import (
    MobilePairingToken,
    MobilePairingTokenId,
    MobileSessionId,
    MobileWebSession,
)


class MobileSessionRepository(Protocol):
    """Durable pairing tokens and web sessions, stored as hashes."""

    async def add_pairing_token(self, pairing: MobilePairingToken) -> MobilePairingToken:
        """Store one pairing token hash."""
        ...

    async def get_pairing_token(
        self, token_hash: str
    ) -> MobilePairingToken | None:
        """Return the pairing record for one token hash, or `None`."""
        ...

    async def get_pairing_token_by_id(
        self, pairing_id: MobilePairingTokenId
    ) -> MobilePairingToken | None:
        """Return one pairing record, or `None`."""
        ...

    async def consume_pairing_and_add_session(
        self,
        *,
        token_hash: str,
        session: MobileWebSession,
        now: datetime,
    ) -> MobileWebSession:
        """Consume one pairing code and create the session it buys, atomically.

        Raises:
            MobilePairingTokenInvalid: the code is unknown, expired or already used.
        """
        ...

    async def get_session_by_hash(self, session_hash: str) -> MobileWebSession | None:
        """Return the session for one token hash, or `None`."""
        ...

    async def get_session(self, session_id: MobileSessionId) -> MobileWebSession | None:
        """Return one session by identity, or `None`."""
        ...

    async def list_sessions(self, *, include_inactive: bool = True) -> list[MobileWebSession]:
        """List sessions, newest first."""
        ...

    async def touch_session(
        self, session_id: MobileSessionId, *, at: datetime
    ) -> MobileWebSession | None:
        """Record that a session was just used."""
        ...

    async def revoke_session(
        self, session_id: MobileSessionId, *, at: datetime
    ) -> MobileWebSession:
        """Revoke one session, or report that it does not exist.

        Raises:
            MobileSessionNotFound: no such session.
        """
        ...


__all__ = ["MobileSessionRepository"]
