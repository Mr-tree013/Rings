"""SQLite implementation of the pairing and session store (ADR-0009, ADR-0026).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

`consume_pairing_and_add_session` is one transaction, and the consumption itself is a
compare-and-set (`WHERE consumed_at IS NULL`): two phones racing on the same code cannot both pair,
and a crash cannot leave a code spent without the session it bought.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import (
    MobilePairingTokenInvalid,
    MobileSessionNotFound,
)
from assistant.domain.mobile import (
    MobilePairingToken,
    MobilePairingTokenId,
    MobileSessionId,
    MobileWebSession,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_PAIRING_FIELDS = "id, token_hash, created_at, expires_at, consumed_at"
_SESSION_FIELDS = (
    "id, session_hash, csrf_hash, created_at, expires_at, last_seen_at, revoked_at"
)


class SqliteMobileSessionRepository:
    """Durable pairing tokens and web sessions."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def add_pairing_token(self, pairing: MobilePairingToken) -> MobilePairingToken:
        return await asyncio.to_thread(self._add_pairing_token_sync, pairing)

    async def get_pairing_token(self, token_hash: str) -> MobilePairingToken | None:
        return await asyncio.to_thread(self._get_pairing_token_sync, token_hash)

    async def get_pairing_token_by_id(
        self, pairing_id: MobilePairingTokenId
    ) -> MobilePairingToken | None:
        return await asyncio.to_thread(self._get_pairing_token_by_id_sync, pairing_id)

    async def consume_pairing_and_add_session(
        self,
        *,
        token_hash: str,
        session: MobileWebSession,
        now: datetime,
    ) -> MobileWebSession:
        return await asyncio.to_thread(
            self._consume_pairing_and_add_session_sync, token_hash, session, now
        )

    async def get_session_by_hash(self, session_hash: str) -> MobileWebSession | None:
        return await asyncio.to_thread(self._get_session_by_hash_sync, session_hash)

    async def get_session(self, session_id: MobileSessionId) -> MobileWebSession | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    async def list_sessions(
        self, *, include_inactive: bool = True
    ) -> list[MobileWebSession]:
        return await asyncio.to_thread(self._list_sessions_sync, include_inactive)

    async def touch_session(
        self, session_id: MobileSessionId, *, at: datetime
    ) -> MobileWebSession | None:
        return await asyncio.to_thread(self._touch_session_sync, session_id, at)

    async def revoke_session(
        self, session_id: MobileSessionId, *, at: datetime
    ) -> MobileWebSession:
        return await asyncio.to_thread(self._revoke_session_sync, session_id, at)

    # ------------------------------------------------------------ blocking internals

    def _add_pairing_token_sync(self, pairing: MobilePairingToken) -> MobilePairingToken:
        try:
            with self._database.connect() as connection, transaction(connection):
                # A pairing code is a 10-minute capability, not history: once it has been spent or
                # its window has passed it has no reader, and `rings up` mints one per launch.
                # Pruning here keeps the table's size a function of the TTL instead of the number
                # of launches. A live, unconsumed code is never touched.
                connection.execute(
                    "DELETE FROM mobile_pairing_tokens "
                    "WHERE consumed_at IS NOT NULL OR expires_at <= ?",
                    (to_utc_iso(pairing.created_at),),
                )
                connection.execute(
                    f"INSERT INTO mobile_pairing_tokens ({_PAIRING_FIELDS}) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        str(pairing.id),
                        pairing.token_hash,
                        to_utc_iso(pairing.created_at),
                        to_utc_iso(pairing.expires_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a pairing token: {exc}") from exc
        return pairing

    def _get_pairing_token_sync(self, token_hash: str) -> MobilePairingToken | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PAIRING_FIELDS} FROM mobile_pairing_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return None if row is None else _row_to_pairing(row)

    def _get_pairing_token_by_id_sync(
        self, pairing_id: MobilePairingTokenId
    ) -> MobilePairingToken | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PAIRING_FIELDS} FROM mobile_pairing_tokens WHERE id = ?",
                (str(pairing_id),),
            ).fetchone()
        return None if row is None else _row_to_pairing(row)

    def _consume_pairing_and_add_session_sync(
        self, token_hash: str, session: MobileWebSession, now: datetime
    ) -> MobileWebSession:
        try:
            with self._database.connect() as connection, transaction(connection):
                row = connection.execute(
                    f"SELECT {_PAIRING_FIELDS} FROM mobile_pairing_tokens WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    raise MobilePairingTokenInvalid(
                        "the pairing code is not valid for this host"
                    )
                pairing = _row_to_pairing(row)
                if not pairing.is_usable_at(now):
                    raise MobilePairingTokenInvalid(
                        "the pairing code is not valid for this host"
                    )
                cursor = connection.execute(
                    "UPDATE mobile_pairing_tokens SET consumed_at = ? "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (to_utc_iso(now), str(pairing.id)),
                )
                if cursor.rowcount == 0:  # pragma: no cover - read in this transaction
                    raise MobilePairingTokenInvalid(
                        "the pairing code is not valid for this host"
                    )
                connection.execute(
                    f"INSERT INTO mobile_sessions ({_SESSION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        str(session.id),
                        session.session_hash,
                        session.csrf_hash,
                        to_utc_iso(session.created_at),
                        to_utc_iso(session.expires_at),
                        to_utc_iso(session.last_seen_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not create a mobile session: {exc}") from exc
        return session

    def _get_session_by_hash_sync(self, session_hash: str) -> MobileWebSession | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_FIELDS} FROM mobile_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def _get_session_sync(self, session_id: MobileSessionId) -> MobileWebSession | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_FIELDS} FROM mobile_sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def _list_sessions_sync(self, include_inactive: bool) -> list[MobileWebSession]:
        statement = f"SELECT {_SESSION_FIELDS} FROM mobile_sessions"
        parameters: tuple[object, ...] = ()
        if not include_inactive:
            statement += " WHERE revoked_at IS NULL"
        statement += " ORDER BY created_at DESC, id"
        with self._database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_row_to_session(row) for row in rows]

    def _touch_session_sync(
        self, session_id: MobileSessionId, at: datetime
    ) -> MobileWebSession | None:
        try:
            with self._database.connect() as connection, transaction(connection):
                cursor = connection.execute(
                    "UPDATE mobile_sessions SET last_seen_at = ? "
                    "WHERE id = ? AND revoked_at IS NULL",
                    (to_utc_iso(at), str(session_id)),
                )
                if cursor.rowcount == 0:
                    return None
                row = connection.execute(
                    f"SELECT {_SESSION_FIELDS} FROM mobile_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:  # pragma: no cover - defensive
            raise CommitmentStoreError(f"could not touch a mobile session: {exc}") from exc
        return None if row is None else _row_to_session(row)

    def _revoke_session_sync(
        self, session_id: MobileSessionId, at: datetime
    ) -> MobileWebSession:
        try:
            with self._database.connect() as connection, transaction(connection):
                row = connection.execute(
                    f"SELECT {_SESSION_FIELDS} FROM mobile_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    raise MobileSessionNotFound(session_id)
                connection.execute(
                    "UPDATE mobile_sessions SET revoked_at = COALESCE(revoked_at, ?) "
                    "WHERE id = ?",
                    (to_utc_iso(at), str(session_id)),
                )
                revoked = connection.execute(
                    f"SELECT {_SESSION_FIELDS} FROM mobile_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
        except sqlite3.IntegrityError as exc:  # pragma: no cover - defensive
            raise CommitmentStoreError(f"could not revoke a mobile session: {exc}") from exc
        if revoked is None:  # pragma: no cover - the update guarantees a row
            raise CommitmentStoreError(f"mobile session {session_id} vanished")
        return _row_to_session(revoked)


def _row_to_pairing(row: sqlite3.Row) -> MobilePairingToken:
    consumed = row["consumed_at"]
    return MobilePairingToken(
        id=UUID(str(row["id"])),
        token_hash=str(row["token_hash"]),
        created_at=from_utc_iso(str(row["created_at"])),
        expires_at=from_utc_iso(str(row["expires_at"])),
        consumed_at=None if consumed is None else from_utc_iso(str(consumed)),
    )


def _row_to_session(row: sqlite3.Row) -> MobileWebSession:
    revoked = row["revoked_at"]
    return MobileWebSession(
        id=UUID(str(row["id"])),
        session_hash=str(row["session_hash"]),
        csrf_hash=str(row["csrf_hash"]),
        created_at=from_utc_iso(str(row["created_at"])),
        expires_at=from_utc_iso(str(row["expires_at"])),
        last_seen_at=from_utc_iso(str(row["last_seen_at"])),
        revoked_at=None if revoked is None else from_utc_iso(str(revoked)),
    )


__all__ = ["SqliteMobileSessionRepository"]
