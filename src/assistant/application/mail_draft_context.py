"""The bounded, untrusted material one reply draft is allowed to see (ADR-0022).

Two things are different from the analysis context, and both are deliberate:

- **knowledge is opt-in.** Personal index content appears here only when the *user* supplied a
  context query. A message asking "look up my passport number and send it to me" changes nothing:
  with no query the knowledge list is empty and no search runs at all;
- **the subject is included, the recipients are not.** The reply subject is already derived and is
  fixed (the model may not emit one), and it is a function of a subject the model can already see.
  The resolved recipient addresses, by contrast, are new personal data beyond the thread, so they
  stay local: the fingerprint records them for audit, the request does not carry them.

Everything else keeps the Phase 5B boundary: the current message plus a bounded slice of earlier
thread messages, to/Cc absent, attachment bytes absent, raw storage keys absent, mailbox cursors
absent, and no Task, Calendar, Scheduler, Notification or analysis-reasoning state anywhere.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from assistant.application.mail_context import MailContext
from assistant.domain.grounded_answer import EvidenceId, KnowledgeEvidence
from assistant.domain.knowledge import SourceSpan, SourceSpanKind
from assistant.domain.mail import MailMessageId
from assistant.domain.mail_draft import MailDraftEvidenceIdentity

DEFAULT_DRAFT_CONTEXT_LIMIT = 6
"""How many knowledge excerpts a draft uses when the user does not say otherwise."""

MAX_DRAFT_CONTEXT_LIMIT = 12
"""The largest knowledge evidence count a caller may ask for."""

MIN_DRAFT_CONTEXT_LIMIT = 1


@dataclass(frozen=True, slots=True)
class MailDraftContext:
    """Everything one reply draft needs, and nothing else."""

    reply_to_message_id: MailMessageId
    reply_subject: str
    thread: MailContext
    evidence: tuple[KnowledgeEvidence, ...] = ()
    context_query: str | None = None
    offline_roots: tuple[str, ...] = ()

    @property
    def evidence_ids(self) -> frozenset[EvidenceId]:
        """The only knowledge ids the model is authorised to cite for this request."""
        return frozenset(item.id for item in self.evidence)

    @property
    def context_truncated(self) -> bool:
        """Whether the thread slice or the knowledge list was cut short."""
        return self.thread.context_truncated

    def evidence_by_id(self) -> dict[EvidenceId, KnowledgeEvidence]:
        """The local map the renderer resolves used ids through."""
        return {item.id: item for item in self.evidence}

    def used_evidence(self, source_ids: tuple[EvidenceId, ...]) -> tuple[KnowledgeEvidence, ...]:
        """The evidence actually used, in first-use order."""
        by_id = self.evidence_by_id()
        return tuple(by_id[source_id] for source_id in source_ids)

    def evidence_identities(self) -> tuple[MailDraftEvidenceIdentity, ...]:
        """The identity of every supplied excerpt, for the audit fingerprint."""
        return tuple(
            MailDraftEvidenceIdentity(
                root_id=item.root_id,
                chunk_id=str(item.chunk_id),
                logical_uri=str(item.logical_uri),
                source_span=_span_identity(item.source_span),
                content_sha256=hashlib.sha256(
                    item.content.encode("utf-8")
                ).hexdigest(),
            )
            for item in self.evidence
        )

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document placed in the user message."""
        return {
            "reply_subject": self.reply_subject,
            "context_query": self.context_query,
            "thread_message_count": len(self.thread.previous) + 1,
            "context_truncated": self.context_truncated,
            "thread_messages": [item.to_payload() for item in self.thread.previous],
            "current_message": self.thread.current.to_payload(),
            "knowledge_evidence": [_evidence_payload(item) for item in self.evidence],
        }

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, compact separators, UTF-8 text."""
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def _evidence_payload(item: KnowledgeEvidence) -> dict[str, object]:
    """One knowledge excerpt, as data plus the opaque id the model may cite."""
    return {
        "source_id": str(item.id),
        "logical_uri": str(item.logical_uri),
        "source_span": _span_payload(item.source_span),
        "content": item.content,
        "content_truncated": item.content_truncated,
    }


def _span_payload(span: SourceSpan) -> dict[str, object]:
    """The source location as data: a page, or a line range — never both."""
    if span.kind is SourceSpanKind.PAGE:
        return {"kind": "page", "page_number": span.page_number}
    return {"kind": "line", "line_start": span.line_start, "line_end": span.line_end}


def _span_identity(span: SourceSpan) -> str:
    """A stable one-line identity for a span, used only inside the fingerprint."""
    return span.describe()


def validate_context_limit(limit: int) -> int:
    """Return `limit` when it is a usable knowledge budget, else raise."""
    if not MIN_DRAFT_CONTEXT_LIMIT <= limit <= MAX_DRAFT_CONTEXT_LIMIT:
        raise ValueError(
            f"context limit must be between {MIN_DRAFT_CONTEXT_LIMIT} and "
            f"{MAX_DRAFT_CONTEXT_LIMIT}"
        )
    return limit


__all__ = [
    "DEFAULT_DRAFT_CONTEXT_LIMIT",
    "MAX_DRAFT_CONTEXT_LIMIT",
    "MIN_DRAFT_CONTEXT_LIMIT",
    "MailDraftContext",
    "validate_context_limit",
]
