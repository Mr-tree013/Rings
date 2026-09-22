"""The finite set of failure codes the conversation surface recognizes (ADR-0035 §34).

It lives in the domain because it is vocabulary, not a transport detail: the renderer, the service,
the interpreter and the diagnostics logger all name the same failure the same way, and none of them
has to import another layer to do it.

One code per situation a user can actually meet, so that three callers can agree without sharing
Python exception text: the renderer picks the sentence, the tests assert the behaviour, and the
diagnostic log records the code. Exception text stays where it belongs — in debug output and in
`pw`, never in a normal conversation reply.

Phase 11E adds a second, closed vocabulary for the *refusals a readiness check produces* — a
capability that is not configured is not the same thing as one that needs a login, which is not the
same thing as an external result nobody can prove. Collapsing them all into "无法连接" is what
`ConversationRefusalCode` exists to prevent (ADR-0045 §4).
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


class ConversationRefusalCode(StrEnum):
    """Why a capability is not ready, stated in terms a person can act on.

    Each member is a *state of the world*, not an exception type: the renderer turns it into one
    sentence, and no member reveals a filesystem path, a credential, a provider payload or an
    exception string.
    """

    NOT_CONFIGURED = "not_configured"
    AUTH_REQUIRED = "auth_required"
    CONNECTION_FAILED = "connection_failed"
    CREDENTIAL_MISSING = "credential_missing"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    UNKNOWN_EXTERNAL_RESULT = "unknown_external_result"
    STALE_CONFIRMATION = "stale_confirmation"
    CANNOT_CANCEL_SAFELY = "cannot_cancel_safely"
    MISSING_PARAMETERS = "missing_parameters"


__all__ = ["ConversationErrorCode", "ConversationRefusalCode"]
