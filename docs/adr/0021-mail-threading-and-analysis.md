# ADR-0021 — Deterministic Mail Threading and Durable Model Analysis

## Title

Stored mail is threaded by deterministic code and classified by a bounded, untrusted-context model
call whose result is a durable, fingerprinted analysis of candidates.

## Status

Accepted

## Context

Phase 5A made mail durable: every synchronized message is a stable `MailMessage` with a location,
headers and a body, bridged to exactly one `InboundEvent`. Phase 5B is the first time this project
*interprets* what somebody else wrote, and the interpretation has to survive two very different
kinds of uncertainty:

- **identity is not a matter of opinion.** Which message is a reply to which is decided by
  `Message-ID`, `In-Reply-To` and `References` — headers that are missing, duplicated and forged in
  the real world. A model asked to guess a parent would produce an answer that changes with the
  prompt; a thread is a fact about headers and must be decided by code.
- **classification is a matter of judgement, and judgement is not free.** A model call costs money
  and can fail. Event processing is at-least-once (ADR-0010), so a retried event must not pay for an
  analysis it already has; a provider outage must not lose the mail it was meant to analyze; and a
  model must never be able to change anything but its own analysis record.

The existing boundaries already say most of the rest: `ModelPort` has no tools (ADR-0017),
untrusted input is data (ADR-0019), and the durable event pipeline owns retry and dead-lettering
(ADR-0010). Phase 5B is where those rules meet a message written by a stranger.

## Decision

1. Mail threading is deterministic code, not model reasoning.
2. `Message-ID`, `In-Reply-To` and `References` are evidence, not globally trusted identities.
3. Duplicate Message-ID values must never cause an arbitrary thread choice.
4. Mail body, subject and headers are untrusted data, never instructions.
5. Model analysis cannot mutate Task, Case, Calendar, Scheduler or Mail state beyond its own
   analysis record.
6. Mail analysis is durable and idempotent, so an `EventWorker` retry does not repeatedly pay for an
   already completed analysis.
7. A model failure must never cause the original `MailMessage` to disappear.
8. Action, deadline and event-time extraction produces candidates only.
9. A deadline candidate is distinct from an event-start candidate.
10. Phase 5B does not create Tasks or Cases.
11. Phase 5B does not draft or send replies.
12. `EventWorker` starts only when a real mail handler can run; no no-op handler exists.

Additional frozen details:

- **Parent resolution order.** `In-Reply-To` first, then `References` from the most recent entry
  backwards (the list is written oldest-first). Only a Message-ID that matches **exactly one**
  stored message **in the same account** becomes a parent.
- **The four link statuses.** `root` (no reference headers at all), `linked` (one unambiguous
  parent), `unresolved` (reference headers exist but nothing matched, for example a reply whose
  parent was never stored), `ambiguous` (a reference header matches more than one stored message).
  An `ambiguous` result stops resolution rather than falling back to a grandparent: silently
  attaching the message to a different conversation is worse than attaching it to none.
- **One membership, decided once.** `mail_thread_members.message_id` is the primary key, so a
  message belongs to exactly one thread. `ensure_thread` returns the stored decision when there is
  one, which is what makes a later-arriving parent a new root instead of a rewrite of history.
- **Bounded and cycle-safe.** The walk carries a visited set and a depth limit (32); a message that
  references its own descendant terminates, with the reason recorded as link evidence.
- **Bounded, untrusted context.** A request carries the current message (id, subject, from,
  `sent_at`, body) plus at most 5 earlier messages of the same thread, at most 3000 characters per
  body and 12000 in total, with a deterministic truncation flag. `To`/`Cc`, attachment bytes and
  hashes, the raw `.eml`, its storage key, mailbox cursors, credentials and all Task, Calendar,
  Scheduler, Notification and Knowledge state are absent by construction.
- **No clock in the context.** Relative expressions are anchored to the mail's own `Date`, which is
  the anchor its author used. This also means a retry an hour later produces byte-identical request
  content, so a fingerprint over the inputs describes what the model actually saw.
- **Closed output schema.** `category` (four values), `requires_reply`, `summary` (≤800) and up to
  10 `action_candidates`, each with `text`, `temporal_kind` (none/deadline/event_start/other),
  `time_text` and `interpreted_at`. No extra properties anywhere.
- **Semantic validation after schema validation.** `interpreted_at` must parse and must carry an
  explicit UTC offset; a naive value is refused rather than assumed to be local time. A timed
  candidate must carry either the raw text or an instant, and an instant without a temporal kind is
  refused. A refused analysis is not stored: the event retries.
- **Input fingerprint.** Canonical JSON and SHA-256 over analyzer version, schema version, the
  message id, its content fingerprint, the ordered thread message ids and content fingerprints, and
  the planning timezone. A stored analysis is reused only when both the analyzer version and the
  fingerprint match; otherwise it is replaced atomically and keeps its original `created_at`.
- **An oversize body is never sent to a provider.** The handler stores an `unknown` analysis that
  says why, computed locally and for free.
- **Retry classification.** `ModelRateLimited`, `ModelTransientError`, `ModelUnavailable`, a
  provider protocol failure and unusable structured output are retryable under the existing bounded
  `RetryPolicy`. `ModelAuthenticationError`, `ModelBillingError`, `ModelInvalidRequest`,
  `ModelCredentialsMissing` and `ModelConfigurationError` are `PermanentEventError` and dead-letter
  immediately. Nothing retries inside the adapter.
- **A missing event link is retryable, not fatal.** `mail_event_links` is written moments after the
  event is ingested, so a mismatch usually means the mail sync bridge has not finished; the event is
  retried and the next mail round repairs the link.
- **Conditional worker startup.** The daemon starts `event-worker` only when mail accounts *and* a
  usable model configuration exist. Without a model, mail keeps being received and `RECEIVED`
  events keep accumulating; configuring a model and restarting drains the backlog.
- **One dispatcher, one entry.** `InboundEventDispatcher` maps `mail.message.received` to the mail
  handler and refuses an unknown event type permanently. There is no plugin registry and no dynamic
  import.

## Alternatives Considered

- **Let the model decide threading.** Identity would depend on a prompt, so the same mailbox could
  produce different thread graphs on different days, and a duplicate Message-ID would be resolved by
  a coin flip. Rejected; threading is code.
- **Link across accounts when a Message-ID matches.** Two mailboxes legitimately contain the same
  header, and joining them would merge two people's conversations. Rejected; matching is per account.
- **Rewrite an earlier decision when a parent arrives later.** Cheap to implement, but it makes
  thread membership change under the user, and it makes `ambiguous` unrepresentable. Rejected; the
  first decision stands and is returned on every later call.
- **Send the whole thread or the raw message.** The provider would receive the `To`/`Cc` graph,
  attachment bytes and content that has nothing to do with the question. Rejected; bounded context
  with an explicit budget.
- **Store a conversation transcript now.** There is no product decision about what a conversation
  is, and a durable transcript would outlive the design that produced it. Rejected; the durable
  record is one analysis per message.
- **Analyze with no fingerprint and just re-run on retry.** Every retried event would pay again, and
  a provider outage would multiply the cost by the retry count. Rejected; the fingerprint is the
  reason the retry is free.
- **Treat a naive instant as local time.** Guessing a timezone invents a fact the message never
  stated, and the guess would be silently wrong twice a year. Rejected; refuse and retry.
- **Dead-letter the event when the model is unavailable.** A transient provider outage would then
  destroy work permanently. Rejected; transient failures retry under the bounded policy.
- **Start an `event-worker` with no model and let it dead-letter.** This is the worst outcome: the
  backlog is consumed by failures instead of waiting for a working provider. Rejected; the worker is
  simply not started.
- **Add a no-op handler so the worker always exists.** A handler that does nothing would hide the
  fact that nothing is being processed. Rejected.
- **Store analysis in a general "documents" table.** A typed analysis is the reason the candidate
  rules can be enforced by SQLite `CHECK` constraints. Rejected.

## Consequences

- Mail becomes searchable, threaded and classified local state without any of it leaving the machine
  except as the bounded context of one analysis request.
- The boundaries are testable and tested: thread decisions against real SQLite (including duplicate
  Message-IDs, missing parents, cycles, cross-account references), the closed schema, the local
  refusal of naive instants, the reuse of an analysis across a reclaimed lease, the retry/dead-letter
  matrix, and the absence of any change to Task, Deadline, Calendar, Plan, Work, Proposal, Scheduler,
  Notification or Knowledge state.
- Costs and limits: analysis quality is bounded by the model's judgement on a bounded context;
  thread membership is not retroactively reconstructed when a parent arrives late; and a message
  whose only reference headers are ambiguous sits in a thread of its own until a later phase offers
  an explicit repair path.
- Phase 5B produces candidates, not commitments. Turning a deadline candidate into a Task or an
  event-start candidate into a Calendar entry is a separate, reviewable design (ADR-0006), and the
  analysis record is designed to be its input rather than its trigger.
