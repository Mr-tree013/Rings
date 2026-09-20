# ADR-0023 — Actions, Human Approval and the Execution Boundary

## Title

A side effect happens only after a human approves one exact `ActionRequest` fingerprint, and only
through an executor that this deployment actually implements.

## Status

Accepted

## Context

ADR-0006 froze the principle years of phases ago: every external side effect needs an approval
bound to a fingerprint. Phase 6A is where the principle becomes code, and where the failure modes
stop being hypothetical:

- **a prepared action is a claim about the future.** "Send this mail", "submit this form". If its
  content can change after approval, the approval stops meaning anything. If an approval can be
  reused, one decision authorises unbounded repetition;
- **the interesting moment is between approval and result.** The external system may have acted
  and then failed to answer. Guessing "it probably didn't happen" and retrying is how duplicates
  are made;
- **authority must not be reachable by accident.** An agent runtime that can mint its own approval
  has no approval boundary at all, only a logging habit;
- **capability is not vocabulary.** Naming `ehall.submit-certificate` is not the same as being
  able to submit a form. High-risk operations are safe when their implementation does not exist.

Everything here builds on what the project already has: `Repository` ports over one SQLite
database (ADR-0003, ADR-0008, ADR-0009), explicit state machines, and the rule that a model never
owns durable state (ADR-0017).

## Decision

1. A `Case` is the container for a multi-step transaction.
2. An `ActionRequest` is one prepared, exact external side-effect intent.
3. An `ActionRequest` payload is immutable once created; changed content requires a new
   `ActionRequest`.
4. An `ActionRequest` fingerprint is `SHA256(canonical JSON payload)`.
5. An `Approval` is always bound to `action_id` **and** the exact fingerprint.
6. An `Approval` can only be created by the explicit human approval service.
7. `Model`, `Interpreter`, `EventWorker`, `MailAnalysis` and `MailDraftService` have no code path
   that creates an `Approval`.
8. An approval challenge uses a random ≥256-bit token, and the database stores only the token's
   hash — never the plaintext.
9. A challenge and an approval are short-lived and single-use.
10. The executor must re-validate the `ActionRequest` fingerprint before executing.
11. Approval consumption and the `RUNNING` `ExecutionRun` happen in the same transaction.
12. Once an execution is `RUNNING`, its approval is consumed and must not be reused.
13. An unknown or ambiguous execution result must never be retried automatically.
14. The Phase 6A production executor capability set is empty.
15. There is no generic shell, browser or HTTP executor.
16. High-risk capabilities are absent because no implementation exists, not because a prompt
    forbids them.
17. Cases, actions, approvals and executions are durable runtime state.
18. Phase 6A does not create an `ActionRequest` from a `MailDraft` automatically.

Additional frozen details:

- **Canonical payloads only.** `null`, booleans, integers, finite floats, strings, sequences and
  string-keyed mappings. `NaN`, infinities, `bytes`, `datetime` objects and arbitrary Python
  objects are refused rather than coerced. The fingerprint is `SHA256` of
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`,
  and the entity re-derives it on construction, so a row whose payload and fingerprint disagree
  cannot be materialised at all.
- **Derived recipients of authority.** `create_challenge` loads a PREPARED action, re-hashes its
  payload, mints a token and stores only the hash. `approve` runs one transaction: the token hash
  is compared, the expiry and single use are checked, the action is re-verified as PREPARED with a
  matching fingerprint, an expired outstanding approval is superseded, the challenge is marked
  consumed and the `ApprovalRecord` is inserted. A failure rolls the whole thing back.
- **One live approval per action.** A partial unique index
  (`WHERE consumed_at IS NULL AND superseded_at IS NULL`) enforces it in the schema; the service
  refuses with `ApprovalAlreadyOutstanding` before the insert. Re-approving after expiry retires
  the old record as `superseded_at` rather than deleting or overwriting it, so the audit trail
  keeps every decision while only one can be live.
- **Ten-minute TTL.** `DEFAULT_APPROVAL_TTL_SECONDS = 600`, no new configuration. An approval
  inherits the challenge's `expires_at`.
- **Atomic start.** `begin_execution` re-hashes the payload, refuses when an unresolved earlier
  attempt exists, finds the exact unconsumed/unexpired/non-superseded approval, consumes it with
  `UPDATE … WHERE consumed_at IS NULL` (rowcount checked) and inserts the `RUNNING` run — all in
  one `BEGIN IMMEDIATE` transaction. Two racing callers cannot both succeed; the loser sees
  `ApprovalUnavailable` or `ActionExecutionUnresolved`, and the executor runs once.
- **Outcomes.** `SUCCEEDED` finishes the run and marks the action `EXECUTED` in the same
  transaction. `FAILED` finishes the run and leaves the action `PREPARED`, but the approval is
  spent: trying again needs a new human approval. `UNKNOWN` records that the effect may or may not
  have happened and blocks any further execution of that action until a future executor-specific
  reconciliation exists.
- **Crash semantics.** An executor that raises is recorded as `UNKNOWN` and surfaces as
  `ActionExecutionUnknown`. `asyncio.CancelledError` propagates untouched and leaves the run
  `RUNNING` under its consumed approval. Both are deliberate: an unresolved attempt is never
  silently turned into a retry.
- **No capability, no cost.** `pw action execute` resolves the executor before touching any
  approval, so a missing capability raises `CapabilityUnavailable` with the approval still valid
  and no run created. The production registry is empty and a test asserts it.
- **Token secrecy.** The token factory is injected (`secrets.token_urlsafe(32)` at the composition
  root) so neither the domain nor the application can import a randomness source; the plaintext is
  returned once from `create_challenge`, is never stored, never logged and never echoed in an error,
  and the CLI prints "shown once" alongside it.
- **Model and worker isolation.** No module on a model, interpreter, mail-analysis, mail-draft,
  event-worker, scheduler or index-sync path imports the approval service, and the approval service
  imports no model type. This is checked structurally, not by convention.
- **Phase 6A has no action creation CLI.** `ActionRequest`s are prepared by typed factories (a
  mail sender, an eHall submitter) or by the application API. `pw action` shows, challenges,
  approves, executes and cancels; it never manufactures an effect from arbitrary input.

## Alternatives Considered

- **Approve "the intent" instead of the payload.** The approval would cover whatever the action
  happened to say at execution time. Rejected; the fingerprint is the whole point.
- **Editable `ActionRequest`s with an approval-invalidation flag.** Every reader would have to
  remember the rule. Rejected; immutability makes the invariant structural.
- **Store the challenge token so it can be re-displayed.** A database read would then be enough to
  authorise a side effect. Rejected; only the hash is stored.
- **Reusable approvals ("approve this action type once a day").** One decision would authorise
  repetition the user never saw. Rejected.
- **Auto-retry a `FAILED` or `UNKNOWN` execution.** `FAILED` may still have partially applied and
  `UNKNOWN` may have fully applied; both would duplicate work. Rejected (ADR-0006).
- **Mark a `RUNNING` run as `FAILED` on restart.** The process dying says nothing about the
  external system. Rejected; `RUNNING` is left for reconciliation.
- **Let the interpreter or a mail handler prepare and approve its own action.** That is the
  boundary dissolving. Rejected; only a person approves.
- **A generic executor (shell, HTTP, browser) plus a policy layer.** A capability that can do
  anything cannot be audited by reading its call site. Rejected; capabilities are implemented one
  at a time or not at all.
- **Delete expired approvals instead of superseding them.** The audit trail would lose the fact
  that a decision was once made. Rejected.
- **Enforce "one live approval" only in the service.** A second writer could bypass it. Rejected;
  the partial unique index is the database's half of the rule.

## Consequences

- The assistant can prepare an irreversible action and show exactly what would happen, with the
  authority to perform it held by a person and by a capability that must be written deliberately.
- The safety properties are testable end to end: exact-fingerprint binding, tamper refusal, token
  secrecy, expiry, single use, atomic start, concurrent-execution fencing and the unknown-result
  rule are all covered against real SQLite.
- Costs and limits: approval is manual friction by design; an unresolved execution can block an
  action indefinitely until a future reconciliation phase; `ActionRequest`s can only be created
  through typed APIs, so a user cannot drive an arbitrary action from the shell; and the case
  lifecycle has no reopen.
- Phase 6B (a real mail sender) inherits a boundary it must satisfy rather than a policy it must
  remember: it registers one executor for one action type, and everything else in this ADR applies
  to it unchanged.
