"""SQLite implementation of the ObservationAnalysisRepository port (ADR-0009, ADR-0029).

One row per inbound event — `inbound_event_id` is unique — so a retried event finds the analysis it
already has. The row stores a category, a bounded summary and a JSON array of candidate sentences;
it stores no page text, no manual input text and no model reasoning.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from uuid import UUID, uuid4

from assistant.domain.observation_analysis import (
    ObservationActionCandidate,
    ObservationAnalysis,
    ObservationCategory,
)
from assistant.store.db import Database, transaction
from assistant.store.errors import CommitmentStoreError
from assistant.store.serialization import from_utc_iso, to_utc_iso

_ANALYSIS_FIELDS = (
    "id, inbound_event_id, source_kind, analyzer_version, input_fingerprint, category, "
    "summary, action_candidates_json, created_at, updated_at"
)


class SqliteObservationAnalysisRepository:
    """Durable analyses of web changes and manual input."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_analysis(self, event_id: object) -> ObservationAnalysis | None:
        return await asyncio.to_thread(self._get_analysis_sync, event_id)

    async def persist_analysis(
        self, analysis: ObservationAnalysis
    ) -> ObservationAnalysis:
        return await asyncio.to_thread(self._persist_analysis_sync, analysis)

    # ------------------------------------------------------------ blocking internals

    def _get_analysis_sync(self, event_id: object) -> ObservationAnalysis | None:
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT {_ANALYSIS_FIELDS} FROM observation_analyses "
                "WHERE inbound_event_id = ?",
                (str(event_id),),
            ).fetchone()
        return None if row is None else _row_to_analysis(row)

    def _persist_analysis_sync(
        self, analysis: ObservationAnalysis
    ) -> ObservationAnalysis:
        analysis_id = str(analysis.id) if analysis.id is not None else str(uuid4())
        try:
            with self._database.connect() as connection, transaction(connection):
                connection.execute(
                    f"INSERT INTO observation_analyses ({_ANALYSIS_FIELDS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (inbound_event_id) DO UPDATE SET "
                    "analyzer_version = excluded.analyzer_version, "
                    "input_fingerprint = excluded.input_fingerprint, "
                    "category = excluded.category, summary = excluded.summary, "
                    "action_candidates_json = excluded.action_candidates_json, "
                    "updated_at = excluded.updated_at",
                    (
                        analysis_id,
                        str(analysis.event_id),
                        analysis.source_kind,
                        analysis.analyzer_version,
                        analysis.input_fingerprint,
                        analysis.category.value,
                        analysis.summary,
                        analysis.candidates_payload(),
                        to_utc_iso(analysis.created_at),
                        to_utc_iso(analysis.updated_at or analysis.created_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(
                f"could not store the observation analysis: {exc}"
            ) from exc
        stored = self._get_analysis_sync(analysis.event_id)
        return analysis if stored is None else stored


def _row_to_analysis(row: sqlite3.Row) -> ObservationAnalysis:
    candidates = json.loads(str(row["action_candidates_json"]))
    updated = row["updated_at"]
    return ObservationAnalysis(
        id=UUID(str(row["id"])),
        event_id=UUID(str(row["inbound_event_id"])),
        source_kind=str(row["source_kind"]),
        analyzer_version=int(row["analyzer_version"]),
        input_fingerprint=str(row["input_fingerprint"]),
        category=ObservationCategory(str(row["category"])),
        summary=str(row["summary"]),
        action_candidates=tuple(
            ObservationActionCandidate.from_payload(item) for item in candidates
        ),
        created_at=from_utc_iso(str(row["created_at"])),
        updated_at=None if updated is None else from_utc_iso(str(updated)),
    )


__all__ = ["SqliteObservationAnalysisRepository"]
