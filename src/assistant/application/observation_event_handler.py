"""One handler for both observation event types: classify, and nothing else (ADR-0029).

```text
InboundEvent(web.page.changed)      ─┐
InboundEvent(manual.input.received) ─┴─► resolve the source row ── verify the event link
                                              │
                                              ▼
                                  bounded untrusted context (diff / quoted text)
                                              │
              stored analysis with the same fingerprint? ──► reuse it, no provider call
                                              ▼
                StructuredModel (closed schema) ──► local validation ──► durable analysis
```

Two event types share one handler because they share one job, and the job has one shape: classify
and extract candidates. The handler writes exactly one kind of durable state — an
`observation_analyses` row — and nothing in its imports could write another: no task service, no
case service, no planner, no fact service, no playbook service, no action or approval service, and
no HTTP client. The prompt says the same thing, but the structure is what makes it true.

A retried event whose analysis is already stored under the same fingerprint costs nothing: the
stored row is returned and the provider is not called again. That is the whole reason the
fingerprint exists.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from assistant.application.observation_analysis import parse_observation_analysis_output
from assistant.application.observation_analysis_prompt import OBSERVATION_ANALYSIS_INSTRUCTIONS
from assistant.application.observation_analysis_schema import (
    OBSERVATION_ANALYSIS_SCHEMA_V1,
    OBSERVATION_ANALYSIS_SCHEMA_VERSION,
)
from assistant.application.observation_context import (
    ObservationContext,
    ObservationContextBuilder,
)
from assistant.application.structured_model import StructuredModel
from assistant.domain.errors import (
    ManualInputNotFound,
    ModelAuthenticationError,
    ModelBillingError,
    ModelConfigurationError,
    ModelCredentialsMissing,
    ModelInvalidRequest,
    ModelNotConfigured,
    ModelOutputNotJson,
    ModelOutputSchemaViolation,
    ModelProtocolError,
    ModelRateLimited,
    ModelResponseIncomplete,
    ModelTransientError,
    ModelUnavailable,
    ObservationEventLinkMismatch,
    PermanentEventError,
    WebObservationNotFound,
)
from assistant.domain.inbound_event import InboundEvent
from assistant.domain.model import (
    ModelMessage,
    ModelOutputMode,
    ModelReasoningEffort,
    ModelRequest,
    ModelRole,
)
from assistant.domain.observation_analysis import (
    ANALYZER_VERSION,
    observation_analysis_input_fingerprint,
)
from assistant.ports.clock import Clock
from assistant.ports.manual_input_repository import ManualInputRepository
from assistant.ports.observation_analysis_repository import ObservationAnalysisRepository
from assistant.ports.web_watch_repository import WebWatchRepository

LOGGER = logging.getLogger("assistant.observations")

WEB_EVENT_TYPE = "web.page.changed"
MANUAL_EVENT_TYPE = "manual.input.received"
ACCEPTED_EVENT_TYPES = (WEB_EVENT_TYPE, MANUAL_EVENT_TYPE)
"""The only two event types this handler accepts. Anything else is a permanent failure."""

PERMANENT_MODEL_ERRORS = (
    ModelAuthenticationError,
    ModelBillingError,
    ModelInvalidRequest,
    ModelCredentialsMissing,
    ModelConfigurationError,
    ModelNotConfigured,
)
"""Provider failures a second identical attempt cannot fix."""

RETRYABLE_MODEL_ERRORS = (
    ModelRateLimited,
    ModelTransientError,
    ModelUnavailable,
    ModelProtocolError,
    ModelResponseIncomplete,
    ModelOutputNotJson,
    ModelOutputSchemaViolation,
)
"""Provider or output failures the EventWorker's bounded retry policy handles."""


class ObservationInboundEventHandler:
    """Turns a web change or a manual input into a durable analysis. Nothing else."""

    def __init__(
        self,
        observations: WebWatchRepository,
        manual: ManualInputRepository,
        analyses: ObservationAnalysisRepository,
        contexts: ObservationContextBuilder,
        model: StructuredModel,
        clock: Clock,
        *,
        analyzer_version: int = ANALYZER_VERSION,
        reasoning_effort: str = "low",
        max_output_tokens: int = 2048,
    ) -> None:
        self._observations = observations
        self._manual = manual
        self._analyses = analyses
        self._contexts = contexts
        self._model = model
        self._clock = clock
        self._analyzer_version = analyzer_version
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens

    async def handle(self, event: InboundEvent) -> None:
        """Analyze the observation or input this event names, once.

        Raises:
            PermanentEventError: the event type is not accepted, or its payload is not the exact
                identity document the bridge writes.
            ObservationEventLinkMismatch: the event and the stored link disagree; retryable,
                because the bridge repairs the link on its next round.
            WebObservationNotFound: the named observation is not stored.
            ManualInputNotFound: the named input is not stored.
        """
        if event.event_type == WEB_EVENT_TYPE:
            context = await self._web_context(event)
        elif event.event_type == MANUAL_EVENT_TYPE:
            context = await self._manual_context(event)
        else:
            raise PermanentEventError(
                f"the observation handler does not accept event type {event.event_type!r}"
            )
        fingerprint = observation_analysis_input_fingerprint(
            analyzer_version=self._analyzer_version,
            schema_version=OBSERVATION_ANALYSIS_SCHEMA_VERSION,
            event_type=event.event_type,
            event_external_id=event.external_id,
            source_identities=context.source_identities,
            context_fingerprint=context.fingerprint,
        )
        stored = await self._analyses.get_analysis(event.id)
        if (
            stored is not None
            and stored.analyzer_version == self._analyzer_version
            and stored.input_fingerprint == fingerprint
        ):
            # A retried event must not pay for an analysis it already has.
            LOGGER.debug("observation analysis reused event=%s", event.id)
            return
        analysis = parse_observation_analysis_output(
            await self._complete(context.document),
            event_id=event.id,
            source_kind=event.event_type,
            input_fingerprint=fingerprint,
            now=self._clock.now(),
            analyzer_version=self._analyzer_version,
        )
        await self._analyses.persist_analysis(analysis)
        # Counts and a category only: never page text, never pasted text.
        LOGGER.info(
            "observation analyzed type=%s category=%s candidates=%d",
            event.event_type,
            analysis.category.value,
            len(analysis.action_candidates),
        )

    # ------------------------------------------------------------------- sources

    async def _web_context(self, event: InboundEvent) -> ObservationContext:
        payload = _parse_payload(event, keys={"observation_id", "target_id"})
        observation_id = _uuid(payload["observation_id"], event, "observation_id")
        observation = await self._observations.get_observation(observation_id)
        if observation is None:
            raise WebObservationNotFound(observation_id)
        if payload["target_id"] != observation.target_id:
            raise ObservationEventLinkMismatch(
                event.id,
                f"the event names target {payload['target_id']!r} but observation "
                f"{observation.id} belongs to {observation.target_id!r}",
            )
        if await self._observations.get_linked_event_id(observation.id) != event.id:
            # Either the link has not been written yet or it points somewhere else; retry rather
            # than analyze under an identity that does not hold.
            raise ObservationEventLinkMismatch(
                event.id, f"observation {observation.id} is not linked to this event"
            )
        return await self._contexts.for_web_change(observation)

    async def _manual_context(self, event: InboundEvent) -> ObservationContext:
        payload = _parse_payload(event, keys={"manual_input_id", "source"})
        input_id = _uuid(payload["manual_input_id"], event, "manual_input_id")
        manual_input = await self._manual.get_input(input_id)
        if manual_input is None:
            raise ManualInputNotFound(input_id)
        if payload["source"] != manual_input.source.value:
            raise ObservationEventLinkMismatch(
                event.id,
                f"the event names source {payload['source']!r} but input {manual_input.id} "
                f"came from {manual_input.source.value!r}",
            )
        if await self._manual.get_linked_event_id(manual_input.id) != event.id:
            raise ObservationEventLinkMismatch(
                event.id, f"manual input {manual_input.id} is not linked to this event"
            )
        return await self._contexts.for_manual(manual_input)

    # --------------------------------------------------------------------- model

    async def _complete(self, document: str) -> dict[str, Any]:
        """Call the model, mapping provider failures onto the existing retry policy."""
        request = ModelRequest(
            instructions=OBSERVATION_ANALYSIS_INSTRUCTIONS,
            messages=(ModelMessage(role=ModelRole.USER, content=document),),
            output_mode=ModelOutputMode.JSON_SCHEMA,
            json_schema=OBSERVATION_ANALYSIS_SCHEMA_V1,
            reasoning_effort=ModelReasoningEffort(self._reasoning_effort),
            max_output_tokens=self._max_output_tokens,
        )
        try:
            return await self._model.complete_json(request)
        except PermanentEventError:
            raise
        except PERMANENT_MODEL_ERRORS as exc:
            raise PermanentEventError(str(exc)) from exc


def _parse_payload(event: InboundEvent, *, keys: set[str]) -> dict[str, Any]:
    """Read the identity payload of an observation event, refusing anything else.

    The payload carries identity and nothing else: page text or pasted text in an event row would
    duplicate content into the event log. Extra keys are a producer bug, not something to guess
    around.

    Raises:
        PermanentEventError: the payload is missing, malformed or carries other keys.
    """
    if event.content is None:
        raise PermanentEventError(f"event {event.id} carries no payload")
    try:
        decoded = json.loads(event.content)
    except json.JSONDecodeError as exc:
        raise PermanentEventError(f"event {event.id} payload is not JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise PermanentEventError(f"event {event.id} payload is not a JSON object")
    if set(decoded) != keys:
        raise PermanentEventError(
            f"event {event.id} payload keys are {sorted(decoded)}, expected exactly "
            f"{sorted(keys)}"
        )
    return decoded


def _uuid(raw: object, event: InboundEvent, field: str) -> UUID:
    """Read one UUID from an event payload, or refuse the event permanently."""
    if not isinstance(raw, str):
        raise PermanentEventError(f"event {event.id} payload has no {field}")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise PermanentEventError(
            f"event {event.id} payload {field} is not a UUID"
        ) from exc


__all__ = [
    "ACCEPTED_EVENT_TYPES",
    "MANUAL_EVENT_TYPE",
    "PERMANENT_MODEL_ERRORS",
    "RETRYABLE_MODEL_ERRORS",
    "WEB_EVENT_TYPE",
    "ObservationInboundEventHandler",
]
