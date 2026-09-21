# ADR-0041 — Tree Local Chat UI, a Durable Turn Queue and a Non-Authoritative Event Stream

## Title

The browser becomes the preferred daily surface for Tree, as a thin *interaction adapter* over the
existing conversation runtime: accepted input is queued durably before anything runs, one thread
runs one request at a time, progress is reported as a closed set of coarse application stages over
an ephemeral SSE channel, and structured confirmation cards settle exact durable targets through
the deterministic controllers the terminal already uses.

## Status

Accepted

## Context

Phase 10 made Tree a real conversational runtime and v1.1 made it a daily one — in a terminal. The
terminal is honest and fast, and it is the wrong shape for the interaction people actually want to
have with a personal assistant: reading a long answer on a phone, checking what Tree is doing,
seeing the exact bytes of an email before sending it, or tapping "确认记住" instead of typing a
phrase from a vocabulary list.

The obvious way to add a web surface is also the way that destroys everything v1 built. The
dangerous version of this phase is:

```text
browser ──► a second application layer ──► SQL, SMTP, or a generic model tool loop
```

That would put an authorization decision in JavaScript, duplicate the conversation runtime in a
second language, and turn "what will this click do" into something nobody can review. The whole
point of `ActionRequest` → exact fingerprint → human `Approval` → `ExecutionRun`, of a closed
capability registry and of a model with no tools, is that there is exactly one place where an
effect can begin. A browser is not allowed to become a second one.

There is also a new reliability problem, and it is the one a chat UI makes unavoidable. A
conversation turn is expensive (a model call) and partly irreversible (a local write). A browser
cannot hold that turn: it reloads, it has two tabs, it retries a POST after a timeout, and it
disappears while a background worker keeps going. So the interaction needs a *durable* queue with
an explicit crash story, and the UI needs an honest one for "stop".

## Decision

### The surface

1. Tree Web Chat is an interaction adapter over the existing conversation runtime.
2. Browser code has no direct domain, repository or executor access.
3. The terminal `rings` remains supported.
4. `pw` remains advanced, admin and debug tooling.
5. The web UI and the terminal Tree share the same application services.
6. Browser conversation messages are durable in the existing conversation database.
7. Accepted browser input is queued durably before execution.
8. At most one conversation request executes per thread at a time.
9. Additional user messages may queue while one request is active.
10. A queued request survives page reload and process restart.
11. A request that had already begun processing is never blindly replayed after a process restart.
12. POST retries are idempotent through a browser-generated `client_request_id`.
13. SSE is an ephemeral UI notification mechanism, not authoritative state.
14. Durable database state is authoritative.
15. On SSE reconnect or reload, the browser obtains a fresh snapshot.
16. No durable event-stream log is required.

### Progress and confirmation

17. Browser-visible progress consists only of safe, coarse application stages.
18. Progress never exposes model chain-of-thought, hidden reasoning, raw prompts, provider JSON or
    schema internals.
19. Structured confirmation cards are deterministically reconstructed from durable state.
20. Confirmation buttons bypass `ModelPort` and settle the exact pending item through the existing
    deterministic controllers.
21. Clicking "确认发送" remains an explicit human authorization event.
22. No `ApprovalChallenge` value is ever exposed to browser JavaScript.
23. Mail confirmation still revalidates the exact `ActionRequest` fingerprint.
24. Stale confirmation cards fail closed and force a refresh.
25. Browser text rendering treats all user, model, mail and contact content as untrusted text.
26. Conversation content is rendered using `textContent` or equivalent escaping, never
    unsanitized markup.

### Execution, stopping and concurrency

27. One active request per thread; multiple threads may operate independently if the existing
    SQLite and application concurrency permit it.
28. Cancellation is only offered when it is actually safe.
29. A queued request may always be cancelled.
30. A request during model understanding may be cancelled before any mutation is applied.
31. Once local mutation or external execution enters an unsafe cancellation boundary, Stop must not
    claim cancellation succeeded.
32. External execution already in progress cannot be "undone" by UI Stop.
33. SMTP `UNKNOWN` is never transformed into cancellation or failure.
34. Browser history and thread history are durable only through the existing conversation database.
35. No separate browser chat database exists.

### Security, assets and compatibility

36. Existing LAN authentication and CSRF protections are reused.
37. LAN access never receives an anonymous security bypass.
38. No wildcard CORS is introduced.
39. Web UI static assets are local and packaged with the distribution.
40. No CDN or remote JavaScript dependency is required.
41. Vanilla HTML/CSS/JavaScript is sufficient for v1.2.
42. No React, Vue, Vite or npm build pipeline is introduced.
43. SSE is preferred over WebSocket for the v1.2 event channel.
44. Today Brief remains deterministic and read-only.
45. `/chat` does not automatically execute Today Brief as a conversation mutation.
46. The UI may display a deterministic home summary or quick action.
47. Confirmation cards are convenience and safety UI, not new authority.
48. Text-based confirmations remain supported for terminal compatibility.
49. The browser and the terminal must produce equivalent durable results.
50. `rings --web` may open the UI, but the existing default terminal behaviour remains compatible
    in v1.2.0.

## Frozen details

**The queue is a table, not a timer.** `migrations/0020_conversation_requests.sql` stores one row
per accepted message: `thread_id`, `client_request_id`, nullable `input_text`, a bounded `status`
(`QUEUED`/`PROCESSING`/`COMPLETED`/`FAILED`/`CANCELLED`/`INTERRUPTED`), a bounded coarse `stage`,
nullable `turn_id`, `error_code`, `cancel_requested_at`, and the three timestamps.
`UNIQUE(thread_id, client_request_id)` is the idempotency key, and a partial unique index
(`WHERE status = 'processing'`) is what makes "one active request per thread" a property of the
database rather than of a worker's discipline. `conversation_turns` gains a nullable `request_id`
with a partial unique index, so a turn belongs to at most one request and a crash has something
exact to correlate. The row stores no prompt, no provider response, no reasoning and no challenge;
`input_text` is the user's own words, kept only while the accepted intent must survive a restart,
and cleared in the same statement that makes the request terminal *once the durable message
exists*.

**Claiming is one transaction.** `claim_next` selects the oldest `QUEUED` row of a thread whose
thread has no `PROCESSING` row, and updates it to `PROCESSING` inside the same `BEGIN IMMEDIATE`.
Two workers cannot both claim it, and a thread cannot overlap two turns. Different threads claim
independently.

**Crash recovery is fail-closed.** At startup, `APPLYING` operations become `UNKNOWN_LOCAL` (their
existing behaviour), and every `PROCESSING` request is resolved from its *linked turn*: a terminal
turn is mirrored, a turn parked `WAITING_CONFIRMATION` is accepted as finished (the pending offer
is itself durable), and anything else — including a claim with no turn evidence at all — becomes
`INTERRUPTED`. Only `QUEUED` work is picked up again, because it is the only state in which nothing
has happened yet.

**Progress is a closed vocabulary, owned by the application.** `ConversationProgressStage` is
`QUEUED`, `UNDERSTANDING`, `READING_LOCAL_STATE`, `PLANNING`, `QUERYING_KNOWLEDGE`,
`PREPARING_MAIL`, `UPDATING_LOCAL_STATE`, `WAITING_CONFIRMATION`, `EXTERNAL_EXECUTION`,
`FINALIZING`. The stage is derived from the validated plan's operation types (`stage_for_plan`),
never from the model's words, and the database rejects any other value.

**Stop is a promise, not a hope.** Cancellation is cooperative, and it is offered only while the
request is `QUEUED` or `UNDERSTANDING`. The runtime checks a cancellation token before the model
call, after it returns, and before the plan is acted on; a stop that arrives during the model call
abandons that call's answer instead of waiting for it to be applied. Every stage the application
reports after understanding is recorded *before* it is entered, and each of those transitions is
followed by a checkpoint, so "not cancellable" is decided before any mutation, never after.
Everything past that point answers `409 CANNOT_CANCEL_SAFELY`, because a partially applied plan or a
send already in flight is not something a button can undo.

**SSE is a notification, and its queues are bounded.** `ConversationEventBroker` fans one event out
to the subscribers of one thread: `request.queued`, `request.started`, `request.stage`,
`request.cancel_requested`, `request.cancelled`, `request.completed`, `request.failed`,
`request.interrupted`, `assistant.message`, `confirmation.required`, `confirmation.updated`,
`thread.updated`. A subscriber that falls more than 64 events behind has its queue drained and is
sent `resync.required`, after which the connection closes; the browser reconnects and reloads a
snapshot. Nothing is persisted, nothing is replayed, and a process restart may reset the stream —
which is not a correctness problem, because correctness was never in the stream.

**Confirmation cards are presentations of durable state, with a version.** A card carries an `id`
of the form `kind:target` where the kind is one of `MAIL_SEND`, `PLAN_APPLY`, `RECURRING_SCHEDULE`
and `FACT_CONFIRMATION`, and an `expected_revision` derived from the rows behind it (the review's
action fingerprint and status, the operation's fingerprint and status, the candidate's identity and
status, the group's operation fingerprints). The settle path rebuilds the card from durable state
and refuses a click whose revision no longer matches, with `409 STALE_CONFIRMATION`. The dispatch
is a closed mapping over that enum: no reflection, no dynamic import, no "approve this action".

**One settlement per decision, shared with the terminal.** A card click records the exact phrase a
terminal user would have typed (`确认发送`, `确认记住`, `不要记`, `可以`, `取消`) as the durable user
message, then calls the *same* internal settlement the phrase path reaches: `_settle_review` for a
mail send, `_answer_fact_confirmation` for a fact, `_answer_groups` for a plan or a recurring
group. There is one implementation of each decision, and the browser and the terminal produce
equivalent durable results.

## Rejected alternatives

- **The browser calling repositories or SQL directly.** It would put an authorization decision in a
  language with no access to the invariants, and make every future schema change a frontend change.
- **The browser calling SMTP (or any executor).** The one thing v1 promises is that an external
  effect begins at exactly one place, behind an exact fingerprint and a human approval.
- **Raw `ActionRequest` approval from JavaScript.** Approving is not "the UI said yes"; it is a
  challenge, a consumed secret and a fingerprint re-check.
- **Exposing `ApprovalChallenge`.** The plaintext exists for one server-side call; a token in a page
  is a token in a screenshot, a log and a browser history.
- **Model-generated progress narration.** It would be unverifiable, it would drift from what the
  code is doing, and it is chain-of-thought by another name.
- **Streaming chain-of-thought.** It is not product state, it is not the user's, and it is not
  something this project stores or shows.
- **A durable SSE event log as the source of truth.** It would create a second history to
  reconcile, with its own retention and replay semantics, in exchange for nothing: the snapshot
  already is the truth.
- **Blind replay after a restart.** A replayed turn can be a second model call, a second task or a
  second email. `INTERRUPTED` is the honest outcome.
- **A WebSocket-first design.** The channel carries one-way notifications about state that lives
  elsewhere; SSE is the smaller mechanism, and it reconnects with the browser's own semantics.
- **A React/Vite frontend build stack.** It adds a toolchain, a dependency tree and an asset
  pipeline to serve three files, and it makes "what does this page do" a build-output question.
- **Wildcard CORS.** Same-origin is the security model; a wildcard would let any page in the
  browser drive a paired host.
- **Anonymous LAN access.** "It is only local" is not an authentication story, and the existing
  pairing plus CSRF pair already exists.
- **Raw HTML rendering of messages.** A mail subject is not markup, and a task title is not
  permission to run a script.
- **UI-generated generic agent tools.** The capability set is closed in code; a browser that can
  name a new capability is a browser that can escalate.
- **Changing the `rings` default behaviour without dogfooding.** The terminal is the fallback for
  SSH, for development and for a host without a browser; `--web` is additive.

## Consequences

- `rings` remains the terminal conversation and `pw` remains the admin surface; both build the same
  `ConversationService` the web adapter uses, so there is no second runtime to keep honest.
- The queue lives in the runtime database, so it is inside the existing backup, restore and
  integrity surfaces without a new archive format. A queued request survives a backup and comes
  back claimable; a `PROCESSING` row comes back as durable intent that the next start resolves
  rather than replays.
- `pw integrity check` gains read-only checks over the queue: a request belongs to a real thread, a
  correlated turn belongs to the same thread, no thread has two active requests, and no terminal
  row still claims to be live. Message text is never inspected.
- The browser surface is gated on the host being able to *hold* a conversation: a host with no
  `[model]` section keeps the v1.1 control plane and registers no chat routes at all, which is more
  honest than a shell that cannot answer.
- The event stream is deliberately disposable, so it is not backed up, not integrity-checked and
  not replayed — a property that has to keep being true as the UI grows.

## Implementation notes

- Conversation runtime and its crash fence: ADR-0033, ADR-0035. External review: ADR-0034.
  Facts: ADR-0038. Today Brief: ADR-0039. Terminal line editing: ADR-0040.
- Approval, execution and SMTP semantics are untouched: ADR-0023, ADR-0024.
- LAN control plane, pairing, session and CSRF: ADR-0026.
- Runtime database, backup, restore and integrity: ADR-0031, ADR-0032.
- Migration: `migrations/0020_conversation_requests.sql`.
