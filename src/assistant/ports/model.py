"""ModelPort: the only way application code talks to a language model (ADR-0017).

One method, one direction: send a provider-neutral request, get final text and accounting
back. There is deliberately no `stream`, no `tools`, no `conversation` and no `memory` — a
capability that could take an action needs its own application boundary, designed on purpose,
not a flag on this port.

Implementations are adapters: they own credentials, endpoints, HTTP and provider error
translation. Nothing about a provider may appear in this module.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from assistant.domain.model import ModelRequest, ModelResponse


@runtime_checkable
class ModelPort(Protocol):
    """Completes a prompt and returns validated-candidate final text."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the model's final answer.

        Raises:
            ModelTransientError: a network or provider failure a caller may retry.
            ModelResponseIncomplete: the provider stopped before finishing.
            ModelProtocolError: the provider's answer cannot be interpreted.
        """
        ...


__all__ = ["ModelPort"]
