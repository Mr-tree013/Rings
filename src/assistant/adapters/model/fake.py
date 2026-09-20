"""FakeModelAdapter: a scripted ModelPort for tests and deterministic evals (ADR-0017).

It implements the *port*, not a provider: it queues whatever the test wants to happen — a
response, a provider error, a raw answer — and records every request it received. Interpreter
and evaluation tests can then run with no network, no key and no cost.

It is never reachable from host configuration: `provider = "fake"` is rejected by the config
parser, so a fake can never silently stand in for a real provider in production.
"""

from __future__ import annotations

from collections.abc import Sequence

from assistant.domain.model import ModelRequest, ModelResponse, ModelUsage


class FakeModelAdapter:
    """A ModelPort whose answers are supplied by the test."""

    def __init__(
        self,
        responses: Sequence[ModelResponse | Exception] = (),
        *,
        model: str = "fake-model",
    ) -> None:
        self.responses: list[ModelResponse | Exception] = list(responses)
        self.requests: list[ModelRequest] = []
        self._model = model

    @property
    def model(self) -> str:
        """The model name this fake reports on its responses."""
        return self._model

    def queue_text(
        self,
        text: str,
        *,
        usage: ModelUsage | None = None,
        response_id: str | None = None,
    ) -> FakeModelAdapter:
        """Queue a successful text answer."""
        self.responses.append(
            ModelResponse(
                text=text,
                model=self._model,
                response_id=response_id,
                usage=usage,
                provider="fake",
            )
        )
        return self

    def queue_error(self, error: Exception) -> FakeModelAdapter:
        """Queue a failure to raise instead of answering."""
        self.responses.append(error)
        return self

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Record the request and return (or raise) the next scripted answer."""
        self.requests.append(request)
        if not self.responses:
            raise IndexError("FakeModelAdapter has no scripted response left")
        queued = self.responses.pop(0)
        if isinstance(queued, Exception):
            raise queued
        return queued


__all__ = ["FakeModelAdapter"]
