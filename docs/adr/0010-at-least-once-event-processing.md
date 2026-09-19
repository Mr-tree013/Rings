# ADR-0010 — At-Least-Once Event Processing with Leases and Fencing

## Title

Process durable events at-least-once, using database-atomic leases with fencing tokens,
deterministic retry backoff and an explicit dead-letter state.

## Status

Accepted

## Context

Phase 1A/1B made every external input a durable `InboundEvent` and added an async,
database-level deduplicated repository. What remained undefined was how events get
processed: who may work on one, what happens when the process dies mid-attempt, and what
prevents a resurrected worker from overwriting the state of the attempt that replaced it.

Two failure modes are unacceptable here. Processing an event twice by accident is
tolerable if the work is idempotent, but *silently losing* an event, or letting a stale
worker mark an event `PROCESSED` after someone else already picked it up, corrupts the
record that every later phase (mail drafts, eHall errands, archives) will trust.

## Decision

1. Event processing guarantees **at-least-once**, never exactly-once. No claim of
   exactly-once is made anywhere in the code or documentation.
2. The durable Inbox is the source of truth for processing: work is driven from
   `inbound_events`, not from in-memory queues.
3. Workers take work exclusively through an **atomic database claim**. `list_pending` +
   a later `transition` is explicitly not a claim mechanism: it can hand one event to two
   workers.
4. A claim carries a random `claim_token` used as a **fencing token**.
5. `complete_claim` and `fail_claim` must present the current claim token.
6. An expired claim may be reclaimed by another worker.
7. Once reclaimed, the previous token is dead: the old worker's completion or failure is
   rejected with `StaleEventClaim`.
8. Handlers must be idempotent, or must only create durable downstream intents that a
   later step can deduplicate — because a crash between "handler succeeded" and
   "completion recorded" means the attempt happens again.
9. `asyncio.CancelledError` is never recorded as a business failure. Cancellation
   propagates; the event stays `PROCESSING` with its lease, and lease expiry recovers it.
10. Retry uses deterministic exponential backoff (`min(base * 2^(attempts-1), max)`), with
    no jitter in this version, so scheduling is reproducible and testable.
11. Reaching the attempt budget, or raising `PermanentEventError`, moves the event to
    `DEAD_LETTERED` with `dead_lettered_at` and a bounded error summary.
12. `DEAD_LETTERED` is terminal and is never claimed again. Manual replay, if wanted
    later, is a separate design with its own approval story.

## Alternatives Considered

- **Exactly-once processing**: impossible to guarantee across a crash boundary without
  distributed transactions between SQLite and every downstream effect (SMTP, eHall). Any
  claim of exactly-once here would be a lie; the honest design is at-least-once plus
  idempotent handlers.
- **`list_pending()` then `transition()`**: simple to write and racy by construction. Two
  workers can read the same pending row and both start work; the last writer wins the
  status. Rejected as the claim mechanism (the read API is kept for inspection only).
- **`PROCESSING` without a lease**: a crash leaves the event stuck forever with no signal
  to a human, and no automatic recovery. Rejected.
- **Immediate in-process retry**: keeps the worker busy on one poisoned event, hides the
  failure from the inbox, and turns a bad handler into an outage. Rejected in favour of
  scheduled retries visible in the database.
- **Unlimited retries**: a permanently failing event would retry forever, burning quota
  and masking the problem. Rejected in favour of a bounded attempt budget plus dead
  letter, which is a state a human can inspect.
- **Jittered backoff**: the usual reason is many competing workers; here there is one
  user and one process, and jitter would make the timing untestable. Deferred until there
  is evidence it is needed.

## Consequences

- Every event has an auditable trail: attempts, claim owner, lease window, last error and
  (if terminal) dead-letter timestamp.
- A worker that hangs costs one lease duration, not a permanently lost event.
- Handlers carry an obligation (idempotency or durable intent), which is stated in the
  `EventHandler` port and in the project rules.
- Dead-lettered events need human attention; there is deliberately no automatic replay
  path in code, so a bug cannot resurrect them by accident.
- Claiming costs one extra read inside the write transaction; in exchange, contention is
  resolved by SQLite rather than by luck.

