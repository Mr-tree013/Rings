# ADR-0028 — Successful Runs Become Reviewed Non-Executing Playbooks

## Title

A definitively successful action can be remembered as a reviewed, dry-run-tested reference
blueprint — a playbook that records what worked, and that can never run it again.

## Status

Accepted

## Context

Phase 6 gave the project real external side effects, each bound to an exact approved payload.
Phase 7A gave it memory about the person: corrections, candidates and confirmed facts. The obvious
next step is memory about *work*: "that certificate application worked; keep the shape so the next
one is easier."

That step is where a careful system becomes a careless one, because the shortest version of it is
also the most dangerous.

- **a successful run is not a template.** The payload that worked contains a Message-ID, a Date, a
  draft version, a page-contract fingerprint and field values read from a page that day. Repeating
  it verbatim would resend an old message; generalising it into placeholders would mean an action
  nobody reviewed;
- **"replay" is an ambiguous word.** To one reader it means "check that this shape is still valid";
  to another it means "do it again". The first is analysis. The second is an external side effect
  with no approval, which is exactly what Phases 6A–6C exist to prevent;
- **reuse is attractive enough to grow.** Once a playbook exists, "just run it" is one command
  away, and "just fill in the recipient" is one parameter away. Both would rebuild, in the learning
  layer, the capability the approval layer deliberately withholds;
- **a pass is easy to over-read.** A dry run that re-parses a payload says something narrow; a
  reviewer who reads it as "this will work today" has been misled by a check that never touched the
  network;
- **provenance is what makes a library of workflows trustworthy.** A playbook has to point back at
  the exact action and the exact run that earned it, or it is folklore.

Phase 7B therefore builds a *library of evidence*, not a macro recorder. A candidate names one
success; a dry run re-reads the approved payload with today's code; promotion writes a reference.
Nothing in the phase can create an `ActionRequest`, an `Approval`, an `ApprovalChallenge` or an
`ExecutionRun`.

## Decision

1. A `PlaybookCandidate` may originate only from a definitively `SUCCEEDED` `ExecutionRun`.
2. Candidate creation is explicit user action; successful execution never creates a candidate
   automatically.
3. The source `ActionRequest` must be `EXECUTED` and its payload fingerprint must revalidate.
4. The source `ExecutionRun` and `ActionRequest` form durable provenance.
5. `UNKNOWN`, `RUNNING` and `FAILED` executions can never become `PlaybookCandidate`s.
6. A `PlaybookCandidate` is not a `Playbook`.
7. Candidate review is human-driven.
8. Replay testing in Phase 7B is strictly side-effect-free dry-run validation.
9. Replay testing never calls `ActionExecutor.execute()`.
10. Replay testing never creates `ActionRequest`, `Approval`, `ApprovalChallenge` or `ExecutionRun`.
11. Replay testing never performs SMTP, IMAP, eHall, browser, HTTP or filesystem side effects.
12. A replay validator is capability-specific and explicitly registered.
13. V1 production replay validators exist only for `mail.send` and `ehall.submit-certificate`.
14. Replay validation verifies that the exact historical approved payload is still structurally
    understood by current code.
15. A replay `PASS` does not mean the external action would still succeed today.
16. Runtime credentials, session state and browser availability are not required for a dry-run
    `PASS`.
17. Promotion to `Playbook` requires an explicit human command.
18. Promotion requires a `PASS` made with the current replay-contract version and the exact
    candidate source fingerprint.
19. No model may create, test, promote, reject or retire a `Playbook`.
20. Background workers cannot create or promote `Playbook`s.
21. A `Playbook` preserves source provenance and review/test history.
22. Phase 7B `Playbook`s are non-executing reference blueprints.
23. Phase 7B does not instantiate `Playbook`s into new `ActionRequest`s.
24. Phase 7B does not parameterize or generalize payload values automatically.
25. A `Playbook` never grants `Approval` and never bypasses `Approval` requirements.
26. Rejected candidates remain durable audit history.
27. Retired `Playbook`s remain durable audit history.
28. There is no automatic promotion from candidate to `Playbook`.

Additional frozen details:

- **The candidate is a pointer, not a copy.** `playbook_candidates` stores a name, a note, the
  source action id, the source run id, the action type and the source fingerprint. It stores no
  mail body, no eHall field value, no credential and no payload: the immutable `ActionRequest` is
  already the durable snapshot, and duplicating it would create a second thing to keep honest.
- **One success seeds one candidate.** `UNIQUE(source_action_id)` is deliberate and slightly
  unfriendly: rejecting a candidate is a decision, and the answer to "I want a better name" is not
  to run the same execution again. Candidate metadata is immutable in V1 for the same reason —
  what a person approves is what they typed, at the moment they typed it.
- **Eligibility is checked, not assumed.** Creation reloads the action, requires
  `ActionRequestStatus.EXECUTED`, re-hashes the stored payload, requires a `SUCCEEDED` run that
  belongs to it, and requires a registered validator for its type. A `FAILED`, `UNKNOWN`, `RUNNING`
  or still-`PREPARED` action is refused with `PlaybookSourceNotEligible`; an action type with no
  validator is refused with `PlaybookSourceUnsupported`, so every pending candidate has a test it
  could pass.
- **The replay validator is a separate protocol.** `PlaybookReplayValidator` has exactly
  `action_type`, `contract_version` and a pure `validate(action)`. It is not an `ActionExecutor`
  and cannot be one: an executor is called after an approval has been consumed and may change the
  world; a validator is called with no approval, must not touch a network, a browser, SMTP, IMAP or
  the filesystem, and changes nothing. Two protocols keyed by `ActionType` are clearer than one
  protocol that would let "test this" and "do this" be confused.
- **Two validators, registered by name.** `mail.send` re-runs `MailSendPayload.from_payload`, which
  checks the schema version, the addresses, the Message-ID, the Date and the reply-header shape.
  `ehall.submit-certificate` re-runs the typed certificate parser, which checks the pipeline and
  schema versions, the page-contract fingerprint shape, the service identity, the field structure
  and the fixed consequence text. There is no dynamic import, no plugin discovery and no generic
  executor: `PlaybookReplayRegistry` holds the two, and an unregistered type has no replay path at
  all.
- **A dry run states its own narrowness.** The mail validator reads no credential and opens no
  socket; the eHall validator opens no browser and inspects no page. So a `PASS` means "current
  code still understands this exact approved payload", and this ADR says out loud what it does not
  mean: credentials may have expired, the university page may have changed, the recipient may be
  wrong, and repeating the action may be a bad idea. A dry run also passes with no SMTP password
  configured and with the eHall pipeline disabled, because configuration is not what it measures.
- **The audit row carries identities, not content.** `playbook_replay_tests` stores the candidate,
  the action type, the registered contract version, an `input_fingerprint` over
  `(candidate_id, source_action_id, source_action_fingerprint, source_action_type,
  validator_action_type, contract_version)`, the status and a tuple of bounded issue codes. It
  never stores a payload, a body, a field value, an exception repr or free text; the domain refuses
  any code outside `{payload-invalid, schema-version-unsupported, action-type-mismatch}`, and the
  table refuses a passing row that carries codes or a failing row that carries none.
- **A dry run cannot be confused with a defect.** A payload today's parser refuses is a `FAILED`
  test with a bounded code. Corruption is not: a payload that no longer matches its fingerprint, a
  source run that is missing or no longer a success, or a candidate whose recorded fingerprint has
  drifted all raise `PlaybookSourceIntegrityError` (or the repository's own refusal) and record
  nothing. An unexpected programming error propagates.
- **Promotion is one transaction and one human decision.** `promote_candidate` re-checks the
  candidate is still pending, finds the newest `PASSED` test whose contract version and input
  fingerprint match the current validator and the current candidate snapshot, inserts the playbook
  and records the transition — all inside one `BEGIN IMMEDIATE`. With no qualifying pass it raises
  `PlaybookCandidateNotTested`; there is no `--force`, no `--skip-test` and no `--auto`.
  Contract-version bumping is how parser changes invalidate old passes: a pass under version 1 does
  not qualify while the registered validator declares 2, and old playbooks keep the version they
  were promoted under rather than being silently re-tested or retired.
- **A playbook is immutable provenance.** Name, note, action type, source action, source run,
  source fingerprint, contract version and the promotion test are fixed at creation; the only
  transition is `ACTIVE → RETIRED`, and retiring keeps the row, the candidate and the tests.
- **There is no execution path and no parameterisation.** The source contains no
  `PlaybookExecutor`, no `PlaybookRunner`, no `run`/`execute`/`apply`/`instantiate` command, no
  `to_action_request`, and no `${...}` or `{{...}}` template machinery; an architecture test fails
  if any of those appear. A playbook cannot create an `ActionRequest`, an `Approval` or an
  `ExecutionRun`, cannot bypass approval, and is reachable only from `pw playbook` commands.
- **The two learning tracks stay separate.** Corrections feed `FactCandidate`/`ConfirmedFact`;
  successful executions feed `PlaybookCandidate`/`PlaybookReplayTest`/`Playbook`. They are
  different tables, different services and different commands, because "a fact about me" and "a
  shape of work that once succeeded" have different trust stories.
- **The phone surface does not grow.** There is no `/api/playbooks` route and no mobile promotion:
  human review in this phase is a terminal command with the content in front of it.
- **No model, anywhere on the path.** The playbook path imports no `ModelPort`, no
  `StructuredModel` and no provider adapter, and no background module can name a playbook service:
  a successful run is history, and turning history into a lesson is a decision a person makes.

## Alternatives Considered

- **Automatically create a candidate after every success.** Every successful send would fill the
  review queue with near-duplicates, and the queue would become noise nobody reads. Rejected; the
  user points at the run that mattered.
- **Let a model summarise the payload into a reusable recipe.** The summary would be the thing a
  reviewer approves, and it would be an interpretation of an action rather than the action.
  Rejected.
- **Replay by actually invoking SMTP or eHall.** It would resend mail or resubmit a form with no
  approval, which is the one thing this project never does. Rejected outright.
- **Promote after one successful production action, with no dry run.** A success proves the past,
  not that the current code still understands the payload; after a parser change the stored shape
  could be stale. Rejected; a current passing dry run is required.
- **Reuse the historical approval for the playbook.** An approval authorises one execution of one
  fingerprint, and it was consumed. A playbook grants nothing at all. Rejected.
- **Copy the historical payload into a fresh `ActionRequest` at promotion time.** It would create
  an action nobody prepared and nobody approved, from a document that may be months old. Rejected.
- **Infer template parameters (`${recipient}`, `${date}`) so the playbook becomes reusable.**
  Automatic generalisation is a guess about what varies, and it produces an action that no human
  reviewed. Rejected and deferred; this ADR says so explicitly.
- **Auto-promote candidates that pass.** A pass is evidence, not consent. Rejected; promotion is a
  command.
- **Delete rejected candidates or retired playbooks to keep the store tidy.** The refusals and the
  retirements are the audit trail. Rejected; neither delete path exists.
- **One generic replay executor that re-dispatches by action type.** It would be the generic
  browser/HTTP capability this project has refused since Phase 6A, wearing a new name. Rejected;
  two validators, registered by hand.
- **Store the payload, or a redacted copy, beside the replay test for easier review.** The
  immutable action already holds it and `pw action show` prints it; a second copy is a second place
  for a mail body to leak. Rejected.
- **Let the phone promote a playbook.** Review needs the exact payload and the source run in front
  of a person; the mobile surface stays where Phase 6D left it. Rejected for this phase.

## Consequences

- The project can learn from what worked without giving a machine the power to repeat it. A person
  names the run, a pure validator re-reads the approved payload, and a person promotes the result.
- The library is auditable by construction: every playbook points at a candidate, a source action,
  a source run and the dry run that qualified it; every dry run records the contract version and a
  fingerprint of the exact snapshot it tested, and no row contains a payload or a secret.
- The phase is honest about its own value. Because a playbook cannot execute and cannot be
  parameterised, its usefulness today is that the reasoning behind a successful errand is written
  down, reviewable and testable — the material a later phase would need before it could safely
  rebuild an action from it.
- Costs and limits: the review queue is human work and there is no bulk operation; a candidate
  cannot be renamed, so a rejected candidate is retired rather than fixed; a parser change
  invalidates existing passes until they are run again; and a `PASS` deliberately says nothing
  about whether the outside world would accept the action today.
