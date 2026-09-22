"""Errors that belong to the project's vocabulary.

They live in the domain layer because ports and store both speak them: application code
must be able to handle `DuplicateInboundEvent` without ever importing `sqlite3` or the
store implementation (ADR-0002, ADR-0008).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from assistant.domain.conversation_errors import ConversationErrorCode
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


class InvalidAttentionItem(DomainError):
    """An `AttentionItem` was built with values that break its invariants (ADR-0042)."""


class InvalidMailSettings(DomainError):
    """A mail account setting, or a diagnostic about one, is malformed (ADR-0043)."""


class MailAccountSettingsNotFound(DomainError):
    """No configured mail account has that id."""


class AttentionItemNotFound(DomainError):
    """No attention item has that identity."""


class AmbiguousAttentionReference(DomainError):
    """A reference matched more than one live attention item, so nothing was settled."""


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


class InvalidContact(DomainError):
    """A contact's name, address or lifecycle breaks an invariant."""


class ContactNotFound(DomainError):
    """No contact exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"contact {reference} does not exist")


class InvalidNewMailDraft(DomainError):
    """A new-mail draft's recipient, subject, body or version breaks an invariant."""


class NewMailDraftNotFound(DomainError):
    """No new-mail draft exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"new mail draft {reference} does not exist")


class MailRecipientUnresolved(DomainError):
    """A recipient, or the account to send from, could not be resolved deterministically.

    Raised before any draft is written and before any model is asked to guess: the runtime either
    knows which address a name means, or it asks. It never invents one.
    """


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


class InvalidMailSend(DomainError):
    """An outbound send payload, its link or its reconciliation breaks an invariant."""


class MailDraftNeedsUserInput(DomainError):
    """A draft still carries unanswered questions, so it cannot be prepared for sending.

    The user has to read them (`pw mail draft acknowledge`) or edit the body before an exact
    action can be produced. This is checked before anything is created, so a refusal costs nothing.
    """

    def __init__(self, draft_id: object) -> None:
        self.draft_id = draft_id
        super().__init__(
            f"mail draft {draft_id} has open questions the user has not acknowledged; "
            "review them with `pw mail draft show` and acknowledge with "
            "`pw mail draft acknowledge`"
        )


class MailSendNotConfigured(DomainError):
    """The draft's account has no usable outbound SMTP configuration.

    A receive-only account is legitimate; it just cannot send. No action is created.
    """

    def __init__(self, account_id: object, reason: str) -> None:
        self.account_id = account_id
        self.reason = reason
        super().__init__(
            f"mail account {account_id!r} cannot send: {reason}"
        )


class MailSendAlreadyPrepared(DomainError):
    """This exact draft version already has a prepared (or cancelled) send action.

    Re-preparing requires advancing the draft first — an edit or an acknowledgement — so that one
    version can never accumulate two approvals for the same letter.
    """

    def __init__(self, draft_id: object, draft_version: int, action_id: object) -> None:
        self.draft_id = draft_id
        self.draft_version = draft_version
        self.action_id = action_id
        super().__init__(
            f"mail draft {draft_id} version {draft_version} already has action {action_id}; "
            "edit or acknowledge the draft to advance its version before preparing another send"
        )


class CaseNotOpen(DomainError):
    """A terminal case cannot receive new work."""

    def __init__(self, case_id: object, status: object) -> None:
        self.case_id = case_id
        self.status = status
        super().__init__(f"case {case_id} is {status} and cannot receive a new action")


class MailSendNotReconcilable(DomainError):
    """The action is not in a state where a Sent-folder lookup means anything."""

    def __init__(self, action_id: object, reason: str) -> None:
        self.action_id = action_id
        self.reason = reason
        super().__init__(f"mail send {action_id} cannot be reconciled: {reason}")


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


class InvalidCase(DomainError):
    """A case was built with values that break its invariants."""


class InvalidCaseTransition(DomainError):
    """A status transition was attempted that the case lifecycle forbids."""

    def __init__(self, current: object, target: object) -> None:
        self.current = current
        self.target = target
        super().__init__(f"case cannot move from {current} to {target}")


class CaseNotFound(DomainError):
    """No case exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"case {reference} does not exist")


class StaleCaseUpdate(DomainError):
    """An update was built on a case version that has since changed, or that never existed."""

    def __init__(
        self, case_id: object, expected_updated_at: object, actual_updated_at: object
    ) -> None:
        self.case_id = case_id
        self.expected_updated_at = expected_updated_at
        self.actual_updated_at = actual_updated_at
        super().__init__(
            f"case {case_id} was modified since {expected_updated_at} "
            f"(it is now {actual_updated_at}); reload and retry"
        )


class InvalidActionRequest(DomainError):
    """An action request, its payload or its fingerprint breaks an invariant."""


class InvalidActionPayload(DomainError):
    """A payload is not JSON data: `NaN`, bytes, a datetime or an arbitrary object."""


class ActionRequestNotFound(DomainError):
    """No action request exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"action request {reference} does not exist")


class ActionNotExecutable(DomainError):
    """The action is not PREPARED, so it cannot be executed any more."""


class ActionFingerprintMismatch(DomainError):
    """The payload no longer hashes to the fingerprint the action claims.

    Raised after re-hashing the stored payload, never by comparing the stored field with itself.
    """

    def __init__(self, action_id: object) -> None:
        self.action_id = action_id
        super().__init__(
            f"action request {action_id} no longer matches its stored fingerprint"
        )


class CapabilityUnavailable(DomainError):
    """No executor is registered for this action type.

    The registered set is deliberately small; a type that can be *named* is not a type that can
    be performed.
    """

    def __init__(self, action_type: object) -> None:
        self.action_type = action_type
        super().__init__(f"capability unavailable for action type {action_type}")


class InvalidApproval(DomainError):
    """An approval, a challenge or its token breaks an invariant."""


class ApprovalChallengeNotFound(DomainError):
    """No approval challenge exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"approval challenge {reference} does not exist")


class InvalidApprovalToken(DomainError):
    """The presented token is not the one this challenge was issued with.

    The message never contains the presented token: a wrong secret is not echoed back.
    """


class ApprovalChallengeExpired(DomainError):
    """The challenge is past its expiry and cannot be redeemed."""


class ApprovalChallengeConsumed(DomainError):
    """The challenge has already been redeemed. Single use means single use."""


class ApprovalAlreadyOutstanding(DomainError):
    """A live approval for this action already exists.

    The previous one must be used, must expire, or must be superseded explicitly — a second
    approval for the same action may never exist while the first is still valid.
    """


class ApprovalUnavailable(DomainError):
    """No usable approval exists for this action and fingerprint.

    Raised when an execution finds no unconsumed, unexpired approval — including when another
    caller consumed the only one a moment earlier.
    """


class InvalidExecutionRun(DomainError):
    """An execution run was built with values that break its invariants."""


class EHallDisabled(DomainError):
    """The host has not enabled the eHall pipeline.

    Disabled is the default, and it is not an error condition for the rest of the project: a host
    that only reads mail never touches a browser.
    """


class EHallLoginRequired(DomainError):
    """The eHall session is not logged in (or the login expired).

    Raised by inspection and by execution *before* anything is typed. The user runs
    `pw ehall login` and completes SSO by hand; this project never sees the university password.
    """


class EHallBrowserUnavailable(DomainError):
    """Playwright or its Chromium runtime is not usable on this host.

    The package is a declared dependency, but the browser binary is installed separately:
    `uv run playwright install chromium`.
    """


class EHallUnexpectedOrigin(DomainError):
    """Top-level navigation left the whitelisted NJU origins, so the pipeline stopped."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            f"eHall navigation left the allowed NJU origins ({url}); refusing to continue"
        )


class EHallServiceMismatch(DomainError):
    """The certificate service could not be identified exactly, or its page did not match.

    Zero matches, several matches and a missing page marker all end here: the pipeline never
    guesses which service the user meant, and never continues on a page it does not recognise.
    """


class EHallPageChanged(DomainError):
    """The live form no longer matches the page contract that was approved.

    The university may change its portal at any time. When the contract changes, the prepared
    action is stale and nothing is typed or submitted.
    """

    def __init__(self, action_id: object) -> None:
        self.action_id = action_id
        super().__init__(
            f"the eHall certificate form no longer matches the approved page contract for "
            f"action {action_id}; prepare a new action and approve it again"
        )


class InvalidEHallForm(DomainError):
    """A form snapshot, a field definition, a value or a payload breaks an invariant."""


class InvalidMobileSecurity(DomainError):
    """A pairing token, a web session or a CSRF companion breaks its invariants."""


class MobileDisabled(DomainError):
    """The host has not enabled the mobile control plane.

    Disabled is the default: a host that never opens a socket to the network is a perfectly good
    configuration.
    """


class MobilePairingTokenInvalid(DomainError):
    """The presented pairing code is unknown, expired or already used.

    One error for all three: a caller learning *which* of them applies would learn something about
    the tokens that exist.
    """


class MobileSessionInvalid(DomainError):
    """The presented session is unknown, expired or revoked."""


class MobileSessionNotFound(DomainError):
    """No session exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"mobile session {reference} does not exist")


class MobileClientRejected(DomainError):
    """The request did not come from a loopback or private address.

    Proxy headers are never consulted for this decision: a client cannot claim to be local.
    """

    def __init__(self, peer: str) -> None:
        self.peer = peer
        super().__init__(f"mobile control plane refuses a non-private client ({peer})")


class MobileCsrfRejected(DomainError):
    """A mutation arrived without a session-bound CSRF token."""


class EHallActionMismatch(DomainError):
    """The action is not an eHall certificate action, so an eHall view cannot describe it."""

    def __init__(self, action_id: object, reason: str) -> None:
        self.action_id = action_id
        self.reason = reason
        super().__init__(f"action {action_id} is not a certificate submission: {reason}")


class EHallUnsupportedRequiredField(DomainError):
    """The form requires a control this pipeline deliberately cannot fill.

    A required file upload is the user's job, not a click this project is willing to fake.
    """


class ExecutionRunNotFound(DomainError):
    """No execution run exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"execution run {reference} does not exist")


class ActionExecutionUnresolved(DomainError):
    """A previous attempt has no decidable outcome, so nothing may start another one.

    A `RUNNING` run (a crash) or an `UNKNOWN` result (an ambiguous external outcome) both mean the
    side effect may already have happened. Retrying is a decision for a later reconciliation, not
    for an automatic retry or a fresh approval.
    """

    def __init__(self, action_id: object, status: object) -> None:
        self.action_id = action_id
        self.status = status
        super().__init__(
            f"action request {action_id} has an unresolved execution ({status}); "
            "do not retry it blindly"
        )


class ActionExecutionUnknown(DomainError):
    """An execution ended without a decidable result and was recorded as UNKNOWN."""

    def __init__(self, action_id: object) -> None:
        self.action_id = action_id
        super().__init__(
            f"execution of action request {action_id} is unknown: "
            "the external effect may or may not have happened"
        )


class NotificationNotFound(DomainError):
    """No notification exists for the requested identity."""

    def __init__(self, notification_id: object) -> None:
        self.notification_id = notification_id
        super().__init__(f"notification {notification_id} does not exist")


class InvalidCorrection(DomainError):
    """A user correction is blank or longer than the store accepts."""


class CorrectionNotFound(DomainError):
    """No correction exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"correction {reference} does not exist")


class InvalidFactKey(DomainError):
    """A fact key is not a well-formed namespaced identifier."""


class ForbiddenFactKey(DomainError):
    """A fact key names something a personal fact store must never hold.

    Passwords, tokens, credentials and private keys are not facts about a person; they are
    secrets, and this store is not a credential store. The refusal is on the *key*, so the
    project never has to guess whether a value "looks like" a password.
    """

    def __init__(self, key: str, segment: str) -> None:
        self.key = key
        self.segment = segment
        super().__init__(
            f"fact key {key!r} is refused: {segment!r} is a credential-like key segment, and "
            "the fact store is not a credential store"
        )


class InvalidFactCandidate(DomainError):
    """A candidate fact breaks its invariants (blank value, bad validity window)."""


class FactCandidateNotFound(DomainError):
    """No candidate exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"fact candidate {reference} does not exist")


class InvalidFactCandidateTransition(DomainError):
    """A candidate was confirmed or rejected from a state that forbids it."""

    def __init__(self, candidate_id: object, current: object, target: object) -> None:
        self.candidate_id = candidate_id
        self.current = current
        self.target = target
        super().__init__(
            f"fact candidate {candidate_id} is {current} and cannot become {target}"
        )


class ExpiredFactCandidate(DomainError):
    """The candidate's proposed validity window has already closed.

    Confirming it would create a fact whose `valid_until` is not after its `valid_from`, which the
    domain refuses: propose a candidate with a window that is still open.
    """

    def __init__(self, candidate_id: object, proposed_valid_until: object) -> None:
        self.candidate_id = candidate_id
        self.proposed_valid_until = proposed_valid_until
        super().__init__(
            f"fact candidate {candidate_id} proposed validity until {proposed_valid_until}, "
            "which has already passed; propose a new candidate instead"
        )


class InvalidConfirmedFact(DomainError):
    """A confirmed fact breaks its invariants (blank value, bad validity ordering)."""


class ConfirmedFactNotFound(DomainError):
    """No confirmed fact exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"confirmed fact {reference} does not exist")


class InvalidPlaybookCandidate(DomainError):
    """A playbook candidate breaks its invariants (blank name, bad snapshot fingerprint)."""


class PlaybookCandidateNotFound(DomainError):
    """No playbook candidate exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"playbook candidate {reference} does not exist")


class InvalidPlaybookReplayTest(DomainError):
    """A replay test breaks its invariants (unknown issue code, incoherent status)."""


class InvalidPlaybook(DomainError):
    """A playbook breaks its invariants (blank name, bad provenance, bad status)."""


class PlaybookNotFound(DomainError):
    """No playbook exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"playbook {reference} does not exist")


class PlaybookCandidateExists(DomainError):
    """This successful action already seeded a reviewed candidate.

    One action, one candidate: re-running the same execution must not multiply audit rows, and
    rejecting a candidate is a decision, not a reason to try again with a better name.
    """

    def __init__(self, action_id: object) -> None:
        self.action_id = action_id
        super().__init__(
            f"action {action_id} already has a playbook candidate; a successful action can seed "
            "one reviewed candidate only"
        )


class InvalidPlaybookCandidateTransition(DomainError):
    """A candidate was promoted or rejected from a state that forbids it."""

    def __init__(self, candidate_id: object, current: object, target: object) -> None:
        self.candidate_id = candidate_id
        self.current = current
        self.target = target
        super().__init__(
            f"playbook candidate {candidate_id} is {current} and cannot become {target}"
        )


class InvalidPlaybookTransition(DomainError):
    """A playbook was retired from a state that forbids it."""

    def __init__(self, playbook_id: object, current: object, target: object) -> None:
        self.playbook_id = playbook_id
        self.current = current
        self.target = target
        super().__init__(f"playbook {playbook_id} is {current} and cannot become {target}")


class PlaybookSourceNotEligible(DomainError):
    """The action this candidate would come from is not a definitive success.

    Only an `EXECUTED` action with a `SUCCEEDED` run can seed a candidate: a `FAILED`,
    `UNKNOWN`, `RUNNING` or still-`PREPARED` action teaches nothing about what worked.
    """

    def __init__(self, action_id: object, reason: str) -> None:
        self.action_id = action_id
        self.reason = reason
        super().__init__(f"action {action_id} cannot seed a playbook candidate: {reason}")


class PlaybookSourceUnsupported(DomainError):
    """No replay validator exists for this action type, so the candidate could never be tested."""

    def __init__(self, action_type: object) -> None:
        self.action_type = action_type
        super().__init__(
            f"no replay validator is registered for action type {action_type}; this capability "
            "cannot be reviewed as a playbook"
        )


class PlaybookReplayUnsupported(DomainError):
    """A replay was requested for an action type this deployment cannot validate."""

    def __init__(self, action_type: object) -> None:
        self.action_type = action_type
        super().__init__(f"replay validation is unavailable for action type {action_type}")


class PlaybookSourceIntegrityError(DomainError):
    """The stored source action or execution no longer matches what the candidate recorded.

    A payload whose fingerprint does not re-hash, an execution that is not the one named, or a
    source that stopped being a success is corruption or tampering — not a failing dry run, and
    never something to record as an ordinary result and continue past.
    """

    def __init__(self, action_id: object, reason: str) -> None:
        self.action_id = action_id
        self.reason = reason
        super().__init__(
            f"the source action {action_id} is no longer usable as provenance: {reason}"
        )


class PlaybookCandidateNotTested(DomainError):
    """Promotion needs a replay test that passed against the current contract."""

    def __init__(self, candidate_id: object, reason: str) -> None:
        self.candidate_id = candidate_id
        self.reason = reason
        super().__init__(
            f"playbook candidate {candidate_id} has no current passing dry run: {reason}"
        )


class InvalidWebTarget(DomainError):
    """A configured watcher target breaks its invariants (bad id, unusable URL)."""


class WebWatchUnsafeUrl(DomainError):
    """A watcher URL is not something this project is willing to call.

    V1 accepts only `https` URLs with no credentials, no IP-literal host and the default port: a
    watcher is a fixed public page, not a generic HTTP client.
    """

    def __init__(self, url: object, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"watcher URL {url!r} is refused: {reason}")


class WebWatchUnsafeAddress(DomainError):
    """A watcher hostname resolved to an address that is not a public one."""

    def __init__(self, host: str, address: str, reason: str) -> None:
        self.host = host
        self.address = address
        self.reason = reason
        super().__init__(
            f"watcher host {host!r} resolves to {address!r}, which is {reason}"
        )


class WebWatchRedirectNotAllowed(DomainError):
    """The server answered with a redirect. Watchers never follow one."""

    def __init__(self, url: str, status: int, location: str | None) -> None:
        self.url = url
        self.status = status
        self.location = location
        super().__init__(
            f"watcher target {url!r} answered {status}"
            + (f" and wants to redirect to {location!r}" if location else "")
            + "; redirects are not followed — configure the final URL instead"
        )


class WebWatchUnsupportedContentType(DomainError):
    """A watcher response is not one of the three content types this phase understands."""

    def __init__(self, content_type: object) -> None:
        self.content_type = content_type
        super().__init__(
            f"watcher responses must be text/html, text/plain or application/json "
            f"(got {content_type!r})"
        )


class WebWatchResponseTooLarge(DomainError):
    """A watcher response exceeded the configured byte budget while streaming."""

    def __init__(self, url: str, limit: int) -> None:
        self.url = url
        self.limit = limit
        super().__init__(
            f"the response from {url!r} exceeded {limit} bytes and was abandoned"
        )


class WebWatchRequestFailed(DomainError):
    """A watcher request failed: a network error, a timeout or an unusable status."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"could not fetch watcher target {url!r}: {reason}")


class WebObservationNotFound(DomainError):
    """No web observation exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"web observation {reference} does not exist")


class WebTargetNotFound(DomainError):
    """No configured watcher target has that id."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"no watcher target is configured with id {reference!r}")


class InvalidManualInput(DomainError):
    """Manual input breaks its invariants (blank text, too long, unknown source)."""


class ManualInputNotFound(DomainError):
    """No manual input exists for the requested identity."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"manual input {reference} does not exist")


class InvalidObservationAnalysis(DomainError):
    """A structured observation analysis breaks its schema or its invariants."""


class ObservationAnalysisNotFound(DomainError):
    """No analysis exists for the requested inbound event."""

    def __init__(self, reference: object) -> None:
        self.reference = reference
        super().__init__(f"no observation analysis exists for {reference}")


class ObservationEventLinkMismatch(DomainError):
    """An observation or manual input is not linked to the event that claims it."""

    def __init__(self, event_id: object, reason: str) -> None:
        self.event_id = event_id
        self.reason = reason
        super().__init__(f"event {event_id} does not name its source: {reason}")


class WebSnapshotStorageError(DomainError):
    """A normalized snapshot could not be written or verified against its content address."""


class McpDisabled(DomainError):
    """The host has not enabled the local MCP surface."""


class McpToolUnavailable(DomainError):
    """A capability the MCP surface does not have on this host was requested.

    It is raised rather than advertised-and-refused: when a capability is not configured, its tool
    does not exist in the server's registry at all, so a client cannot call it by guessing a name.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(
            f"this MCP server does not expose {capability}; enable it in the host configuration "
            "if you want it"
        )


class McpInvalidArgument(DomainError):
    """An MCP tool was called with an argument the tool does not accept."""

    def __init__(self, argument: str, reason: str) -> None:
        self.argument = argument
        self.reason = reason
        super().__init__(f"{argument}: {reason}")


class InvalidBackupArchive(DomainError):
    """A backup archive is malformed, unsafe, or claims something it does not contain."""


class BackupSourceMissing(DomainError):
    """The runtime database references a content object that is not on disk.

    A backup that silently skipped it would be a restore point with a hole in it, so the whole
    operation fails instead.
    """

    def __init__(self, storage_key: str) -> None:
        self.storage_key = storage_key
        super().__init__(f"runtime data references {storage_key!r}, which is missing")


class BackupSourceCorrupt(DomainError):
    """A content object does not match the hash the database recorded for it."""

    def __init__(self, storage_key: str) -> None:
        self.storage_key = storage_key
        super().__init__(
            f"the content object {storage_key!r} does not match its recorded hash"
        )


class RestoreDestinationRejected(DomainError):
    """A restore destination is not a usable, empty, non-active directory."""

    def __init__(self, path: object, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"restore destination {path} is refused: {reason}")


class IntegrityCheckFailed(DomainError):
    """A read-only integrity check found a problem. Nothing is repaired automatically."""


# --------------------------------------------------------------- tree conversation (ADR-0033)


class ConversationError(DomainError):
    """Base class for every conversation-runtime failure."""


class RecurringRuleNotFound(DomainError):
    """No such weekly recurring rule."""


class InvalidConversationThread(ConversationError):
    """A thread would be stored in a state the domain does not allow."""


class InvalidConversationMessage(ConversationError):
    """A message would be stored in a state the domain does not allow."""


class InvalidConversationOperation(ConversationError):
    """An operation would be stored in a state the domain does not allow."""


class InvalidConversationPlan(ConversationError):
    """A model answer is not a usable conversation plan."""


class ConversationThreadNotFound(ConversationError):
    """No such conversation thread."""


class ConversationTurnFailed(ConversationError):
    """A conversation turn could not be completed."""


class ConversationOperationNotFound(ConversationError):
    """No such conversation operation."""


class ConversationCapabilityUnavailable(ConversationError):
    """The plan asked for an operation this build does not offer."""


class ConversationConfirmationRequired(ConversationError):
    """The operation needs an explicit conversational confirmation first."""


class ConversationConfirmationExpired(ConversationError):
    """A pending confirmation was answered after it expired."""


class ConversationNeedsClarification(ConversationError):
    """The request is ambiguous; the runtime asks instead of guessing."""


class ConversationTimezoneRequired(ConversationError):
    """A relative civil time arrived with no configured planning timezone."""


class ConversationOperationUnknown(ConversationError):
    """An operation was interrupted mid-apply; its outcome is not knowable locally."""


class ConversationInterpretationFailed(ConversationError):
    """The model's answer could not be turned into a usable plan, even after one repair attempt.

    `code` is one of the finite conversation error codes; `detail` is developer-facing text that is
    never shown in the normal user interface (ADR-0035 §8, §34).
    """

    def __init__(self, code: ConversationErrorCode, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or "the answer could not be interpreted")


# ------------------------------------------------- durable browser requests (ADR-0041)


class InvalidConversationRequest(ConversationError):
    """An accepted-input row would be stored in a state the domain does not allow."""


class ConversationRequestNotFound(ConversationError):
    """No such conversation request."""


class ConversationRequestCancelled(ConversationError):
    """The user asked to stop this request, and the runtime reached a safe checkpoint.

    Raised inside a turn and caught by the runtime: it is a control signal, never a message shown
    to the user. The turn it interrupts is recorded as `INTERRUPTED`, and no pending operation of
    that turn is ever applied (ADR-0041 §30).
    """


class CannotCancelSafely(ConversationError):
    """Stop was refused because the request has crossed a boundary that cannot be undone.

    The refusal is the honest answer: local state or an external effect has already moved, and a
    browser button cannot roll that back (ADR-0041 §31-§33).
    """


class StaleConversationCard(ConversationError):
    """A confirmation card no longer describes the durable state it would settle.

    Nothing is applied, approved or executed: the caller refreshes its snapshot and shows the
    current card (ADR-0041 §24, §27).
    """


class UnknownConversationCard(ConversationError):
    """The card id names a kind or a target this build does not settle."""


__all__ = [
    "ActionExecutionUnknown",
    "ActionExecutionUnresolved",
    "ActionFingerprintMismatch",
    "ActionNotExecutable",
    "ActionRequestNotFound",
    "AmbiguousId",
    "ApprovalAlreadyOutstanding",
    "ApprovalChallengeConsumed",
    "ApprovalChallengeExpired",
    "ApprovalChallengeNotFound",
    "ApprovalUnavailable",
    "BackupSourceCorrupt",
    "BackupSourceMissing",
    "CalendarEventNotActive",
    "CalendarEventNotFound",
    "CannotCancelSafely",
    "CapabilityUnavailable",
    "CaseNotFound",
    "CaseNotOpen",
    "ConfiguredRootNotFound",
    "ConfirmedFactNotFound",
    "ContentExtractionError",
    "ContentTooLarge",
    "ConversationCapabilityUnavailable",
    "ConversationConfirmationExpired",
    "ConversationConfirmationRequired",
    "ConversationError",
    "ConversationInterpretationFailed",
    "ConversationNeedsClarification",
    "ConversationOperationNotFound",
    "ConversationOperationUnknown",
    "ConversationRequestCancelled",
    "ConversationRequestNotFound",
    "ConversationThreadNotFound",
    "ConversationTimezoneRequired",
    "ConversationTurnFailed",
    "CorrectionNotFound",
    "DeadlineNotFound",
    "DomainError",
    "DuplicateCommitment",
    "DuplicateInboundEvent",
    "EHallActionMismatch",
    "EHallBrowserUnavailable",
    "EHallDisabled",
    "EHallLoginRequired",
    "EHallPageChanged",
    "EHallServiceMismatch",
    "EHallUnexpectedOrigin",
    "EHallUnsupportedRequiredField",
    "EventNotFound",
    "ExecutionRunNotFound",
    "ExpiredFactCandidate",
    "FactCandidateNotFound",
    "FileChangedDuringExtraction",
    "ForbiddenFactKey",
    "Fts5Unavailable",
    "GroundedAnswerInputTooLong",
    "GroundedAnswerInvalidCitation",
    "GroundedAnswerSemanticError",
    "IntegrityCheckFailed",
    "InterpreterInputTooLong",
    "InterpreterInvalidReference",
    "InterpreterSemanticError",
    "InvalidActionPayload",
    "InvalidActionRequest",
    "InvalidApproval",
    "InvalidApprovalToken",
    "InvalidAssistantConfig",
    "InvalidBackupArchive",
    "InvalidCalendarEvent",
    "InvalidCase",
    "InvalidCaseTransition",
    "InvalidCatalogEntry",
    "InvalidCommandDraft",
    "InvalidCommitment",
    "InvalidConfirmedFact",
    "InvalidConversationMessage",
    "InvalidConversationOperation",
    "InvalidConversationPlan",
    "InvalidConversationRequest",
    "InvalidConversationThread",
    "InvalidCorrection",
    "InvalidDeadline",
    "InvalidEHallForm",
    "InvalidEventClaim",
    "InvalidEventTransition",
    "InvalidExecutionRun",
    "InvalidFactCandidate",
    "InvalidFactCandidateTransition",
    "InvalidFactKey",
    "InvalidGroundedAnswer",
    "InvalidInboundEvent",
    "InvalidInterpretationResult",
    "InvalidKnowledgeDocument",
    "InvalidMailAnalysis",
    "InvalidMailDraft",
    "InvalidMailMessage",
    "InvalidMailboxState",
    "InvalidManualInput",
    "InvalidMobileSecurity",
    "InvalidModelRequest",
    "InvalidModelSchema",
    "InvalidNotification",
    "InvalidObservationAnalysis",
    "InvalidPlanBlock",
    "InvalidPlaybook",
    "InvalidPlaybookCandidate",
    "InvalidPlaybookCandidateTransition",
    "InvalidPlaybookReplayTest",
    "InvalidPlaybookTransition",
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
    "InvalidWebTarget",
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
    "McpDisabled",
    "McpInvalidArgument",
    "McpToolUnavailable",
    "MobileClientRejected",
    "MobileCsrfRejected",
    "MobileDisabled",
    "MobilePairingTokenInvalid",
    "MobileSessionInvalid",
    "MobileSessionNotFound",
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
    "ObservationAnalysisNotFound",
    "ObservationEventLinkMismatch",
    "PermanentEventError",
    "PermanentScheduledJobError",
    "PlanBlockNotActive",
    "PlanBlockNotFound",
    "PlanProposalNotFound",
    "PlanProposalNotPending",
    "PlanningNotConfigured",
    "PlanningSnapshotChanged",
    "PlanningStateUnstable",
    "PlaybookCandidateExists",
    "PlaybookCandidateNotFound",
    "PlaybookCandidateNotTested",
    "PlaybookNotFound",
    "PlaybookReplayUnsupported",
    "PlaybookSourceIntegrityError",
    "PlaybookSourceNotEligible",
    "PlaybookSourceUnsupported",
    "RecurringRuleNotFound",
    "RestoreDestinationRejected",
    "ScheduledJobNotFound",
    "StaleCaseUpdate",
    "StaleConversationCard",
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
    "UnknownConversationCard",
    "UnknownStorageRoot",
    "UnsafeFilePath",
    "UnsafeStorageRoot",
    "VaultAlreadyInitialized",
    "VaultNotInitialized",
    "WebObservationNotFound",
    "WebSnapshotStorageError",
    "WebTargetNotFound",
    "WebWatchRedirectNotAllowed",
    "WebWatchRequestFailed",
    "WebWatchResponseTooLarge",
    "WebWatchUnsafeAddress",
    "WebWatchUnsafeUrl",
    "WebWatchUnsupportedContentType",
    "WorkSessionNotFound",
]
