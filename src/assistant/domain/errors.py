"""Errors that belong to the project's vocabulary.

They live in the domain layer because ports and store both speak them: application code
must be able to handle `DuplicateInboundEvent` without ever importing `sqlite3` or the
store implementation (ADR-0002, ADR-0008).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from assistant.domain.inbound_event import EventStatus


class DomainError(Exception):
    """Base class for errors that are part of the project's vocabulary."""


class InvalidInboundEvent(DomainError):
    """An `InboundEvent` was built with values that break its invariants."""


class InvalidEventTransition(DomainError):
    """A status transition was attempted that the event state machine forbids."""

    def __init__(self, current: EventStatus, target: EventStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"inbound event cannot move from {current} to {target}")


class UnexpectedEventStatus(DomainError):
    """The stored status is not the one the caller declared it expected.

    This is the compare-and-set guard of `EventRepository.transition`: a mismatch means
    another writer moved the event first, so the caller's decision is stale.
    """

    def __init__(self, event_id: UUID, expected: EventStatus, actual: EventStatus) -> None:
        self.event_id = event_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"inbound event {event_id} is {actual}, but the caller expected {expected}"
        )


class DuplicateInboundEvent(DomainError):
    """An event with the same `(source, external_id)` identity already exists.

    Raised by `EventRepository.add` when the database-level uniqueness guarantee rejects
    a second record. The duplicate is never silently reported as a successful insert.
    """

    def __init__(self, source: str, external_id: str) -> None:
        self.source = source
        self.external_id = external_id
        super().__init__(f"inbound event {source!r}/{external_id!r} already exists")


class EventNotFound(DomainError):
    """No inbound event exists for the requested identity."""

    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        super().__init__(f"inbound event {event_id} does not exist")


class InvalidEventClaim(DomainError):
    """An `EventClaim` was built with values that break its invariants."""


class StaleEventClaim(DomainError):
    """A worker tried to finish work whose claim is no longer the current one.

    The lease expired and another worker reclaimed the event, or the event moved on in
    some other way. The caller must stop: its result belongs to a superseded attempt and
    may never overwrite the state of the newer claim (ADR-0010).
    """

    def __init__(self, event_id: UUID, claim_token: UUID) -> None:
        self.event_id = event_id
        self.claim_token = claim_token
        super().__init__(f"claim {claim_token} on inbound event {event_id} is no longer current")


class PermanentEventError(DomainError):
    """A handler failed in a way that retrying cannot fix.

    Raised by handlers to send an event straight to dead letter instead of consuming the
    remaining retry budget (ADR-0010).
    """


class InvalidStorageRoot(DomainError):
    """A storage root was declared with an invalid identity or label."""


class InvalidStorageUri(DomainError):
    """A logical storage URI is malformed, absolute, or tries to traverse out of its root."""


class StorageRootConflict(DomainError):
    """An existing root_id was reused with a different storage kind.

    A root's identity is permanent (ADR-0011): the same id cannot be a local folder in one
    scan and an archive vault in the next.
    """


class InvalidVaultManifest(DomainError):
    """A `.pa/vault.toml` is missing required fields, malformed, or self-contradictory."""


class VaultNotInitialized(DomainError):
    """The directory has no `.pa/vault.toml`, so it is not an archive vault yet.

    Initialisation is always explicit: seeing a removable drive must never create a
    manifest on its own.
    """


class VaultAlreadyInitialized(DomainError):
    """The directory already has a `.pa/vault.toml`; initialisation never overwrites it."""


class InvalidCatalogEntry(DomainError):
    """A catalog record, snapshot entry or scan result breaks its invariants."""


class InvalidSourceSpan(DomainError):
    """A source span is neither a usable line range nor a usable page number."""


class InvalidKnowledgeDocument(DomainError):
    """Extracted content or knowledge-index state breaks its invariants."""


class UnknownStorageRoot(DomainError):
    """The catalog has no such storage root."""


class UnknownCatalogEntry(DomainError):
    """The catalog has no such entry under that root."""


class StorageRootOffline(DomainError):
    """A storage root's physical location is not currently present.

    Offline is a runtime condition, never a catalog state: nothing is marked missing and no
    index metadata is discarded because a drive is unplugged.
    """


class StorageRootIdentityMismatch(DomainError):
    """The storage mounted at a root's known path is not the root we catalogued.

    A different vault id at the same mount point means the index (and any content read)
    would belong to someone else's data, so the operation is refused.
    """


class UnsafeFilePath(DomainError):
    """A path is unsafe to read: symlink, non-regular file, or escaping its root."""


class FileChangedDuringExtraction(DomainError):
    """The file no longer matches the catalog metadata that was used to plan the read."""


class ContentTooLarge(DomainError):
    """A file exceeds the size limit for its extractor."""


class ContentExtractionError(DomainError):
    """An extractor could not read a file it claims to support.

    Adapter-specific exceptions (for example pypdf's) are translated into this error so no
    third-party exception type crosses the adapter boundary.
    """


class Fts5Unavailable(DomainError):
    """This SQLite build has no FTS5 module."""


class TrigramTokenizerUnavailable(DomainError):
    """This SQLite build has FTS5 but not the trigram tokenizer."""


class KnowledgeIndexMismatch(DomainError):
    """An index database is bound to a different root or storage kind."""


class KnowledgeIndexNeedsRebuild(DomainError):
    """An index database was written by a schema version this build does not understand."""


class KnowledgeIndexCorrupt(DomainError):
    """An index database cannot be read.

    The index is derived data: recovery is a deliberate rebuild, never an automatic delete.
    """


class InvalidAssistantConfig(DomainError):
    """The host configuration is malformed, self-contradictory or unsupported."""


class ConfiguredRootNotFound(DomainError):
    """The configuration has no storage root with that id."""


class UnsafeStorageRoot(DomainError):
    """A configured root path cannot be scanned automatically.

    Background indexing refuses root-level symlinks and non-directories: following a root
    symlink would silently index a different tree than the one the user configured.
    """


class InvalidCommitment(DomainError):
    """Base class for commitment-domain validation failures."""


class InvalidTask(InvalidCommitment):
    """A task breaks its invariants (blank title, bad estimate, inconsistent timestamps)."""


class InvalidDeadline(InvalidCommitment):
    """A deadline breaks its invariants."""


class InvalidCalendarEvent(InvalidCommitment):
    """A calendar event breaks its invariants (non-positive duration, naive time)."""


class InvalidPlanBlock(InvalidCommitment):
    """A plan block breaks its invariants."""


class InvalidWorkSession(InvalidCommitment):
    """A work session breaks its invariants."""


class InvalidTimeInterval(DomainError):
    """An interval is not a usable half-open range."""


class DuplicateCommitment(DomainError):
    """A task, deadline, event, plan block or session with that identity already exists."""


class InvalidTaskTransition(DomainError):
    """A task status transition was attempted that the state machine forbids."""

    def __init__(self, current: object, target: object) -> None:
        self.current = current
        self.target = target
        super().__init__(f"task cannot move from {current} to {target}")


class TaskNotOpen(DomainError):
    """The operation requires an OPEN task."""


class TaskNotFound(DomainError):
    """No task exists for the requested identity."""

    def __init__(self, task_id: object) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} does not exist")


class DeadlineNotFound(DomainError):
    """The task has no active deadline."""

    def __init__(self, task_id: object) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id} has no active deadline")


class CalendarEventNotFound(DomainError):
    """No calendar event exists for the requested identity."""

    def __init__(self, event_id: object) -> None:
        self.event_id = event_id
        super().__init__(f"calendar event {event_id} does not exist")


class PlanBlockNotFound(DomainError):
    """No plan block exists for the requested identity."""

    def __init__(self, plan_block_id: object) -> None:
        self.plan_block_id = plan_block_id
        super().__init__(f"plan block {plan_block_id} does not exist")


class WorkSessionNotFound(DomainError):
    """No work session exists for the requested identity."""

    def __init__(self, session_id: object) -> None:
        self.session_id = session_id
        super().__init__(f"work session {session_id} does not exist")


class CalendarEventNotActive(DomainError):
    """The calendar event is already cancelled."""


class PlanBlockNotActive(DomainError):
    """The plan block is already cancelled."""


class StaleTaskUpdate(DomainError):
    """An update was built on a task version that has since changed.

    `updated_at` is the optimistic concurrency token: a caller that read the task at T1 and
    writes with `expected_updated_at = T1` is rejected once anyone else has moved the task,
    so two callers can never silently overwrite each other.
    """

    def __init__(self, task_id: object, expected_updated_at: object) -> None:
        self.task_id = task_id
        self.expected_updated_at = expected_updated_at
        super().__init__(
            f"task {task_id} was modified since {expected_updated_at}; reload and retry"
        )


class AmbiguousId(DomainError):
    """An id prefix matched more than one object."""

    def __init__(self, prefix: str, matches: int) -> None:
        self.prefix = prefix
        self.matches = matches
        super().__init__(f"id prefix {prefix!r} is ambiguous ({matches} matches)")


class PlanningNotConfigured(DomainError):
    """The host config has no `[planning]` section.

    The planner never guesses a timezone or working hours: without explicit configuration it
    refuses to plan.
    """


class PlanProposalNotFound(DomainError):
    """No plan proposal exists for the requested identity."""

    def __init__(self, proposal_id: object) -> None:
        self.proposal_id = proposal_id
        super().__init__(f"plan proposal {proposal_id} does not exist")


class StalePlanProposal(DomainError):
    """A proposal was built from planning input that has since changed.

    The proposal is marked stale and nothing is written: applying it could overwrite a newer
    deadline, a new busy block or recorded work.
    """

    def __init__(self, proposal_id: object) -> None:
        self.proposal_id = proposal_id
        super().__init__(
            f"plan proposal {proposal_id} is stale; create a new plan with `pw plan week`"
        )


class PlanProposalNotPending(DomainError):
    """The proposal was already applied, superseded or marked stale."""

    def __init__(self, proposal_id: object, status: object) -> None:
        self.proposal_id = proposal_id
        self.status = status
        super().__init__(f"plan proposal {proposal_id} is {status}, not pending")


class PlanningSnapshotChanged(DomainError):
    """Commitment state changed between reading a planning snapshot and storing its proposal.

    The proposal is not persisted at all: a proposal that is born stale would only waste the
    user's review. The caller re-reads the snapshot and re-plans (bounded retries).
    """


class PlanningStateUnstable(DomainError):
    """Planning input kept changing across the allowed retries."""


class InvalidScheduledJob(DomainError):
    """A `ScheduledJob` was built with values that break its invariants."""


class InvalidScheduledJobClaim(DomainError):
    """A `ScheduledJobClaim` was built with values that break its invariants."""


class ScheduledJobNotFound(DomainError):
    """No scheduled job exists for the requested identity."""

    def __init__(self, job_id: object) -> None:
        self.job_id = job_id
        super().__init__(f"scheduled job {job_id} does not exist")


class StaleScheduledJobClaim(DomainError):
    """A worker tried to finish a job whose claim is no longer the current one.

    The lease expired and another worker reclaimed the job, or the job was cancelled while it
    was being processed. The caller's result belongs to a superseded attempt and must never
    overwrite the newer state (ADR-0016).
    """

    def __init__(self, job_id: object, claim_token: object) -> None:
        self.job_id = job_id
        self.claim_token = claim_token
        super().__init__(
            f"scheduled job {job_id} is not processing under claim {claim_token}"
        )


class InvalidScheduledJobPayload(DomainError):
    """A job payload is not the canonical JSON object its kind requires."""


class PermanentScheduledJobError(DomainError):
    """A job failed in a way that retrying cannot fix.

    Raised by handlers to send a job straight to dead letter instead of consuming the
    remaining retry budget (ADR-0016).
    """


class InvalidNotification(DomainError):
    """A `Notification` was built with values that break its invariants."""


class InvalidModelRequest(DomainError):
    """A `ModelRequest`, `ModelMessage`, `ModelUsage` or `ModelResponse` is malformed."""


class InvalidModelSchema(DomainError):
    """A JSON Schema is not a valid Draft 2020-12 schema, or its name is unusable."""


class ModelNotConfigured(DomainError):
    """The host config has no `[model]` section, so no model capability is available."""


class ModelConfigurationError(DomainError):
    """The model configuration itself is unusable (unknown provider, bad bounds)."""


class ModelCredentialsMissing(DomainError):
    """No provider credential is available in the environment.

    The message names the environment variable to set and never contains a credential.
    """


class ModelAuthenticationError(DomainError):
    """The provider rejected the credential."""


class ModelBillingError(DomainError):
    """The provider refused the request because the account has no balance."""


class ModelRateLimited(DomainError):
    """The provider is throttling this client."""


class ModelInvalidRequest(DomainError):
    """The provider rejected the request as malformed or invalid."""


class ModelTransientError(DomainError):
    """A network or provider failure that a caller may choose to retry.

    The adapter never retries by itself: a request that may already have been generated and
    billed must not be repeated behind the caller's back.
    """


class ModelUnavailable(DomainError):
    """The provider is reachable but is not serving requests right now."""


class ModelProtocolError(DomainError):
    """The provider answered with something this adapter cannot interpret.

    Raised for unexpected output items (for example a tool call that was never offered), for a
    response without final text, and for a body that is not the documented shape.
    """


class ModelResponseIncomplete(DomainError):
    """The provider stopped early: the response is not a usable answer."""


class ModelOutputNotJson(DomainError):
    """Structured output was requested but the text is not parseable JSON."""


class ModelOutputSchemaViolation(DomainError):
    """The parsed JSON does not satisfy the requested schema."""


class InvalidCommandDraft(DomainError):
    """A typed command draft was built with values that break its invariants."""


class InvalidInterpretationResult(DomainError):
    """An interpretation result mixes statuses (a draft with a question, and so on)."""


class InterpreterInputTooLong(DomainError):
    """The natural-language request is longer than the interpreter accepts."""


class InterpreterInvalidReference(DomainError):
    """The model referenced an entity that was not in the context it was given.

    Identity is authorised by the context, never by the database: a task id that was not
    supplied to the model does not become valid because a row with that id happens to exist.
    """

    def __init__(self, kind: object, reference: object) -> None:
        self.kind = kind
        self.reference = reference
        super().__init__(
            f"the model referenced {kind} {reference}, which was not in the supplied context"
        )


class InterpreterSemanticError(DomainError):
    """A schema-valid interpretation is not a usable command.

    Raised for a naive datetime, a backwards interval, a blank clarification, or any other case
    where the honest answer is "reject", never "guess".
    """


class InvalidGroundedAnswer(DomainError):
    """A grounded-answer value was built with values that break its invariants."""


class GroundedAnswerInputTooLong(DomainError):
    """The question is longer than the grounded-answer path accepts."""


class GroundedAnswerSemanticError(DomainError):
    """A schema-valid grounded answer is not a usable answer."""


class GroundedAnswerInvalidCitation(GroundedAnswerSemanticError):
    """An answer cited a source that was not supplied in the exact model request.

    A subclass of the semantic error because it *is* one: the answer was well formed and still
    unusable, and repairing it is not an option.
    """


class MailConfigurationError(DomainError):
    """The `[mail]` configuration itself is unusable."""


class MailCredentialsMissing(DomainError):
    """No credential is available in the environment for a configured mail account.

    The message names the environment variable to set and never contains a credential.
    """


class MailAuthenticationError(DomainError):
    """The mail server rejected the credential."""


class MailConnectionError(DomainError):
    """The mail server could not be reached, or the connection failed mid-conversation."""


class MailProtocolError(DomainError):
    """The mail server answered with something this adapter cannot interpret."""


class MailMessageNotFound(DomainError):
    """No mail message exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"mail message {reference} does not exist")


class InvalidMailMessage(DomainError):
    """A mail message, location, attachment or sync state breaks its invariants."""


class InvalidMailboxState(DomainError):
    """A mailbox cursor or UIDVALIDITY value is not a usable mailbox state."""


class MailRawStorageError(DomainError):
    """The raw message store could not persist or verify an object."""


class MailReconciliationConflict(DomainError):
    """Reconciliation evidence conflicts, so no automatic match may be made."""


class MailEventLinkMismatch(DomainError):
    """A mail event and the message it names disagree about their identity.

    Retryable on purpose: `mail_event_links` is written moments after the event is ingested, so
    a mismatch usually means the bridge has not finished yet rather than that the data is wrong.
    """


class InvalidMailDraft(DomainError):
    """A reply draft, its recipients or its stored sources break an invariant."""


class MailDraftNotFound(DomainError):
    """No draft exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"mail draft {reference} does not exist")


class MailReplyRecipientUnavailable(DomainError):
    """A reply has nobody to go to: neither `Reply-To` nor `From` names a usable mailbox.

    Raised before any model call, because a recipient is derived locally and a model is never
    asked to invent one.
    """


class MailDraftSourceUnavailable(DomainError):
    """The message a reply would answer has no readable body.

    An oversize (header-only) message, or one whose body could not be extracted, is not a basis
    for guessing what to write back.
    """


class MailDraftInvalidKnowledgeReference(DomainError):
    """A draft cited a knowledge source that was not supplied in that exact request.

    The method is the missing reference; the id is the model's claim about it.
    """

    def __init__(self, source_id: object) -> None:
        self.source_id = source_id
        super().__init__(
            f"the draft cited {source_id}, which was not among the supplied knowledge sources"
        )


class StaleMailDraftUpdate(DomainError):
    """An edit presented a draft version that is no longer current. The edit is refused."""

    def __init__(self, draft_id: object, expected: int, actual: int) -> None:
        self.draft_id = draft_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"mail draft {draft_id} is at version {actual}, but the edit expected {expected}"
        )


class InvalidMailAnalysis(DomainError):
    """A mail analysis, action candidate or thread record breaks its invariants."""


class MailAnalysisNotFound(DomainError):
    """No stored analysis exists for the requested message."""

    def __init__(self, message_id: object) -> None:
        self.message_id = message_id
        super().__init__(f"mail message {message_id} has no analysis")


class MailThreadNotFound(DomainError):
    """No thread exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"mail thread {reference} does not exist")


class NotificationNotFound(DomainError):
    """No notification exists for the requested identity."""

    def __init__(self, notification_id: object) -> None:
        self.notification_id = notification_id
        super().__init__(f"notification {notification_id} does not exist")


__all__ = [
    "AmbiguousId",
    "CalendarEventNotActive",
    "CalendarEventNotFound",
    "ConfiguredRootNotFound",
    "ContentExtractionError",
    "ContentTooLarge",
    "DeadlineNotFound",
    "DomainError",
    "DuplicateCommitment",
    "DuplicateInboundEvent",
    "EventNotFound",
    "FileChangedDuringExtraction",
    "Fts5Unavailable",
    "GroundedAnswerInputTooLong",
    "GroundedAnswerInvalidCitation",
    "GroundedAnswerSemanticError",
    "InterpreterInputTooLong",
    "InterpreterInvalidReference",
    "InterpreterSemanticError",
    "InvalidAssistantConfig",
    "InvalidCalendarEvent",
    "InvalidCatalogEntry",
    "InvalidCommandDraft",
    "InvalidCommitment",
    "InvalidDeadline",
    "InvalidEventClaim",
    "InvalidEventTransition",
    "InvalidGroundedAnswer",
    "InvalidInboundEvent",
    "InvalidInterpretationResult",
    "InvalidKnowledgeDocument",
    "InvalidMailAnalysis",
    "InvalidMailDraft",
    "InvalidMailMessage",
    "InvalidMailboxState",
    "InvalidModelRequest",
    "InvalidModelSchema",
    "InvalidNotification",
    "InvalidPlanBlock",
    "InvalidScheduledJob",
    "InvalidScheduledJobClaim",
    "InvalidScheduledJobPayload",
    "InvalidSourceSpan",
    "InvalidStorageRoot",
    "InvalidStorageUri",
    "InvalidTask",
    "InvalidTaskTransition",
    "InvalidTimeInterval",
    "InvalidVaultManifest",
    "InvalidWorkSession",
    "KnowledgeIndexCorrupt",
    "KnowledgeIndexMismatch",
    "KnowledgeIndexNeedsRebuild",
    "MailAnalysisNotFound",
    "MailAuthenticationError",
    "MailConfigurationError",
    "MailConnectionError",
    "MailCredentialsMissing",
    "MailDraftInvalidKnowledgeReference",
    "MailDraftNotFound",
    "MailDraftSourceUnavailable",
    "MailEventLinkMismatch",
    "MailMessageNotFound",
    "MailProtocolError",
    "MailRawStorageError",
    "MailReconciliationConflict",
    "MailReplyRecipientUnavailable",
    "MailThreadNotFound",
    "ModelAuthenticationError",
    "ModelBillingError",
    "ModelConfigurationError",
    "ModelCredentialsMissing",
    "ModelInvalidRequest",
    "ModelNotConfigured",
    "ModelOutputNotJson",
    "ModelOutputSchemaViolation",
    "ModelProtocolError",
    "ModelRateLimited",
    "ModelResponseIncomplete",
    "ModelTransientError",
    "ModelUnavailable",
    "NotificationNotFound",
    "PermanentEventError",
    "PermanentScheduledJobError",
    "PlanBlockNotActive",
    "PlanBlockNotFound",
    "PlanProposalNotFound",
    "PlanProposalNotPending",
    "PlanningNotConfigured",
    "PlanningSnapshotChanged",
    "PlanningStateUnstable",
    "ScheduledJobNotFound",
    "StaleEventClaim",
    "StaleMailDraftUpdate",
    "StalePlanProposal",
    "StaleScheduledJobClaim",
    "StaleTaskUpdate",
    "StorageRootConflict",
    "StorageRootIdentityMismatch",
    "StorageRootOffline",
    "TaskNotFound",
    "TaskNotOpen",
    "TrigramTokenizerUnavailable",
    "UnexpectedEventStatus",
    "UnknownCatalogEntry",
    "UnknownStorageRoot",
    "UnsafeFilePath",
    "UnsafeStorageRoot",
    "VaultAlreadyInitialized",
    "VaultNotInitialized",
    "WorkSessionNotFound",
]
