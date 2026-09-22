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

from assistant.domain.errors import (
    ActionExecutionUnknown,
    ActionExecutionUnresolved,
    ActionNotExecutable,
    AmbiguousAttentionReference,
    AmbiguousId,
    ApprovalChallengeConsumed,
    ApprovalChallengeExpired,
    ApprovalUnavailable,
    CannotCancelSafely,
    CapabilityUnavailable,
    EHallBrowserUnavailable,
    EHallDisabled,
    EHallLoginRequired,
    EHallServiceMismatch,
    EHallUnexpectedOrigin,
    EHallUnsupportedRequiredField,
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailCredentialsMissing,
    MailSendNotConfigured,
    McpDisabled,
    McpToolUnavailable,
    ModelAuthenticationError,
    ModelCredentialsMissing,
    ModelNotConfigured,
    ModelRateLimited,
    ModelTransientError,
    ModelUnavailable,
    PlanningNotConfigured,
    PlanProposalNotPending,
    PlaybookReplayUnsupported,
    StaleCaseUpdate,
    StaleConversationCard,
    StaleMailDraftUpdate,
    StalePlanProposal,
    StaleTaskUpdate,
    StorageRootOffline,
    WebWatchRequestFailed,
)


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


_NOT_CONFIGURED = (
    ModelNotConfigured,
    PlanningNotConfigured,
    MailSendNotConfigured,
    MailConfigurationError,
    EHallDisabled,
    McpDisabled,
)
_AUTH_REQUIRED = (ModelAuthenticationError, MailAuthenticationError, EHallLoginRequired)
_CONNECTION_FAILED = (
    MailConnectionError,
    ModelUnavailable,
    ModelTransientError,
    ModelRateLimited,
    EHallBrowserUnavailable,
    WebWatchRequestFailed,
    StorageRootOffline,
)
_CREDENTIAL_MISSING = (ModelCredentialsMissing, MailCredentialsMissing)
_UNSUPPORTED_CAPABILITY = (
    CapabilityUnavailable,
    McpToolUnavailable,
    PlaybookReplayUnsupported,
    EHallServiceMismatch,
    EHallUnsupportedRequiredField,
    EHallUnexpectedOrigin,
)
_AMBIGUOUS_REFERENCE = (AmbiguousId, AmbiguousAttentionReference)
_UNKNOWN_EXTERNAL_RESULT = (ActionExecutionUnknown, ActionExecutionUnresolved)
_STALE_CONFIRMATION = (
    StaleConversationCard,
    StalePlanProposal,
    PlanProposalNotPending,
    StaleMailDraftUpdate,
    StaleTaskUpdate,
    StaleCaseUpdate,
    ApprovalChallengeExpired,
    ApprovalChallengeConsumed,
    ApprovalUnavailable,
    ActionNotExecutable,
)


def refusal_code_for(error: BaseException) -> ConversationRefusalCode | None:
    """How one failure should be described to a person, or `None` when it is not a refusal.

    A closed mapping over this project's own error types: the point of it is that "not configured",
    "needs a login", "the network is down" and "an external result is unknown" stop collapsing into
    one apology (ADR-0045 §4). An error outside this vocabulary is left to the caller, which
    renders it without any internal text.
    """
    if isinstance(error, _NOT_CONFIGURED):
        return ConversationRefusalCode.NOT_CONFIGURED
    if isinstance(error, _AUTH_REQUIRED):
        return ConversationRefusalCode.AUTH_REQUIRED
    if isinstance(error, _CONNECTION_FAILED):
        return ConversationRefusalCode.CONNECTION_FAILED
    if isinstance(error, _CREDENTIAL_MISSING):
        return ConversationRefusalCode.CREDENTIAL_MISSING
    if isinstance(error, _UNSUPPORTED_CAPABILITY):
        return ConversationRefusalCode.UNSUPPORTED_CAPABILITY
    if isinstance(error, _AMBIGUOUS_REFERENCE):
        return ConversationRefusalCode.AMBIGUOUS_REFERENCE
    if isinstance(error, _UNKNOWN_EXTERNAL_RESULT):
        return ConversationRefusalCode.UNKNOWN_EXTERNAL_RESULT
    if isinstance(error, CannotCancelSafely):
        return ConversationRefusalCode.CANNOT_CANCEL_SAFELY
    if isinstance(error, _STALE_CONFIRMATION):
        return ConversationRefusalCode.STALE_CONFIRMATION
    return None


__all__ = ["ConversationErrorCode", "ConversationRefusalCode", "refusal_code_for"]
