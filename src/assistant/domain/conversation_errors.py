"""The finite set of failure codes the conversation surface recognizes (ADR-0035 §34).

It lives in the domain because it is vocabulary, not a transport detail: the renderer, the service,
the interpreter and the diagnostics logger all name the same failure the same way, and none of them
has to import another layer to do it.

One code per situation a user can actually meet, so that three callers can agree without sharing
Python exception text: the renderer picks the sentence, the tests assert the behaviour, and the
diagnostic log records the code. Exception text stays where it belongs — in debug output and in
`pw`, never in a normal conversation reply.
"""

from __future__ import annotations

from enum import StrEnum


class ConversationErrorCode(StrEnum):
    """Why a turn could not do what the user asked."""

    INPUT_DECODE_FAILED = "input_decode_failed"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_INVALID_OUTPUT = "model_invalid_output"
    MODEL_REPAIR_FAILED = "model_repair_failed"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    UNSUPPORTED_SEMANTICS = "unsupported_semantics"
    OPERATION_FAILED = "operation_failed"
    OPERATION_UNCERTAIN = "operation_uncertain"
    INTERNAL_ERROR = "internal_error"


__all__ = ["ConversationErrorCode"]
