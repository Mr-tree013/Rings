"""Turning schema-valid model output into a durable analysis, or refusing it (ADR-0021).

The schema the provider was given already fixes the *shape*: four categories, four temporal
kinds, bounded strings, no extra fields. What is left for local code is the part a JSON Schema
cannot express here:

- an instant must be parseable and must carry an explicit UTC offset. A wall-clock value without
  an offset is not "probably local time" — it is unusable, and the analysis is refused rather
  than stored with an invented timezone;
- a candidate that claims to be a deadline or an event start must carry either the raw text or
  the instant it was read from, so "there is a deadline" is never stored without the evidence;
- an instant without a temporal kind is refused too: it would silently turn a time into a fact
  with no meaning attached;
- a deadline stays a deadline. Nothing here ever rewrites one kind into the other.

Everything in this module is pure. It touches no store, no provider and no clock, which is what
makes the mapping table testable on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assistant.domain.errors import InvalidMailAnalysis
from assistant.domain.mail import MailMessageId
from assistant.domain.mail_analysis import (
    ANALYZER_VERSION,
    MailActionCandidate,
    MailAnalysis,
    MailCategory,
    MailTemporalKind,
)

UNAVAILABLE_BODY_SUMMARY = (
    "No analysis: the message body is not stored locally because it exceeded the configured "
    "size limit."
)
"""The deterministic summary used when a message is too large to analyze."""


def parse_mail_analysis_output(
    payload: dict[str, Any],
    *,
    message_id: MailMessageId,
    input_fingerprint: str,
    now: datetime,
    analyzer_version: int = ANALYZER_VERSION,
) -> MailAnalysis:
    """Turn one schema-valid analysis object into a validated `MailAnalysis`.

    Raises:
        InvalidMailAnalysis: the object is schema-valid but semantically unusable.
    """
    category = _category(payload.get("category"))
    requires_reply = payload.get("requires_reply")
    if not isinstance(requires_reply, bool):
        raise InvalidMailAnalysis("requires_reply must be a JSON boolean")
    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise InvalidMailAnalysis("an analysis needs a summary string")
    raw_candidates = payload.get("action_candidates")
    if not isinstance(raw_candidates, list):
        raise InvalidMailAnalysis("action_candidates must be a JSON array")
    candidates = tuple(_candidate(raw) for raw in raw_candidates)
    return MailAnalysis(
        message_id=message_id,
        analyzer_version=analyzer_version,
        input_fingerprint=input_fingerprint,
        category=category,
        requires_reply=requires_reply,
        summary=summary,
        action_candidates=candidates,
        created_at=now,
        updated_at=now,
    )


def unavailable_body_analysis(
    *,
    message_id: MailMessageId,
    input_fingerprint: str,
    now: datetime,
    analyzer_version: int = ANALYZER_VERSION,
) -> MailAnalysis:
    """The durable record for a message that will not be analyzed.

    An oversize message is never sent to a provider, so the honest outcome is a stored
    `unknown` analysis that says why, computed without a model and therefore free of cost.
    """
    return MailAnalysis(
        message_id=message_id,
        analyzer_version=analyzer_version,
        input_fingerprint=input_fingerprint,
        category=MailCategory.UNKNOWN,
        requires_reply=False,
        summary=UNAVAILABLE_BODY_SUMMARY,
        created_at=now,
        updated_at=now,
    )


def _category(raw: object) -> MailCategory:
    if not isinstance(raw, str):
        raise InvalidMailAnalysis("an analysis needs a category")
    try:
        return MailCategory(raw)
    except ValueError as exc:
        raise InvalidMailAnalysis(f"unknown mail category {raw!r}") from exc


def _candidate(raw: object) -> MailActionCandidate:
    if not isinstance(raw, dict):
        raise InvalidMailAnalysis("an action candidate must be a JSON object")
    text = raw.get("text")
    if not isinstance(text, str):
        raise InvalidMailAnalysis("an action candidate needs text")
    kind = raw.get("temporal_kind")
    if not isinstance(kind, str):
        raise InvalidMailAnalysis("an action candidate needs a temporal kind")
    try:
        temporal_kind = MailTemporalKind(kind)
    except ValueError as exc:
        raise InvalidMailAnalysis(f"unknown temporal kind {kind!r}") from exc
    time_text = raw.get("time_text")
    if time_text is not None and not isinstance(time_text, str):
        raise InvalidMailAnalysis("time_text must be a string or null")
    return MailActionCandidate(
        text=text,
        temporal_kind=temporal_kind,
        time_text=time_text,
        interpreted_at=_instant(raw.get("interpreted_at")),
    )


def _instant(raw: object) -> datetime | None:
    """Parse a model-supplied instant, refusing anything without an explicit offset."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidMailAnalysis("interpreted_at must be an ISO 8601 string or null")
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidMailAnalysis(f"interpreted_at is not ISO 8601: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidMailAnalysis(
            f"interpreted_at must carry an explicit UTC offset: {text!r}"
        )
    return parsed.astimezone(UTC)


__all__ = [
    "UNAVAILABLE_BODY_SUMMARY",
    "parse_mail_analysis_output",
    "unavailable_body_analysis",
]
