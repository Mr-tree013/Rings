"""Durable reply drafts: explicit, bounded, local — and never sent (ADR-0022).

```text
pw mail draft create MESSAGE [--context-query Q]
        │
        ▼
resolve the message ── body readable? ── recipients (Reply-To, else From) ── subject (pure)
        │
        ▼
bounded untrusted thread context
        │
        ├── no --context-query ──► no knowledge search at all, zero evidence
        └── --context-query Q   ──► the existing bounded GroundedContext boundary
        ▼
StructuredModel (closed schema) ──► local validation ──► MailDraft + used-source provenance
```

Four properties are the point of this module:

- **the user triggers it, nothing else does.** No `EventWorker`, no `MailSyncService`, no
  scheduler and no daemon startup path can create a draft, so a stranger's email cannot make the
  assistant read the personal index;
- **knowledge is opt-in by the user.** The index is searched only when a context query was
  supplied. A message that begs for personal data changes nothing about that;
- **the model decides one thing: prose.** Recipients and subject are derived locally before the
  call, and the schema has nowhere to put either;
- **nothing is sent.** There is no SMTP client, no approval, no `ActionRequest` and no send
  method anywhere on this path — a draft is local content a user may review and edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from assistant.application.grounded_context import GroundedContext, GroundedContextBuilder
from assistant.application.mail_context import MailContext, MailContextBuilder
from assistant.application.mail_draft_context import (
    DEFAULT_DRAFT_CONTEXT_LIMIT,
    MailDraftContext,
    validate_context_limit,
)
from assistant.application.mail_draft_prompt import MAIL_REPLY_DRAFT_INSTRUCTIONS
from assistant.application.mail_draft_schema import (
    MAIL_REPLY_DRAFT_SCHEMA_V1,
    MAIL_REPLY_DRAFT_SCHEMA_VERSION,
)
from assistant.application.structured_model import StructuredModel
from assistant.domain.errors import (
    InvalidGroundedAnswer,
    InvalidMailDraft,
    MailDraftInvalidKnowledgeReference,
    MailDraftNotFound,
    MailDraftSourceUnavailable,
    MailMessageNotFound,
    ModelNotConfigured,
)
from assistant.domain.grounded_answer import EvidenceId, KnowledgeEvidence
from assistant.domain.mail import MailBodyStatus, MailMessage, MailMessageId
from assistant.domain.mail_draft import (
    PROMPT_VERSION,
    MailDraft,
    MailDraftId,
    MailDraftOrigin,
    MailDraftPlan,
    MailDraftSource,
    mail_draft_from_plan,
    mail_draft_input_fingerprint,
    reply_subject,
    resolve_reply_recipients,
)
from assistant.domain.model import (
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelRole,
)
from assistant.ports.clock import Clock
from assistant.ports.mail_draft_repository import MailDraftRepository
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository

LOGGER = logging.getLogger("assistant.mail")


@dataclass(frozen=True, slots=True)
class MailDraftListing:
    """One row of the draft list: the draft and how many sources it cites."""

    draft: MailDraft
    source_count: int


@dataclass(frozen=True, slots=True)
class MailDraftResult:
    """A created (or fetched) draft, with the local provenance a reviewer needs."""

    draft: MailDraft
    sources: tuple[MailDraftSource, ...] = ()
    offline_roots: tuple[str, ...] = ()
    context_truncated: bool = False


class MailDraftService:
    """Creates, reads and edits local reply drafts. It cannot send anything."""

    def __init__(
        self,
        mail: MailRepository,
        intelligence: MailIntelligenceRepository,
        drafts: MailDraftRepository,
        context_builder: MailContextBuilder,
        knowledge: GroundedContextBuilder,
        model: StructuredModel | None,
        clock: Clock,
        *,
        reasoning_effort: str = "low",
        max_output_tokens: int = 4096,
    ) -> None:
        self._mail = mail
        self._intelligence = intelligence
        self._drafts = drafts
        self._context_builder = context_builder
        self._knowledge = knowledge
        self._model = model
        self._clock = clock
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens

    @property
    def can_generate(self) -> bool:
        """Whether this instance holds a provider. Reading and editing never need one."""
        return self._model is not None

    async def create_reply_draft(
        self,
        message_id: MailMessageId,
        *,
        context_query: str | None = None,
        root_id: str | None = None,
        context_limit: int = DEFAULT_DRAFT_CONTEXT_LIMIT,
    ) -> MailDraftResult:
        """Draft a reply to one stored message, from bounded local material.

        Raises:
            MailMessageNotFound: the message is not stored.
            MailDraftSourceUnavailable: the message has no readable body.
            MailReplyRecipientUnavailable: neither `Reply-To` nor `From` is usable.
            MailDraftInvalidKnowledgeReference: the draft cited a source it was not given.
            ModelOutputNotJson / ModelOutputSchemaViolation: the answer was unusable.
        """
        query = _clean_query(context_query)
        if query is None and root_id is not None:
            raise ValueError("a knowledge root filter is meaningless without a context query")
        validate_context_limit(context_limit)
        if self._model is None:
            raise ModelNotConfigured(
                "reply drafting needs a configured model provider; reading and editing a "
                "stored draft does not"
            )
        message = await self._mail.get_message(message_id)
        if message is None:
            raise MailMessageNotFound(message_id)
        if message.body_status is not MailBodyStatus.AVAILABLE or not (
            message.body_text or ""
        ).strip():
            # A header-only (oversize) message, or one whose body could not be extracted, is not
            # a basis for guessing what to write back. No provider call is made.
            raise MailDraftSourceUnavailable(
                f"mail message {message.id} has no readable body to reply to"
            )
        recipients = resolve_reply_recipients(message)
        subject = reply_subject(message.subject)
        thread, thread_id = await self._thread_context(message)
        evidence, offline_roots = await self._knowledge_evidence(
            query, root_id=root_id, context_limit=context_limit
        )
        context = MailDraftContext(
            reply_to_message_id=message.id,
            reply_subject=subject,
            thread=thread,
            evidence=evidence,
            context_query=query,
            offline_roots=offline_roots,
        )
        plan = MailDraftPlan(
            account_id=message.account_id,
            reply_to_message_id=message.id,
            to_addresses=recipients,
            subject=subject,
            thread_id=thread_id,
            generation_input_fingerprint=mail_draft_input_fingerprint(
                prompt_version=PROMPT_VERSION,
                schema_version=MAIL_REPLY_DRAFT_SCHEMA_VERSION,
                reply_to_message_id=message.id,
                to_addresses=recipients,
                subject=subject,
                thread_message_ids=thread.thread_message_ids,
                thread_content_fingerprints=thread.thread_content_fingerprints,
                context_query=query,
                root_id=root_id,
                evidence=context.evidence_identities(),
            ),
        )
        model = self._model  # the guard above proved it is present
        payload = await model.complete_json(self._build_request(context))
        now = self._clock.now()
        completed, sources = parse_mail_draft_output(
            payload, plan=plan, context=context, now=now
        )
        stored = await self._drafts.add_draft(completed, sources)
        LOGGER.info(
            "mail draft created account=%s sources=%d open_questions=%d",
            stored.account_id,
            len(sources),
            len(stored.needs_user_input),
        )
        return MailDraftResult(
            draft=stored,
            sources=tuple(await self._drafts.list_sources(stored.id)),
            offline_roots=offline_roots,
            context_truncated=context.context_truncated,
        )

    async def get_draft(self, reference: MailDraftId | str) -> MailDraftResult:
        """Return one draft and its stored sources.

        Raises:
            MailDraftNotFound: no such draft.
            AmbiguousId: a prefix matched several drafts.
        """
        draft = await self._require_draft(reference)
        return MailDraftResult(
            draft=draft, sources=tuple(await self._drafts.list_sources(draft.id))
        )

    async def list_drafts(self, *, limit: int | None = 20) -> list[MailDraftListing]:
        """List drafts, newest update first."""
        return [
            MailDraftListing(draft=draft, source_count=await self._drafts.count_sources(draft.id))
            for draft in await self._drafts.list_drafts(limit=limit)
        ]

    async def edit_draft(
        self,
        reference: MailDraftId | str,
        *,
        subject: str | None = None,
        body: str | None = None,
        needs_user_input: tuple[str, ...] | None = None,
    ) -> MailDraft:
        """Edit a draft's subject and/or body under optimistic concurrency.

        Recipients and generation provenance are not editable: a reply goes where the headers say
        it goes, and a draft must keep the record of what it was generated from.

        Raises:
            ValueError: no field was supplied to change.
            InvalidMailDraft: the new value breaks a draft invariant.
            MailDraftNotFound: no such draft.
            StaleMailDraftUpdate: someone else changed the draft first.
        """
        if subject is None and body is None and needs_user_input is None:
            raise ValueError("an edit needs at least one of subject, body or open questions")
        current = await self._require_draft(reference)
        now = self._clock.now()
        updated = replace(
            current,
            subject=current.subject if subject is None else subject,
            body_text=current.body_text if body is None else body,
            needs_user_input=(
                current.needs_user_input if needs_user_input is None else needs_user_input
            ),
            origin=MailDraftOrigin.USER_EDITED,
            version=current.version + 1,
            updated_at=now,
        )
        return await self._drafts.update_draft(updated, expected_version=current.version)

    # ------------------------------------------------------------------ internals

    async def _require_draft(self, reference: MailDraftId | str) -> MailDraft:
        draft_id = (
            reference
            if isinstance(reference, UUID)
            else await self._drafts.resolve_draft_id(reference)
        )
        draft = await self._drafts.get_draft(draft_id)
        if draft is None:
            raise MailDraftNotFound(reference)
        return draft

    async def _thread_context(
        self, message: MailMessage
    ) -> tuple[MailContext, UUID | None]:
        """The thread the reply belongs to, read-only.

        A draft never creates thread membership: threading is the analysis pipeline's decision,
        and a read-only path must not change mail state to make its own life easier. When the
        message has no membership yet, the context is the message itself.
        """
        member = await self._intelligence.get_member(message.id)
        if member is None:
            return self._context_builder.build_standalone(message), None
        thread = await self._context_builder.build(message, member.thread_id)
        return thread, member.thread_id

    async def _knowledge_evidence(
        self, query: str | None, *, root_id: str | None, context_limit: int
    ) -> tuple[tuple[KnowledgeEvidence, ...], tuple[str, ...]]:
        """Bounded personal knowledge — only when the user asked for it by name."""
        if query is None:
            # The whole privacy boundary in one branch: no query, no search, no evidence.
            return (), ()
        grounded: GroundedContext = await self._knowledge.build(
            query, root_id=root_id, limit=context_limit
        )
        return grounded.evidence, grounded.offline_roots

    def _build_request(self, context: MailDraftContext) -> ModelRequest:
        """One USER message holding canonical JSON; the rules live in `instructions`."""
        return ModelRequest(
            instructions=MAIL_REPLY_DRAFT_INSTRUCTIONS,
            messages=(ModelMessage(role=ModelRole.USER, content=context.to_json()),),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=MAIL_REPLY_DRAFT_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._reasoning_effort),
            max_output_tokens=self._max_output_tokens,
        )


def _clean_query(context_query: str | None) -> str | None:
    if context_query is None:
        return None
    stripped = context_query.strip()
    return stripped or None


def parse_mail_draft_output(
    payload: dict[str, Any],
    *,
    plan: MailDraftPlan,
    context: MailDraftContext,
    now: datetime,
) -> tuple[MailDraft, tuple[MailDraftSource, ...]]:
    """Turn schema-valid output into a validated draft, or refuse it.

    Pure on purpose: this is where the knowledge boundary lives, so it must be testable without a
    provider. A cited id that was not supplied is refused outright; it is never dropped, repaired
    or looked up again.

    Raises:
        MailDraftInvalidKnowledgeReference: the draft cited an unsupplied knowledge id.
        InvalidMailDraft: the answer is schema-valid but not a usable draft.
    """
    body = payload.get("body")
    if not isinstance(body, str):
        raise InvalidMailDraft("a draft needs a body string")
    used_ids = _source_ids(payload.get("used_source_ids"), allowed=context.evidence_ids)
    questions = _questions(payload.get("needs_user_input"))
    completed = mail_draft_from_plan(
        plan,
        body_text=body,
        needs_user_input=questions,
        origin=MailDraftOrigin.MODEL_GENERATED,
        at=now,
    )
    return completed, _sources(completed.id, context.used_evidence(used_ids))


def _source_ids(raw: object, *, allowed: frozenset[EvidenceId]) -> tuple[EvidenceId, ...]:
    if not isinstance(raw, list):
        raise InvalidMailDraft("used_source_ids must be a JSON array")
    resolved: list[EvidenceId] = []
    for value in raw:
        if not isinstance(value, str):
            raise InvalidMailDraft("used_source_ids entries must be strings")
        try:
            source_id = EvidenceId(value)
        except InvalidGroundedAnswer as exc:
            raise InvalidMailDraft(f"used source id is malformed: {value!r}") from exc
        if source_id not in allowed:
            raise MailDraftInvalidKnowledgeReference(source_id)
        if source_id not in resolved:
            resolved.append(source_id)
    return tuple(resolved)


def _questions(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise InvalidMailDraft("needs_user_input must be a JSON array")
    questions: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InvalidMailDraft("needs_user_input entries must be strings")
        questions.append(item)
    return tuple(questions)


def _sources(
    draft_id: MailDraftId, evidence: tuple[KnowledgeEvidence, ...]
) -> tuple[MailDraftSource, ...]:
    """The stored provenance: identity and location, never the excerpt text."""
    return tuple(
        MailDraftSource(
            draft_id=draft_id,
            root_id=item.root_id,
            entry_id=item.entry_id,
            chunk_id=item.chunk_id,
            logical_uri=item.logical_uri,
            source_span=item.source_span,
            ordinal=position,
        )
        for position, item in enumerate(evidence)
    )


__all__ = [
    "MailDraftListing",
    "MailDraftResult",
    "MailDraftService",
    "parse_mail_draft_output",
]
