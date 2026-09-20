"""Source-grounded answers: question in, cited answer out, nothing executed (ADR-0019).

```text
question ──► deterministic search ──► bounded evidence ──┐
                                                          │  no evidence?
                                                          ├─► INSUFFICIENT_EVIDENCE (no model call)
                                                          ▼
                                        ModelPort (JSON schema) ──► local validation
                                                                        │ citations
                                                                        ▼
                                                              answer + local source map
```

The service is read-only by construction: it holds a context builder (knowledge search) and a
structured model, and nothing else. It cannot mutate a task, a plan, a file or the index, and it
falls back to nothing: if the personal index has no matching text, the answer is
`INSUFFICIENT_EVIDENCE` even when the model would happily answer from general knowledge.

The model's answer is untrusted after schema validation as well. Every cited source id is checked
against the exact evidence ids that were supplied in this request; an id that was not supplied
invalidates the answer instead of being dropped, guessed at or resolved later.
"""

from __future__ import annotations

import json
from typing import Any

from assistant.application.grounded_answer_prompt import GROUNDED_ANSWER_INSTRUCTIONS
from assistant.application.grounded_answer_schema import GROUNDED_ANSWER_SCHEMA_V1
from assistant.application.grounded_context import GroundedContext, GroundedContextBuilder
from assistant.application.structured_model import StructuredModel
from assistant.domain.config import ModelConfig
from assistant.domain.errors import (
    GroundedAnswerInputTooLong,
    GroundedAnswerSemanticError,
    InvalidGroundedAnswer,
)
from assistant.domain.grounded_answer import (
    EvidenceId,
    GroundedAnswer,
    GroundedAnswerResult,
    GroundedAnswerSegment,
    GroundedAnswerStatus,
    validate_citations,
)
from assistant.domain.model import (
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelRole,
)

MAX_QUESTION_CHARS = 4000
"""Beyond this the input is pasted content, not a question."""

NO_EVIDENCE_REASON = "No indexed source content matched the question."
"""The deterministic reason when retrieval found nothing usable."""


class GroundedAnswerService:
    """Answers a question from indexed personal sources, with local citations."""

    def __init__(
        self,
        context_builder: GroundedContextBuilder,
        model: StructuredModel,
        config: ModelConfig,
    ) -> None:
        self._context_builder = context_builder
        self._model = model
        self._config = config

    async def answer(
        self,
        question: str,
        *,
        root_id: str | None = None,
        limit: int = 8,
    ) -> GroundedAnswerResult:
        """Answer `question`, or say that the indexed evidence cannot.

        Raises:
            GroundedAnswerInputTooLong: the question exceeds the accepted length.
            GroundedAnswerSemanticError: a schema-valid answer is unusable.
            GroundedAnswerInvalidCitation: an answer cited a source it was not given.
            ModelOutputSchemaViolation: the answer does not match the schema.
        """
        context = await self._context_builder.build(
            _validate_question(question), root_id=root_id, limit=limit
        )
        return await self.answer_context(context)

    async def answer_context(self, context: GroundedContext) -> GroundedAnswerResult:
        """Answer from an already-built context.

        This is the model half of the flow, for callers that build the context themselves so
        they can decide whether a provider is needed at all.

        Raises:
            GroundedAnswerSemanticError: a schema-valid answer is unusable.
            GroundedAnswerInvalidCitation: an answer cited a source it was not given.
            ModelOutputSchemaViolation: the answer does not match the schema.
        """
        if not context.evidence:
            # No evidence, no provider call: the honest answer is already determined, and
            # asking a model would only invite world knowledge into a grounded answer.
            return insufficient_evidence_result(context)
        payload = await self._model.complete_json(self._build_request(context))
        answer = parse_grounded_answer_output(payload, context)
        return GroundedAnswerResult(
            answer=answer,
            evidence=context.evidence,
            metadata_matches=context.metadata_matches,
            offline_roots=context.offline_roots,
            context_truncated=context.context_truncated,
        )

    def _build_request(self, context: GroundedContext) -> ModelRequest:
        """One USER message holding canonical JSON; the rules live in `instructions`."""
        return ModelRequest(
            instructions=GROUNDED_ANSWER_INSTRUCTIONS,
            messages=(
                ModelMessage(
                    role=ModelRole.USER,
                    content=json.dumps(
                        context.to_payload(),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
            ),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=GROUNDED_ANSWER_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._config.reasoning_effort),
            max_output_tokens=self._config.max_output_tokens,
        )


def _validate_question(question: str) -> str:
    cleaned = question.strip()
    if not cleaned:
        raise GroundedAnswerSemanticError("there is no question to answer")
    if len(cleaned) > MAX_QUESTION_CHARS:
        raise GroundedAnswerInputTooLong(
            f"the question is {len(cleaned)} characters; the answer path accepts at most "
            f"{MAX_QUESTION_CHARS}"
        )
    return cleaned


def insufficient_evidence_result(context: GroundedContext) -> GroundedAnswerResult:
    """The deterministic "nothing to quote" result, with no model involved."""
    return GroundedAnswerResult(
        answer=GroundedAnswer(
            status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE,
            reason=NO_EVIDENCE_REASON,
        ),
        evidence=context.evidence,
        metadata_matches=context.metadata_matches,
        offline_roots=context.offline_roots,
        context_truncated=context.context_truncated,
    )


def parse_grounded_answer_output(
    payload: dict[str, Any], context: GroundedContext
) -> GroundedAnswer:
    """Turn a schema-valid answer into a validated one, or refuse it.

    Pure on purpose: this is where the citation boundary lives, so it must be testable without
    any provider at all.
    """
    status = payload.get("status")
    if status == GroundedAnswerStatus.INSUFFICIENT_EVIDENCE.value:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise GroundedAnswerSemanticError(
                "an insufficient-evidence answer needs a non-blank reason"
            )
        return GroundedAnswer(
            status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE, reason=reason
        )
    if status != GroundedAnswerStatus.ANSWERED.value:
        raise GroundedAnswerSemanticError(f"unknown answer status {status!r}")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise GroundedAnswerSemanticError("an answered result needs at least one segment")
    parsed = tuple(_segment(raw) for raw in segments)
    try:
        answer = GroundedAnswer(
            status=GroundedAnswerStatus.ANSWERED, segments=parsed
        )
    except InvalidGroundedAnswer as exc:
        raise GroundedAnswerSemanticError(str(exc)) from exc
    validate_citations(answer, context.evidence_ids)
    return answer


def _segment(raw: object) -> GroundedAnswerSegment:
    if not isinstance(raw, dict):
        raise GroundedAnswerSemanticError("an answer segment must be an object")
    text = raw.get("text")
    if not isinstance(text, str):
        raise GroundedAnswerSemanticError("an answer segment needs text")
    raw_ids = raw.get("source_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise GroundedAnswerSemanticError("an answer segment needs at least one source id")
    source_ids: list[EvidenceId] = []
    for value in raw_ids:
        if not isinstance(value, str):
            raise GroundedAnswerSemanticError("source ids must be strings")
        try:
            source_ids.append(EvidenceId(value))
        except InvalidGroundedAnswer as exc:
            raise GroundedAnswerSemanticError(str(exc)) from exc
    try:
        return GroundedAnswerSegment(text=text, source_ids=tuple(source_ids))
    except InvalidGroundedAnswer as exc:
        raise GroundedAnswerSemanticError(str(exc)) from exc


__all__ = [
    "MAX_QUESTION_CHARS",
    "NO_EVIDENCE_REASON",
    "GroundedAnswerService",
    "insufficient_evidence_result",
    "parse_grounded_answer_output",
]
