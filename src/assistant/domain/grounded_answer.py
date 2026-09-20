"""Source-grounded answers: evidence, citations and the answer they support (ADR-0019).

An answer here is never "the model said so". It is a set of segments, each of which cites one or
more *locally supplied* evidence identifiers, plus the evidence map that turns those identifiers
back into a logical URI and a source span. The model never sees a path, a page number or a line
range as something it may produce — it is handed opaque request-local ids, and local code
resolves them for display.

Nothing in this module is durable: an `EvidenceId` is a capability token that lives for exactly
one request, not an identity anything may store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from assistant.domain.catalog import CatalogEntry
from assistant.domain.errors import (
    GroundedAnswerInvalidCitation,
    InvalidGroundedAnswer,
)
from assistant.domain.knowledge import SourceSpan
from assistant.domain.storage import StorageUri, validate_root_id

EVIDENCE_ID_PATTERN = re.compile(r"^S[1-9][0-9]*$")

MAX_SEGMENT_CHARS = 2000
MAX_SEGMENTS = 12
MAX_CITATIONS_PER_SEGMENT = 8
MAX_INSUFFICIENT_REASON_CHARS = 500


@dataclass(frozen=True, slots=True)
class EvidenceId:
    """An opaque, request-local source identifier such as `S1`."""

    value: str

    def __post_init__(self) -> None:
        if not EVIDENCE_ID_PATTERN.match(self.value):
            raise InvalidGroundedAnswer(
                f"evidence id must match {EVIDENCE_ID_PATTERN.pattern}, not {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value

    @classmethod
    def numbered(cls, position: int) -> EvidenceId:
        """The id for the `position`-th piece of evidence (1-based)."""
        if position < 1:
            raise InvalidGroundedAnswer("evidence numbering starts at 1")
        return cls(f"S{position}")


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    """One bounded excerpt of indexed text, with everything needed to cite it locally."""

    id: EvidenceId
    root_id: str
    entry_id: UUID
    chunk_id: UUID
    logical_uri: StorageUri
    source_span: SourceSpan
    content: str
    content_truncated: bool = False

    def __post_init__(self) -> None:
        try:
            validate_root_id(self.root_id)
        except Exception as exc:
            raise InvalidGroundedAnswer(f"evidence root id is invalid: {exc}") from exc
        if self.logical_uri.root_id != self.root_id:
            raise InvalidGroundedAnswer(
                "evidence logical URI and root id must describe the same root"
            )
        if not self.content.strip():
            raise InvalidGroundedAnswer("evidence content must not be blank")

    @property
    def location(self) -> str:
        """Human-readable source location, resolved locally: `lines 18-31` or `page 4`."""
        return self.source_span.describe()


class GroundedAnswerStatus(StrEnum):
    """The only two outcomes a grounded answer may have."""

    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class GroundedAnswerSegment:
    """One part of an answer, always attached to at least one piece of evidence."""

    text: str
    source_ids: tuple[EvidenceId, ...]

    def __post_init__(self) -> None:
        stripped = self.text.strip()
        if not stripped:
            raise InvalidGroundedAnswer("an answer segment must not be blank")
        if len(stripped) > MAX_SEGMENT_CHARS:
            raise InvalidGroundedAnswer(
                f"an answer segment must be at most {MAX_SEGMENT_CHARS} characters"
            )
        if not self.source_ids:
            raise InvalidGroundedAnswer("an answer segment must cite at least one source")
        if len(self.source_ids) > MAX_CITATIONS_PER_SEGMENT:
            raise InvalidGroundedAnswer(
                f"an answer segment must cite at most {MAX_CITATIONS_PER_SEGMENT} sources"
            )
        if len(set(self.source_ids)) != len(self.source_ids):
            raise InvalidGroundedAnswer("an answer segment must not repeat a source id")
        object.__setattr__(self, "text", stripped)


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """What the model produced, after local validation of every citation."""

    status: GroundedAnswerStatus
    segments: tuple[GroundedAnswerSegment, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is GroundedAnswerStatus.ANSWERED:
            if not self.segments:
                raise InvalidGroundedAnswer("an answered result needs at least one segment")
            if len(self.segments) > MAX_SEGMENTS:
                raise InvalidGroundedAnswer(
                    f"an answered result must have at most {MAX_SEGMENTS} segments"
                )
            if self.reason is not None:
                raise InvalidGroundedAnswer("an answered result must not carry a reason")
        else:
            if self.segments:
                raise InvalidGroundedAnswer(
                    "an insufficient-evidence result must not carry segments"
                )
            if self.reason is None or not self.reason.strip():
                raise InvalidGroundedAnswer(
                    "an insufficient-evidence result needs a reason"
                )
            if len(self.reason.strip()) > MAX_INSUFFICIENT_REASON_CHARS:
                raise InvalidGroundedAnswer(
                    "an insufficient-evidence reason must be at most "
                    f"{MAX_INSUFFICIENT_REASON_CHARS} characters"
                )
            object.__setattr__(self, "reason", self.reason.strip())

    @property
    def cited_source_ids(self) -> tuple[EvidenceId, ...]:
        """Every cited source, in first-appearance order, without duplicates."""
        seen: dict[EvidenceId, None] = {}
        for segment in self.segments:
            for source_id in segment.source_ids:
                seen.setdefault(source_id, None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class GroundedAnswerResult:
    """The answer plus the local evidence it was allowed to use.

    `metadata_matches` and `offline_roots` exist so the CLI can hint at what could not be used;
    they are not evidence and never reach the model.
    """

    answer: GroundedAnswer
    evidence: tuple[KnowledgeEvidence, ...]
    metadata_matches: tuple[CatalogEntry, ...] = ()
    offline_roots: tuple[str, ...] = ()
    context_truncated: bool = False

    def evidence_by_id(self) -> dict[EvidenceId, KnowledgeEvidence]:
        """The local map the renderer resolves citations through."""
        return {item.id: item for item in self.evidence}

    def cited_evidence(self) -> tuple[KnowledgeEvidence, ...]:
        """The evidence actually cited, in first-citation order."""
        if self.answer.status is not GroundedAnswerStatus.ANSWERED:
            return ()
        by_id = self.evidence_by_id()
        return tuple(by_id[source_id] for source_id in self.answer.cited_source_ids)


def validate_citations(
    answer: GroundedAnswer, evidence_ids: frozenset[EvidenceId]
) -> None:
    """Reject any citation that is not exactly one of the supplied evidence ids.

    The model's ids are request-local capability tokens. An id it was not given is not a
    formatting problem to be repaired: it is an unsupported claim, and the answer is refused.

    Raises:
        GroundedAnswerInvalidCitation: an answer segment cites an unknown source.
    """
    for segment in answer.segments:
        for source_id in segment.source_ids:
            if source_id not in evidence_ids:
                raise GroundedAnswerInvalidCitation(
                    f"the model cited {source_id}, which was not among the supplied sources"
                )


__all__ = [
    "EVIDENCE_ID_PATTERN",
    "MAX_CITATIONS_PER_SEGMENT",
    "MAX_INSUFFICIENT_REASON_CHARS",
    "MAX_SEGMENTS",
    "MAX_SEGMENT_CHARS",
    "EvidenceId",
    "GroundedAnswer",
    "GroundedAnswerResult",
    "GroundedAnswerSegment",
    "GroundedAnswerStatus",
    "KnowledgeEvidence",
    "validate_citations",
]
