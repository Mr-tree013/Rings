# ADR-0016 — Durable Scheduling, Reminders, and Rolling Replanning

## Title

Reminders and rolling replans are durable scheduling intent executed by a supervised daemon
service: they survive a restart, they never silently disappear, and they never apply a plan.

## Status

Accepted

## Context

Phase 3A gave commitments honest state; Phase 3B turned that state into reviewable proposals.
What is still missing is *time*: a deadline that nobody is told about is not a deadline, and a
plan that is never re-derived after the week changes is a snapshot, not a plan.

Three failure modes shaped this design:

- **an `asyncio` timer is not a schedule.** A reminder that lives only in process memory is
  lost on the next restart, and the user never learns that it existed;
- **a reminder that fires twice is as bad as one that never fires.** Execution is
  at-least-once, so the delivery path must be idempotent rather than hopeful;
- **automatic replanning must not become automatic reprioritising.** The user reviews plans;
  a background job that silently rewrote the calendar would break the central promise of
  Phase 3B.

## Decision

1. A `ScheduledJob` is an independent domain concept. It is not a task, not a plan block and
   not an `InboundEvent`.
2. Scheduling intent lives in the runtime SQLite database. It is never only in asyncio memory.
3. After a daemon restart, every still-pending job is recovered from the database.
4. Job execution uses a durable claim lease with a fencing token.
5. Execution is at-least-once. Distributed exactly-once is not promised.
6. A job handler is idempotent, or it relies on a durable dedup key.
7. Reminder delivery writes to a local durable notification inbox. It does not push to the OS,
   email, QQ or SMS.
8. The notification inbox is the single source future mobile, web and CLI clients read.
9. Deadline reminder jobs are maintained by the task and deadline mutations themselves, in the
   same transaction as the mutation.
10. When a deadline moves, the old reminder jobs are invalidated and replaced.
11. Clearing a deadline, completing a task or cancelling a task cancels its unsent reminders.
12. The scheduler never scans tasks to guess reminders: a business mutation materializes jobs
    explicitly.
13. Rolling replanning is one kind of `ScheduledJob`.
14. Rolling replanning may only create a new `PENDING` `PlanProposal`.
15. Rolling replanning never applies a proposal.
16. Commitment revision and input fingerprint keep protecting proposal correctness.
17. Work sessions, calendar events, manual plan blocks, deadline changes, task changes and
    plan block changes may all request a rolling replan.
18. High-frequency changes coalesce; they never accumulate one replan job per mutation.
19. One replan scope has at most one active (pending or processing) job at a time.
20. Reminder and replan failures use a deterministic retry and backoff policy.
21. A job that exhausts its attempts becomes `DEAD_LETTERED`.
22. `CancelledError` is never converted into a job failure.
23. A job whose lease expired may be reclaimed; the older fencing token stops working.
24. The scheduler service is supervised by the daemon supervisor.
25. A scheduler failure never stops the index-sync service.
26. No APScheduler, Celery or Redis is introduced.
27. No recurring/cron abstraction is implemented.
28. No arbitrary user-supplied code jobs exist.
29. No LLM-generated reminders exist.
30. Notification delivery in this phase means exactly one thing: a row in the inbox.

Additional frozen details:

- **No persistent `FAILED` status.** A retryable failure returns the job to `PENDING` with a
  new `next_attempt_at`; the job already owns an explicit due time, so copying the event
  worker's `FAILED` model would add a second, redundant notion of "when to try again".
- **Eligibility.** A job is claimable when it is `PENDING` and `coalesce(next_attempt_at,
  due_at) <= now`, or when it is `PROCESSING` with `lease_expires_at <= now`. Claiming sets
  `status = processing`, `attempts += 1`, a fresh claim token, and clears `next_attempt_at`.
  Ordering is `effective due ASC, created_at ASC, id ASC`.
- **Payloads are canonical JSON objects.** No pickle, no module paths, no shell strings, no
  `eval`. A handler parses the payload of its own kind and re-reads authoritative rows.
- **Reminder payload** = `task_id`, `deadline_id`, `deadline_due_at`,
  `reminder_offset_minutes`. It deliberately does not copy the task's title or description.
- **Reminder identity** = `deadline-reminder:<deadline-id>:<due-at-digest>:<offset>`. The due
  instant is part of the key so a rescheduled deadline produces a new generation of jobs
  instead of silently reusing an in-flight one.
- **Notification identity** = `notification:deadline:<job-id>`, `notification:plan-ready:
  <job-id>` or `notification:scheduler-warning:<job-id>`. One job yields at most one message,
  enforced by a `UNIQUE` constraint, which is what makes a crash between "message written" and
  "job completed" harmless.
- **Reminder due times are clamped to now.** A deadline that is already inside its reminder
  offsets produces an immediately due reminder; reminders are never dropped for being late.
- **Reminder execution is defensive twice.** The mutation cancels obsolete jobs, and the
  handler independently checks that the task is still open and that the deadline still exists
  with the same identity and due instant. A reminder that fails either check completes as
  obsolete without notifying.
- **Replan payload** = `timezone` + `window_kind` (`current_week`). Nothing about planning
  state is copied; the handler re-reads authoritative state through `PlannerService`.
- **Replan debounce.** Every request sets `due_at = now + replan_debounce_seconds` on the single
  active `rolling-replan:current-week` job, so changes at `T`, `T+10s`, `T+20s` coalesce to
  `T+80s` (a trailing debounce). An already `PROCESSING` replan job is left alone: it reads the
  authoritative state when it runs, so its result is measured against fresh input.
- **Replan no-op rule.** If a pending proposal already exists for the same planning horizon
  and the commitment revision has not moved, the handler completes without creating anything.
  Replanning is a response to change, not a heartbeat.
- **Planning horizon identity** is `(window_end, timezone)`. `window_start` is clamped to
  "now" and therefore drifts by minutes between proposals; a week is what the user reviews, so
  a newer proposal for the same week supersedes the older pending one.
- **No planning configuration is not a transient failure.** With no `[planning]` section the
  handler completes the job and writes one `SCHEDULER_WARNING` notification instead of
  retrying five times.

## Alternatives Considered

- **`asyncio.sleep`-only scheduler**: no restart recovery, invisible loss, untestable without
  the process. Rejected.
- **APScheduler**: a general cron framework with its own persistence model, its own job
  identity and no notion of claim fencing. Rejected; the durable store already exists.
- **Scan every task's deadline each cycle to decide what to remind**: cheap to write, and it
  makes reminders a derived guess instead of explicit intent that mutations own. Rejected.
- **Turn every `ScheduledJob` into an `InboundEvent`**: it would blur two domains that have
  genuinely different lifecycles (an event is something that *arrived*; a job is something
  that is *due*) and would change Phase 1 semantics. Rejected.
- **Deliver reminders by printing a log line**: invisible to the user and impossible for a
  mobile client to read later. Rejected; the inbox is durable and queryable.
- **Let rolling replanning apply its proposal**: convenient, and it silently rewrites the
  user's calendar. Rejected; the proposal stays reviewable.
- **Replan synchronously on every state change**: unbounded work inside a user command, and
  the plan would be recomputed once per keystroke-scale change. Rejected in favour of a
  debounced job.

## Consequences

- Reminders survive restarts, and a delivery may repeat after a crash but can never silently
  disappear: the second attempt finds the notification it already wrote.
- Manual plan blocks remain user-owned: rolling replanning only produces proposals, and only
  `pw plan apply` writes planner blocks.
- The scheduler is boring infrastructure: two fixed job kinds, typed payloads, deterministic
  retry, and no place for a model to make a decision.
- Costs: the inbox is the only delivery channel in this phase, so nothing reaches a phone; the
  scheduler polls (every 15 seconds by default) rather than waking on an event; and a reminder
  placed inside its own offset window fires immediately, which is intentional but surprising
  the first time.
