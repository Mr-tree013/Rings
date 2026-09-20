# ADR-0033 — Tree Conversation Runtime and Typed Local Operations

## Title

Tree Conversation is the primary human-facing interaction direction for Rings after v1. A
conversation turn is interpreted into one closed-schema plan of typed local operations, and a
deterministic Conversation Runtime — never the model — decides which of those operations is
allowed, whether it needs a confirmation, and who executes it.

## Status

Accepted

## Context

v1 shipped the right safety model and the wrong daily interface. Every real capability is reachable
only through `pw`: 37 top-level commands and 79 subcommands, shaped like the domain
(`Task`/`Case`/`ActionRequest`/`Approval`/`ExecutionRun`, proposals and revisions) rather than like
the user's day. Sending one reply means knowing six commands in the right order. Natural language
exists, but only as three isolated flows: `pw interpret` (one command preview, never executed),
`pw ask` (a grounded answer) and `pw ingest text` (an inbound observation). None of them can carry
a conversation, and none of them can do the local work the user just asked for.

That gap is the whole problem. The user speaks in goals ("明天下午三点提醒我交报告"); the system
answers in implementation commands. Phase 10A closes it for daily local planning and knowledge, and
deliberately nothing else.

The risk does not change because the interface is nicer to type. A conversation is a place where
the user's *words* arrive next to the user's *authority*, so an interpretation error becomes an
action error. Every existing boundary therefore survives Phase 10A unchanged: the model still has
no tools, no filesystem, no database and no HTTP; external effects still go through
`ActionRequest` → human `Approval` → `ExecutionRun`; and a conversation cannot create an approval.

ADR-0018 §27–28 anticipated exactly this step: "a future Agent Runtime may consume the same typed
`CommandDraft`s through a separately designed execution boundary", and "no tool loop, retrieval
augmentation, conversation memory or autonomous planning is introduced" in that phase. Phase 10A is
that next phase, and it keeps the second half of that sentence: the typed plan replaces the tool
loop, and the conversation runtime is the new execution boundary.

## Decision

1. Tree Conversation is the primary human-facing interaction direction for Rings after v1.
2. Existing `pw` commands remain supported as advanced/admin interfaces, unchanged.
3. The model never receives direct application-service, database, filesystem, HTTP, shell, browser,
   Approval or executor access.
4. The model produces only closed-schema typed conversation plans.
5. A Conversation Runtime, not the model, resolves and executes allowed operations.
6. Operations come from an explicit ConversationCapabilityRegistry.
7. There is no generic tool loop.
8. One model response cannot invent new capabilities.
9. Conversation execution never uses reflection to expose arbitrary application methods.
10. Read operations may execute immediately.
11. Explicit, unambiguous local commitment writes may execute immediately.
12. Ambiguous local mutations ask a clarification question instead.
13. Bulk or structurally significant local mutations, such as applying a weekly plan, require an
    explicit conversational confirmation.
14. External effects are not added to the conversation capability set in Phase 10A.
15. Conversation cannot create an `Approval`.
16. Conversation cannot execute an `ActionRequest`.
17. Conversation cannot send SMTP mail.
18. Conversation cannot submit eHall forms.
19. Existing `ActionRequest` → `Approval` → `ExecutionRun` semantics remain untouched.
20. Natural-language relative times are resolved only against an explicit configured planning
    timezone.
21. If a relative time requires a timezone and no planning timezone exists, Tree asks the user
    instead of guessing the host timezone.
22. Conversation history is durable but is not automatically converted into `ConfirmedFact`s.
23. Conversation history is not a replacement for Roots or Facts.
24. Knowledge is queried only through the existing grounded-answer/search boundary.
25. No arbitrary private knowledge, mail body or fact set is automatically attached to every model
    request.
26. Recent conversation context is bounded.
27. Operation results are durable and auditable.
28. A partially executed conversation write is never automatically retried after an ambiguous
    process crash.
29. Conversation logs do not contain raw user message contents by default.
30. The first v1.1 conversation capability set is intentionally limited to daily local planning and
    knowledge workflows.

Freezing details:

- **The plan is data.** A model turn produces one `ConversationPlan` with `mode` in
  `DIRECT_REPLY` / `CLARIFICATION` / `OPERATIONS`; every operation carries a closed `type` and
  typed arguments. There is no `tool_name`, `function_name`, `method`, `command` or `url` field
  anywhere in the schema, so "call something I was not offered" is a schema violation.
- **The capability set is closed and code-owned.** `ConversationCapabilityRegistry` maps an
  operation type to a validator, a handler and a `ConfirmationPolicy`
  (`READ` / `LOCAL_WRITE` / `CONFIRM_LOCAL`). Only `plan.apply_proposal` is `CONFIRM_LOCAL` in
  Phase 10A, and no `EXTERNAL_WRITE` policy exists at all.
- **At most five operations per turn.** More work than that is a conversation, not one message.
- **Confirmation is deterministic.** A pending confirmation is bound to the thread, the operation
  id, the operation fingerprint and an expiry; the accepted phrases are a fixed vocabulary and no
  model call is involved. One pending confirmation is answered directly; more than one is a
  question back to the user, never a guess.
- **Crash semantics are explicit.** An operation is persisted as `APPLYING` before the mutating
  service is called and becomes `APPLIED` (with `result_ref`) only after it returned. A process
  that dies inside `APPLYING` leaves `UNKNOWN_LOCAL`, which is never replayed automatically.
- **The runtime is an orchestration layer.** Conversation handlers call the existing
  `TaskService`, `CalendarService`, `WorkService`, `PlannerService`, `GroundedAnswerService` and
  scheduler repository. No SQL, no duplicated task logic and no parallel planning model is written
  in the conversation layer.

## Rejected alternatives

- **Giving `ModelPort` arbitrary tools.** It moves the authorization decision into the least
  reliable component, and it is unfalsifiable afterwards: a tool call that happened is a side
  effect that already happened.
- **A LangChain/LangGraph-style agent loop.** An unbounded loop cannot be reviewed, cannot be
  bounded by a fingerprint, and makes "what will this turn do" unknowable before it runs.
- **A shell tool.** The project's defining property is that there is no shell.
- **A filesystem tool.** Roots are indexed deliberately; conversation is not a file manager.
- **A generic HTTP or browser tool.** Watchers observe fixed, configured, public pages only, and
  eHall is a single approved workflow.
- **Exposing the whole CLI as model tools.** 100+ commands with domain-shaped arguments is the
  problem this phase exists to fix, and it hands the model the entire surface at once.
- **Letting the model call `ApprovalService`.** A conversation would become a way to approve, which
  is the one thing v1 promises no automation can do.
- **Automatically treating chat statements as `ConfirmedFact`s.** "记住我的办公室在仙林" is a
  statement in a conversation, not a human confirmation of a durable fact.
- **Guessing relative times from the machine timezone.** A WSL host, a CI runner and a laptop in
  another country disagree; the user's planning timezone is the only defensible basis.
- **Replacing the existing application/service architecture.** The conversation layer is an
  adapter over the core; the core stays the authority for every rule it already owns.

## Consequences

- `rings` becomes the branded entry point and `pw chat` the power-user equivalent; both build the
  same `ConversationService`, so there is no second conversational implementation to keep honest.
- Conversation rows live in the runtime database, so they are inside the existing backup, restore
  and integrity surfaces without a new archive format.
- The capability registry is the single place where the question "what may a conversation do?"
  is answered, and a test pins it to the Phase 10A vocabulary.
- Phase 10A deliberately leaves conversational external actions (mail send, eHall submission) for
  a later phase with its own ADR, because those need an approval flow that is designed for a
  conversation rather than borrowed from the CLI.

## Implementation notes

- Interpretation precedent and the typed-draft boundary: ADR-0018.
- Grounded answers: ADR-0019. Model boundary: ADR-0017.
- Approval and execution boundary (untouched): ADR-0023. Facts: ADR-0027. Playbooks: ADR-0028.
- Runtime, backup and release contract: ADR-0032.
