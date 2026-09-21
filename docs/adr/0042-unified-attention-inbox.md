# ADR-0042 — A Unified Attention Inbox

## Title

The subsystem state that already says "a human has to decide something" becomes one durable,
derived, deterministic inbox: `attention_items`, projected from tasks, deadlines, mail analyses,
plan proposals, fact candidates, waiting confirmations, unresolved external runs, notifications
and observations — reconciled periodically, deduplicated by stable identity, settled by the user
and never able to execute anything.

## Status

Accepted

## Context

By v1.2 the system knew a great deal about what needed a person: a task was overdue, a mail asked
for a reply, a weekly proposal was waiting, a fact candidate was unconfirmed, a mail send had been
prepared and not approved, an SMTP attempt had come back meaningless, a watcher had noticed a
change. Each of those facts was durable and correct. Each of them lived in its own subsystem, with
its own name, its own screen and its own vocabulary.

The result was a product that mostly responded when asked. Nothing was wrong, and nothing was
*visible*: a user had to know that deadlines live under tasks, that the pending proposal is under
planning, that the unresolved send is under mail, and that a page change is under watchers. Two
specific failures were already reproducible in dogfooding:

```text
Updated plan proposal is ready      ← an English scheduler string, in a Chinese UI
Updated plan proposal is ready      ← the same situation, once per delivery path
```

The tempting fix is a model: "look at everything the user has, decide what is important, and tell
them". That fix is wrong three times over. It makes urgency non-deterministic, so nobody can test
it or explain it. It puts personal state — mail, files, facts — into a prompt for no reason, since
the system already knows exactly which rows are pending. And it invites the next step, in which
the thing that decides what is "important" is also the thing that acts, which is precisely the
capability this project has spent seven phases refusing to build.

## Decision

Attention is durable, derived, user-facing state:

1. **Attention is durable user-facing derived state.** It survives a restart, it is backed up, and
   it is audited. It is not a view computed in the browser.
2. **Attention is not an autonomous agent.** It has no goals, no schedule of its own, no ability to
   wake up and do something.
3. **Watchers may create observations/events but never external effects.** The existing boundary is
   unchanged; an observation may reach the inbox, and nothing else about it changes.
4. **Attention projection is deterministic application logic.** `AttentionProjector` reads bounded
   source collections and writes rows. The same state always produces the same inbox.
5. **A model does not decide whether an Approval/execution occurs.** Attention can be listed,
   acknowledged and dismissed in a conversation; it can never approve, execute or submit.
6. **Duplicate subsystem notifications must collapse behind stable dedupe keys.** A `plan_ready`
   reminder and the pending proposal behind it are one situation, and the inbox says so once.
7. **Internal event names must never be shown directly to normal users.** No `plan_ready`, no
   `WAITING_CONFIRMATION`, no `ActionRequest` in a title or a summary.
8. **Attention items have a lifecycle.**
9. **The supported lifecycle is `OPEN → ACKNOWLEDGED | DISMISSED`, and `RESOLVED` from either.**
   Each state owns exactly one timestamp, enforced by a database `CHECK`.
10. **Source resolution may resolve an attention item automatically.** Completing a task, applying
    a proposal, confirming a candidate or reconciling a send closes the item with no user action.
11. **Dismissal does not mutate the underlying source object.** "Stop reminding me" is not
    "cancel the thing".
12. **A materially changed source may create or reopen a new attention generation.** Identity is
    the *situation*; a moved deadline or an edited proposal is a new generation, so an old dismissal
    cannot silently swallow a new problem.
13. **Attention may aggregate:** Task, Deadline, Mail analysis, Notification, Plan proposal, Fact
    candidate, Recurring confirmation, Conversation external review, ExecutionRun UNKNOWN and
    Watcher/manual observation.
14. **Attention does not duplicate source data unnecessarily.** A row carries what a human needs to
    recognise the situation, not a copy of the thing.
15. **Sensitive mail bodies are not copied into attention items.** A subject line and a sender are
    metadata; the body is not.
16. **Attention is bounded and sortable.** Severity, then age, then a stable tie-break, with an
    explicit bound and an honest overflow count.
17. **Today Brief consumes normalized attention** instead of leaking raw notification strings.
18. **The Web UI may show an attention badge/drawer.**
19. **New attention can be delivered over SSE as a UI event.**
20. **SSE is not authoritative; DB state is authoritative.** A missed frame costs a refresh.
21. **Proactive surfacing does not mean proactive execution.**
22. **No browser Notification permission or OS notification is required in v1.3.**
23. **Periodic reconciliation is correctness.**
24. **Event-driven refresh may be an optimization.**
25. **The attention projector must be idempotent.** Running it twice writes one row.

### Rejected alternatives

- **A model decides urgency from all local data every minute.** Untestable, unexplainable, and it
  would put personal content into prompts that already provide no benefit.
- **A watcher invokes actions directly.** It would dissolve the approval boundary for the least
  trustworthy input in the system.
- **Every source creates a chat message.** The conversation is for talking, not for a feed; a
  hundred synthetic messages would destroy the history it depends on.
- **A raw Notification message becomes product UI.** That is the bug being fixed, not the design.
- **Browser state as attention authority.** A reload would change what the user "has to do".
- **Unbounded notification history.** An inbox nobody can read is not an inbox.
- **Duplicate reminders every refresh.** Idempotence is the point of the dedupe key.

## Consequences

- `migrations/0021_attention_items.sql` adds one table with a closed kind vocabulary, a closed
  status vocabulary, a live-identity unique index and a lifecycle `CHECK`. Nothing is copied from
  mail bodies, credentials, fact values or provider responses.
- `AttentionProjector` has no `ModelPort`, no HTTP client, no socket and no executor in its
  constructor, and an architecture test keeps it that way. It may read fact *candidates* (so it can
  say one is waiting) and never promotes, confirms or rejects one.
- The projector runs under the daemon supervisor, so a failure is logged and retried and cannot
  take the mail pipeline or the web server down with it.
- Today Brief's "需要处理" section is the normalised inbox, and its "等待确认" section keeps only what
  attention does not describe. Unresolved external outcomes keep their own calmer section, so one
  unknown send is reported once rather than twice.
- The browser gains one badge and one drawer. It can list, acknowledge and dismiss. There is no
  route that executes, approves, sends or submits, and no generic action-creation endpoint.
- `pw integrity check` gains read-only checks over attention rows: identity, lifecycle timestamps,
  a SHA-256 fingerprint, a bounded title and a source that still exists.

## Implementation notes

- Source identities and lifecycle come from the existing domain vocabulary: tasks and deadlines
  (ADR-0004, ADR-0014), mail analysis (ADR-0021), plan proposals (ADR-0015), fact candidates
  (ADR-0027), conversation external reviews (ADR-0034), execution runs (ADR-0023, ADR-0024),
  notifications and the scheduler (ADR-0016), and observations (ADR-0029).
- The conversation operations added are `attention.list`, `attention.acknowledge` and
  `attention.dismiss`. There is no `attention.execute`, and the registry has no policy that could
  express one.
- The SSE frame is `attention.updated`, added to the existing closed `SAFE_EVENT_NAMES`. It carries
  a flag, not the inbox.
- Storage follows ADR-0009: blocking `sqlite3` calls live in private `_*_sync` methods reached
  through `asyncio.to_thread`, with the connection created and closed in the executing thread.
- Migration: `migrations/0021_attention_items.sql`.
