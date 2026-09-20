"""SQLite implementation of the WebWatchRepository port (ADR-0009, ADR-0029).

Every public method is a thin async boundary over a blocking `_*_sync` method, and each blocking
method opens its own connection inside the worker thread that uses it.

`record_observation` is one `BEGIN IMMEDIATE`: the observation row and the target state that points
at it commit together, so the next poll can never see one without the other. The bridge rows are
written separately and idempotently (`ON CONFLICT DO NOTHING`), because the event they name is
ingested through `EventInbox` and a crash between the two has to be repairable.

Only the *relative* storage key is persisted. The snapshot text itself lives in the
content-addressed store under the runtime data directory.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from uuid import UUID

from assistant.domain.errors import (
    AmbiguousId,
    WebObservationNotFound,
)
from assistant.domain.inbound_event import EventId
from assistant.domain.web_watch import (
    WebObservation,
    WebObservationId,
    WebTargetId,
    WebWatchState,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_OBSERVATION_FIELDS = (
    "id, target_id, url, content_sha256, storage_key, previous_observation_id, is_baseline, "
    "fetched_at"
)
_STATE_FIELDS = (
    "target_id, url, content_sha256, latest_observation_id, etag, last_modified, "
    "checks_since_full, last_checked_at, last_changed_at, updated_at"
)


class SqliteWebWatchRepository:
    """Durable watcher state, versioned observations and their event links."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_state(self, target_id: WebTargetId) -> WebWatchState | None:
        return await asyncio.to_thread(self._get_state_sync, target_id)

    async def record_observation(
        self, observation: WebObservation, state: WebWatchState
    ) -> WebObservation:
        return await asyncio.to_thread(
            self._record_observation_sync, observation, state
        )

    async def save_state(self, state: WebWatchState) -> WebWatchState:
        return await asyncio.to_thread(self._save_state_sync, state)

    async def get_observation(
        self, observation_id: WebObservationId
    ) -> WebObservation | None:
        return await asyncio.to_thread(self._get_observation_sync, observation_id)

    async def list_observations(
        self, *, target_id: WebTargetId | None = None, limit: int | None = 20
    ) -> list[WebObservation]:
        return await asyncio.to_thread(self._list_observations_sync, target_id, limit)

    async def resolve_observation_id(self, reference: str) -> WebObservationId:
        observations = await self.list_observations(limit=None)
        return _resolve_id(
            reference,
            [item.id for item in observations],
            WebObservationNotFound(reference),
        )

    async def link_event(
        self,
        observation_id: WebObservationId,
        inbound_event_id: EventId,
        *,
        linked_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._link_event_sync, observation_id, inbound_event_id, linked_at
        )

    async def get_linked_event_id(
        self, observation_id: WebObservationId
    ) -> EventId | None:
        return await asyncio.to_thread(self._get_linked_event_id_sync, observation_id)

    async def list_unlinked_observations(self, *, limit: int) -> list[WebObservation]:
        return await asyncio.to_thread(self._list_unlinked_sync, limit)

    # ------------------------------------------------------------ blocking internals

    def _get_state_sync(self, target_id: WebTargetId) -> WebWatchState | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_STATE_FIELDS} FROM web_watch_state WHERE target_id = ?",
                (target_id,),
            ).fetchone()
        return None if row is None else _row_to_state(row)

    def _record_observation_sync(
        self, observation: WebObservation, state: WebWatchState
    ) -> WebObservation:
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO web_observations ({_OBSERVATION_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _observation_parameters(observation),
                )
                _upsert_state(connection, state)
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not record the web observation: {exc}"
            ) from exc
        return observation

    def _save_state_sync(self, state: WebWatchState) -> WebWatchState:
        try:
            with self._database.connect() as connection, transaction(connection):
                _upsert_state(connection, state)
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store the watch state: {exc}") from exc
        return state

    def _get_observation_sync(
        self, observation_id: WebObservationId
    ) -> WebObservation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_OBSERVATION_FIELDS} FROM web_observations WHERE id = ?",
                (str(observation_id),),
            ).fetchone()
        return None if row is None else _row_to_observation(row)

    def _list_observations_sync(
        self, target_id: WebTargetId | None, limit: int | None
    ) -> list[WebObservation]:
        statement = f"SELECT {_OBSERVATION_FIELDS} FROM web_observations"
        parameters: list[object] = []
        if target_id is not None:
            statement += " WHERE target_id = ?"
            parameters.append(target_id)
        statement += " ORDER BY fetched_at DESC, id DESC"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return [_row_to_observation(row) for row in rows]

    def _link_event_sync(
        self,
        observation_id: WebObservationId,
        inbound_event_id: EventId,
        linked_at: datetime,
    ) -> None:
        with self._database.connect() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO web_observation_event_links "
                "(observation_id, inbound_event_id, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT (observation_id) DO NOTHING",
                (str(observation_id), str(inbound_event_id), to_utc_iso(linked_at)),
            )

    def _get_linked_event_id_sync(
        self, observation_id: WebObservationId
    ) -> EventId | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT inbound_event_id FROM web_observation_event_links "
                "WHERE observation_id = ?",
                (str(observation_id),),
            ).fetchone()
        return None if row is None else UUID(str(row["inbound_event_id"]))

    def _list_unlinked_sync(self, limit: int) -> list[WebObservation]:
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_OBSERVATION_FIELDS} FROM web_observations AS o "
                "WHERE o.is_baseline = 0 "
                "AND NOT EXISTS (SELECT 1 FROM web_observation_event_links AS l "
                "WHERE l.observation_id = o.id) "
                "ORDER BY o.fetched_at, o.id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_observation(row) for row in rows]


def _upsert_state(connection: sqlite3.Connection, state: WebWatchState) -> None:
    connection.execute(
        f"INSERT INTO web_watch_state ({_STATE_FIELDS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (target_id) DO UPDATE SET "
        "url = excluded.url, content_sha256 = excluded.content_sha256, "
        "latest_observation_id = excluded.latest_observation_id, etag = excluded.etag, "
        "last_modified = excluded.last_modified, "
        "checks_since_full = excluded.checks_since_full, "
        "last_checked_at = excluded.last_checked_at, "
        "last_changed_at = excluded.last_changed_at, updated_at = excluded.updated_at",
        _state_parameters(state),
    )


def _resolve_id[T](reference: str, identifiers: list[UUID], missing: Exception) -> UUID:
    """Resolve a full UUID or a unique prefix against a list of identities."""
    text = reference.strip().lower()
    if not text:
        raise missing
    try:
        candidate = UUID(text)
    except ValueError:
        candidate = None
    if candidate is not None:
        if candidate not in identifiers:
            raise missing
        return candidate
    matching = [item for item in identifiers if str(item).startswith(text)]
    if not matching:
        raise missing
    if len(matching) > 1:
        raise AmbiguousId(reference, len(matching))
    return matching[0]


def _observation_parameters(observation: WebObservation) -> tuple[object, ...]:
    return (
        str(observation.id),
        observation.target_id,
        observation.url,
        observation.content_sha256,
        observation.storage_key,
        None
        if observation.previous_observation_id is None
        else str(observation.previous_observation_id),
        1 if observation.is_baseline else 0,
        to_utc_iso(observation.fetched_at),
    )


def _state_parameters(state: WebWatchState) -> tuple[object, ...]:
    return (
        state.target_id,
        state.url,
        state.content_sha256,
        None if state.latest_observation_id is None else str(state.latest_observation_id),
        state.etag,
        state.last_modified,
        state.checks_since_full,
        None if state.last_checked_at is None else to_utc_iso(state.last_checked_at),
        None if state.last_changed_at is None else to_utc_iso(state.last_changed_at),
        to_utc_iso(state.updated_at),
    )


def _row_to_observation(row: sqlite3.Row) -> WebObservation:
    previous = row["previous_observation_id"]
    return WebObservation(
        id=UUID(str(row["id"])),
        target_id=str(row["target_id"]),
        url=str(row["url"]),
        content_sha256=str(row["content_sha256"]),
        storage_key=str(row["storage_key"]),
        previous_observation_id=None if previous is None else UUID(str(previous)),
        is_baseline=bool(row["is_baseline"]),
        fetched_at=from_utc_iso(str(row["fetched_at"])),
    )


def _row_to_state(row: sqlite3.Row) -> WebWatchState:
    latest = row["latest_observation_id"]
    checked = row["last_checked_at"]
    changed = row["last_changed_at"]
    return WebWatchState(
        target_id=str(row["target_id"]),
        url=str(row["url"]),
        content_sha256=None if row["content_sha256"] is None else str(row["content_sha256"]),
        latest_observation_id=None if latest is None else UUID(str(latest)),
        etag=None if row["etag"] is None else str(row["etag"]),
        last_modified=None
        if row["last_modified"] is None
        else str(row["last_modified"]),
        checks_since_full=int(row["checks_since_full"]),
        last_checked_at=None if checked is None else from_utc_iso(str(checked)),
        last_changed_at=None if changed is None else from_utc_iso(str(changed)),
        updated_at=from_utc_iso(str(row["updated_at"])),
    )


__all__ = ["SqliteWebWatchRepository"]
