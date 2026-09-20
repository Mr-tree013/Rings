"""Bounded evidence context for a grounded answer (ADR-0019).

Retrieval is deterministic and local: the search query is the user's question, the index is
Phase 2's full-text index, and the only decisions made here are structural — how many chunks,
how many characters, in what order, and which ids the model is allowed to cite.

Four boundaries matter:

- **only content hits are evidence.** Metadata-only matches are returned for the UI to hint at;
  a file name is not a statement about a file's contents, and an offline vault's name even less
  so.
- **the text comes from the index, not the file.** Re-reading the original would fork the
  representation the search ranked, and would fail differently for a detached vault, a reparsed
  PDF or a file that changed since indexing.
- **the budget is explicit.** Per-chunk and total character limits are enforced here, with a
  deterministic truncation flag, instead of hoping a provider window absorbs the difference.
- **ids are assigned after deduplication**, in retrieval-rank order, so the same question over
  the same index always produces the same `S1..Sn` and the same request bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.domain.catalog import CatalogEntry
from assistant.domain.grounded_answer import EvidenceId, KnowledgeEvidence
from assistant.domain.knowledge import KnowledgeContextHit

MAX_EVIDENCE_CHARS_PER_CHUNK = 4000
"""How much of one indexed chunk may reach the model."""

MAX_EVIDENCE_CHARS_TOTAL = 24000
"""The whole evidence budget: a hard ceiling, not a target to be tested."""

MAX_EVIDENCE_LIMIT = 20
"""Largest evidence count a caller may ask for."""

DEFAULT_EVIDENCE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class GroundedContext:
    """Everything one grounded answer needs, and nothing else."""

    question: str
    evidence: tuple[KnowledgeEvidence, ...]
    metadata_matches: tuple[CatalogEntry, ...] = ()
    offline_roots: tuple[str, ...] = ()
    context_truncated: bool = False

    @property
    def evidence_ids(self) -> frozenset[EvidenceId]:
        """The only source ids the model is authorised to cite for this request."""
        return frozenset(item.id for item in self.evidence)

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document placed in the user message."""
        return {
            "question": self.question,
            "evidence": [
                {
                    "source_id": str(item.id),
                    "logical_uri": str(item.logical_uri),
                    "source_span": _span_payload(item),
                    "content": item.content,
                    "content_truncated": item.content_truncated,
                }
                for item in self.evidence
            ],
        }


class GroundedContextBuilder:
    """Turns a question into bounded, citable evidence. It can only read."""

    def __init__(self, search: KnowledgeSearchService) -> None:
        self._search = search

    async def build(
        self,
        question: str,
        *,
        root_id: str | None = None,
        limit: int = DEFAULT_EVIDENCE_LIMIT,
    ) -> GroundedContext:
        """Search, then bound and number the evidence.

        Raises:
            ValueError: the question is blank, or the limit is outside 1..20.
        """
        if not question.strip():
            raise ValueError("question must not be blank")
        if not 1 <= limit <= MAX_EVIDENCE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_EVIDENCE_LIMIT}")
        result = await self._search.search_context(
            question.strip(), root_id=root_id, limit=limit
        )
        evidence, truncated = _select_evidence(result.content_hits)
        return GroundedContext(
            question=question.strip(),
            evidence=evidence,
            metadata_matches=result.metadata_hits,
            offline_roots=result.offline_roots,
            context_truncated=truncated,
        )


def _select_evidence(
    hits: tuple[KnowledgeContextHit, ...],
) -> tuple[tuple[KnowledgeEvidence, ...], bool]:
    """Deduplicate by `(root_id, chunk_id)`, number in rank order, and apply the budget."""
    seen: set[tuple[str, object]] = set()
    evidence: list[KnowledgeEvidence] = []
    total_chars = 0
    context_truncated = False
    for hit in hits:
        key = (hit.root_id, hit.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        content, truncated = _bounded_content(hit.content)
        if total_chars + len(content) > MAX_EVIDENCE_CHARS_TOTAL:
            context_truncated = True
            break
        total_chars += len(content)
        evidence.append(
            KnowledgeEvidence(
                id=EvidenceId.numbered(len(evidence) + 1),
                root_id=hit.root_id,
                entry_id=hit.entry_id,
                chunk_id=hit.chunk_id,
                logical_uri=hit.logical_uri,
                source_span=hit.source_span,
                content=content,
                content_truncated=truncated,
            )
        )
        context_truncated = context_truncated or truncated
    return tuple(evidence), context_truncated


def _bounded_content(content: str) -> tuple[str, bool]:
    """First `MAX_EVIDENCE_CHARS_PER_CHUNK` Unicode characters, or the whole thing."""
    if len(content) <= MAX_EVIDENCE_CHARS_PER_CHUNK:
        return content, False
    return content[:MAX_EVIDENCE_CHARS_PER_CHUNK], True


def _span_payload(item: KnowledgeEvidence) -> dict[str, object]:
    """The source location as data: a page, or a line range — never both."""
    span = item.source_span
    if span.page_number is not None:
        return {"kind": "page", "page_number": span.page_number}
    return {"kind": "line", "line_start": span.line_start, "line_end": span.line_end}


__all__ = [
    "DEFAULT_EVIDENCE_LIMIT",
    "MAX_EVIDENCE_CHARS_PER_CHUNK",
    "MAX_EVIDENCE_CHARS_TOTAL",
    "MAX_EVIDENCE_LIMIT",
    "GroundedContext",
    "GroundedContextBuilder",
]
