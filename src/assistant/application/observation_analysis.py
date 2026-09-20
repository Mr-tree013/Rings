"""Turning schema-valid model output into a durable analysis, or refusing it (ADR-0029).

The schema already fixes the shape. What is left for local code is what a JSON Schema cannot say:

- a candidate that claims a deadline or an event start must carry either the text it was read from
  or the instant, so "there is a deadline" is never stored without evidence;
- an instant must be parseable and must carry an explicit offset, because a wall-clock value is not
  "probably local time" — it is unusable;
- an instant with no temporal kind is refused, and a kind with no evidence is refused;
- a deadline stays a deadline. Nothing here rewrites one kind into the other.

Everything in this module is pure: no store, no provider, no clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assistant.domain.errors import InvalidObservationAnalysis
from assistant.domain.observation_analysis import (
    ANALYZER_VERSION,
    ObservationActionCandidate,
    ObservationAnalysis,
    ObservationCategory,
    ObservationTemporalKind,
)

TIMED_KINDS = (
    ObservationTemporalKind.DEADLINE,
    ObservationTemporalKind.EVENT_START,
    ObservationTemporalKind.OTHER,
)


def parse_observation_analysis_output(
    payload: dict[str, Any],
    *,
    event_id: object,
    source_kind: str,
    input_fingerprint: str,
    now: datetime,
    analyzer_version: int = ANALYZER_VERSION,
) -> ObservationAnalysis:
    """Turn one schema-valid analysis object into a validated `ObservationAnalysis`.

    Raises:
        InvalidObservationAnalysis: the object is schema-valid but semantically unusable.
    """
    category = payload.get("category")
    if not isinstance(category, str):
        raise InvalidObservationAnalysis("an analysis needs a category")
    try:
        parsed_category = ObservationCategory(category)
    except ValueError as exc:
        raise InvalidObservationAnalysis(f"unknown category {category!r}") from exc
    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise InvalidObservationAnalysis("an analysis needs a summary string")
    raw_candidates = payload.get("action_candidates")
    if not isinstance(raw_candidates, list):
        raise InvalidObservationAnalysis("action_candidates must be a JSON array")
    return ObservationAnalysis(
        event_id=event_id,
        source_kind=source_kind,
        analyzer_version=analyzer_version,
        input_fingerprint=input_fingerprint,
        category=parsed_category,
        summary=summary,
        action_candidates=tuple(_candidate(raw) for raw in raw_candidates),
        created_at=now,
        updated_at=now,
    )


def _candidate(raw: object) -> ObservationActionCandidate:
    if not isinstance(raw, dict):
        raise InvalidObservationAnalysis("an action candidate must be a JSON object")
    text = raw.get("text")
    if not isinstance(text, str):
        raise InvalidObservationAnalysis("an action candidate needs text")
    kind = raw.get("temporal_kind")
    if not isinstance(kind, str):
        raise InvalidObservationAnalysis("an action candidate needs a temporal kind")
    try:
        temporal_kind = ObservationTemporalKind(kind)
    except ValueError as exc:
        raise InvalidObservationAnalysis(f"unknown temporal kind {kind!r}") from exc
    time_text = raw.get("time_text")
    if time_text is not None and not isinstance(time_text, str):
        raise InvalidObservationAnalysis("time_text must be a string or null")
    instant = _instant(raw.get("interpreted_at"))
    stripped_time = None if time_text is None else " ".join(time_text.split())
    if temporal_kind in TIMED_KINDS and not stripped_time and instant is None:
        raise InvalidObservationAnalysis(
            f"a {temporal_kind.value} candidate needs the text or the instant it was read from"
        )
    return ObservationActionCandidate(
        text=text,
        temporal_kind=temporal_kind,
        time_text=stripped_time or None,
        interpreted_at=instant,
    )


def _instant(raw: object) -> datetime | None:
    """Parse a model-supplied instant, refusing anything without an explicit offset."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidObservationAnalysis("interpreted_at must be an ISO 8601 string or null")
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidObservationAnalysis(f"interpreted_at is not ISO 8601: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidObservationAnalysis(
            f"interpreted_at must carry an explicit UTC offset: {text!r}"
        )
    return parsed.astimezone(UTC)


__all__ = ["TIMED_KINDS", "parse_observation_analysis_output"]
