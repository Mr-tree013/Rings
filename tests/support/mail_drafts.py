"""Helpers for the reply-draft tests: real stores, a scripted provider, a knowledge spy.

The knowledge spy is the point of this module: whether the personal index is read at all is a
privacy property, and the only honest way to test it is to count the calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.grounded_context import GroundedContext
from assistant.application.mail_context import MailContextBuilder
from assistant.application.mail_drafts import MailDraftService
from assistant.application.structured_model import StructuredModel
from assistant.domain.grounded_answer import EvidenceId, KnowledgeEvidence
from assistant.domain.knowledge import SourceSpan
from assistant.domain.mail_draft import (
    PROMPT_VERSION,
    MailDraft,
    MailDraftOrigin,
)
from assistant.domain.storage import StorageUri
from assistant.store.db import Database
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


class KnowledgeSpy:
    """A `GroundedContextBuilder` stand-in that records every question it is asked."""

    def __init__(self, evidence: tuple[KnowledgeEvidence, ...] = ()) -> None:
        self.evidence = evidence
        self.queries: list[tuple[str, str | None, int]] = []

    async def build(
        self, question: str, *, root_id: str | None = None, limit: int = 8
    ) -> GroundedContext:
        self.queries.append((question, root_id, limit))
        return GroundedContext(question=question, evidence=self.evidence[:limit])

    @property
    def called(self) -> bool:
        """Whether any search ran at all."""
        return bool(self.queries)


def make_evidence(
    *,
    position: int = 1,
    root_id: str = "university",
    relative_path: str = "notes/office-hours.md",
    content: str = "Office hours are Tuesday 14:00-16:00 in room B203.",
    page: int | None = None,
    lines: tuple[int, int] | None = None,
) -> KnowledgeEvidence:
    """One piece of knowledge evidence, shaped like the real retrieval result."""
    if page is not None:
        span = SourceSpan.page(page)
    else:
        start, end = lines if lines is not None else (18, 31)
        span = SourceSpan.lines(start, end)
    return KnowledgeEvidence(
        id=EvidenceId.numbered(position),
        root_id=root_id,
        entry_id=uuid4(),
        chunk_id=uuid4(),
        logical_uri=StorageUri.parse(f"vault://{root_id}/{relative_path}"),
        source_span=span,
        content=content,
    )


@dataclass
class DraftStores:
    """The stores a draft test needs, over a real database."""

    database: Database
    clock: FakeClock
    mail: SqliteMailRepository = field(init=False)
    intelligence: SqliteMailIntelligenceRepository = field(init=False)
    drafts: SqliteMailDraftRepository = field(init=False)

    def __post_init__(self) -> None:
        self.mail = SqliteMailRepository(self.database)
        self.intelligence = SqliteMailIntelligenceRepository(self.database)
        self.drafts = SqliteMailDraftRepository(self.database)

    def service(
        self,
        model: FakeModelAdapter | None = None,
        knowledge: KnowledgeSpy | None = None,
        *,
        planning_timezone: str | None = "Asia/Shanghai",
    ) -> MailDraftService:
        """The service under test, over the real stores and a scripted provider."""
        return MailDraftService(
            self.mail,
            self.intelligence,
            self.drafts,
            MailContextBuilder(
                self.mail, self.intelligence, planning_timezone=planning_timezone
            ),
            knowledge if knowledge is not None else KnowledgeSpy(),  # type: ignore[arg-type]
            None if model is None else StructuredModel(model),
            self.clock,
        )


def build_draft(
    *,
    reply_to_message_id: UUID | None = None,
    to_addresses: tuple[str, ...] = ("ada@example.edu",),
    subject: str = "Re: SE lab deadline",
    body_text: str = "Thanks — I will submit it on Friday.",
    needs_user_input: tuple[str, ...] = (),
    origin: MailDraftOrigin = MailDraftOrigin.MODEL_GENERATED,
    version: int = 1,
    fingerprint: str = "a" * 64,
    at: datetime = NOW,
    thread_id: UUID | None = None,
    account_id: str = "smail",
) -> MailDraft:
    """Build one draft with sensible, deterministic defaults."""
    return MailDraft(
        id=uuid4(),
        account_id=account_id,
        thread_id=thread_id,
        reply_to_message_id=reply_to_message_id or uuid4(),
        to_addresses=to_addresses,
        subject=subject,
        body_text=body_text,
        needs_user_input=needs_user_input,
        origin=origin,
        version=version,
        prompt_version=PROMPT_VERSION,
        generation_input_fingerprint=fingerprint,
        created_at=at,
        updated_at=at,
    )


def draft_response(
    *,
    body: str = "Thanks for the note — I will submit the report on Friday.",
    used_source_ids: list[str] | None = None,
    needs_user_input: list[str] | None = None,
) -> str:
    """A model answer that satisfies the draft schema."""
    return json.dumps(
        {
            "body": body,
            "used_source_ids": used_source_ids or [],
            "needs_user_input": needs_user_input or [],
        }
    )


__all__ = [
    "NOW",
    "DraftStores",
    "KnowledgeSpy",
    "build_draft",
    "draft_response",
    "make_evidence",
]
