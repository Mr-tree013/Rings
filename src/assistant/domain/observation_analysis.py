"""What an analysis of a web change or a manual input is allowed to say (ADR-0029).

The output shape *is* the safety property. An analysis may classify, summarise and name possible
action, deadline or event candidates — and there is nowhere in this module to put a command, a
task, a case, an action request, an approval, a URL, a tool call or a shell command. A model that
wanted to escalate does not have a field to escalate into.

`DEADLINE` and `EVENT_START` stay separate here for the same reason they are separate in mail
analysis: "this is due by Friday" and "this happens on Friday" are different facts, and merging
them would make a due date look like a calendar entry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from assistant.domain.errors import InvalidObservationAnalysis

ANALYZER_VERSION = 1
"""Bump when the prompt, the schema or this parsing changes meaning."""

MAX_SUMMARY_CHARS = 800
MAX_ACTION_CANDIDATES = 10
MAX_CANDIDATE_TEXT_CHARS = 300
MAX_TIME_TEXT_CHARS = 120


class ObservationCategory(StrEnum):
    """What kind of thing the content turned out to be."""

    INFORMATIONAL = "informational"
    ACTIONABLE = "actionable"
    IGNORE = "ignore"
    UNKNOWN = "unknown"


class ObservationTemporalKind(StrEnum):
    """Whether a candidate carries a deadline, an event start, neither or something else."""

    NONE = "none"
    DEADLINE = "deadline"
    EVENT_START = "event-start"
    OTHER = "other"


def _optional_text(value: object, limit: int, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidObservationAnalysis(f"{field_name} must be a string or null")
    stripped = " ".join(value.split())
    if not stripped:
        return None
    if len(stripped) > limit:
        raise InvalidObservationAnalysis(
            f"{field_name} must be at most {limit} characters"
        )
    return stripped


@dataclass(frozen=True, slots=True)
class ObservationActionCandidate:
    """One possible thing to do, as a sentence — never as a command."""

    text: str
    temporal_kind: ObservationTemporalKind = ObservationTemporalKind.NONE
    time_text: str | None = None
    interpreted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidObservationAnalysis("a candidate needs text")
        stripped = " ".join(self.text.split())
        if not stripped:
            raise InvalidObservationAnalysis("a candidate's text must not be blank")
        if len(stripped) > MAX_CANDIDATE_TEXT_CHARS:
            raise InvalidObservationAnalysis(
                f"a candidate's text must be at most {MAX_CANDIDATE_TEXT_CHARS} characters"
            )
        object.__setattr__(self, "text", stripped)
        if not isinstance(self.temporal_kind, ObservationTemporalKind):
            try:
                object.__setattr__(
                    self, "temporal_kind", ObservationTemporalKind(self.temporal_kind)
                )
            except ValueError as exc:
                raise InvalidObservationAnalysis(
                    f"unknown temporal kind {self.temporal_kind!r}"
                ) from exc
        object.__setattr__(
            self, "time_text", _optional_text(self.time_text, MAX_TIME_TEXT_CHARS, "time_text")
        )
        if self.interpreted_at is not None:
            if (
                self.interpreted_at.tzinfo is None
                or self.interpreted_at.utcoffset() is None
            ):
                raise InvalidObservationAnalysis("interpreted_at must be timezone-aware")
            if self.temporal_kind is ObservationTemporalKind.NONE:
                raise InvalidObservationAnalysis(
                    "only a dated candidate may carry an interpreted time"
                )
        if (
            self.temporal_kind is ObservationTemporalKind.NONE
            and self.time_text is not None
        ):
            raise InvalidObservationAnalysis(
                "a candidate with no temporal kind must not carry time text"
            )
        if (
            self.temporal_kind is not ObservationTemporalKind.NONE
            and self.time_text is None
            and self.interpreted_at is None
        ):
            raise InvalidObservationAnalysis(
                f"a {self.temporal_kind.value} candidate needs the text or the instant it was "
                "read from"
            )

    def to_payload(self) -> dict[str, object]:
        """The stored form: a sentence plus how its time was understood."""
        return {
            "text": self.text,
            "temporal_kind": self.temporal_kind.value,
            "time_text": self.time_text,
            "interpreted_at": None
            if self.interpreted_at is None
            else self.interpreted_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> ObservationActionCandidate:
        """Rebuild one candidate from stored JSON, refusing anything unexpected."""
        if not isinstance(payload, dict):
            raise InvalidObservationAnalysis("a stored candidate must be a JSON object")
        expected = {"text", "temporal_kind", "time_text", "interpreted_at"}
        if set(payload) != expected:
            raise InvalidObservationAnalysis("a stored candidate has unexpected fields")
        interpreted = payload["interpreted_at"]
        if interpreted is not None and not isinstance(interpreted, str):
            raise InvalidObservationAnalysis("interpreted_at must be a string or null")
        try:
            moment = None if interpreted is None else datetime.fromisoformat(interpreted)
        except ValueError as exc:
            raise InvalidObservationAnalysis("interpreted_at is not ISO 8601") from exc
        kind = payload["temporal_kind"]
        if not isinstance(kind, str):
            raise InvalidObservationAnalysis("temporal_kind must be a string")
        try:
            parsed_kind = ObservationTemporalKind(kind)
        except ValueError as exc:
            raise InvalidObservationAnalysis(f"unknown temporal kind {kind!r}") from exc
        return cls(
            text=payload["text"],
            temporal_kind=parsed_kind,
            time_text=payload["time_text"],
            interpreted_at=moment,
        )


@dataclass(frozen=True, slots=True)
class ObservationAnalysis:
    """The durable result of analyzing one web change or one manual input."""

    event_id: object
    source_kind: str
    analyzer_version: int
    input_fingerprint: str
    category: ObservationCategory
    summary: str
    created_at: datetime
    action_candidates: tuple[ObservationActionCandidate, ...] = ()
    updated_at: datetime | None = None
    id: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, ObservationCategory):
            try:
                object.__setattr__(self, "category", ObservationCategory(self.category))
            except ValueError as exc:
                raise InvalidObservationAnalysis(
                    f"unknown observation category {self.category!r}"
                ) from exc
        if self.analyzer_version < 1:
            raise InvalidObservationAnalysis("analyzer_version must be positive")
        if len(self.input_fingerprint) != 64:
            raise InvalidObservationAnalysis("input_fingerprint must be a SHA-256 hex digest")
        summary = " ".join(self.summary.split())
        if not summary:
            raise InvalidObservationAnalysis("an analysis needs a summary")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise InvalidObservationAnalysis(
                f"a summary must be at most {MAX_SUMMARY_CHARS} characters"
            )
        object.__setattr__(self, "summary", summary)
        if len(self.action_candidates) > MAX_ACTION_CANDIDATES:
            raise InvalidObservationAnalysis(
                f"an analysis may carry at most {MAX_ACTION_CANDIDATES} candidates"
            )
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidObservationAnalysis(f"{name} must be timezone-aware")

    def candidates_payload(self) -> str:
        """The canonical JSON array stored in the analysis row."""
        return json.dumps(
            [item.to_payload() for item in self.action_candidates],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def observation_analysis_input_fingerprint(
    *,
    analyzer_version: int,
    schema_version: int,
    event_type: str,
    event_external_id: str | None,
    source_identities: tuple[str, ...],
    context_fingerprint: str,
) -> str:
    """The canonical fingerprint that makes an analysis reusable across retries.

    It covers the versions, the event identity, the identity of every source document and the exact
    bounded context that was sent — so a retry of the same event reuses the stored analysis, while
    any change to the content, the context or the analyzer produces a new one.
    """
    document = {
        "analyzer_version": analyzer_version,
        "schema_version": schema_version,
        "event_type": event_type,
        "event_external_id": event_external_id,
        "source_identities": sorted(source_identities),
        "context_fingerprint": context_fingerprint,
    }
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ANALYZER_VERSION",
    "MAX_ACTION_CANDIDATES",
    "MAX_CANDIDATE_TEXT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TIME_TEXT_CHARS",
    "ObservationActionCandidate",
    "ObservationAnalysis",
    "ObservationCategory",
    "ObservationTemporalKind",
    "observation_analysis_input_fingerprint",
]
