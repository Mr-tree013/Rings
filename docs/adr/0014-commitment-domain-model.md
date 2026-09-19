# ADR-0014 — Explicit Commitment and Time-Planning Domain Model

## Title

Model commitments with five distinct concepts — Task, Deadline, CalendarEvent, PlanBlock and
WorkSession — instead of one generic schedule item.

## Status

Accepted

## Context

Phase 3 begins the personal-assistant half of the project: tasks, deadlines and actual work.
The tempting shortcut is to treat everything as a calendar entry ("this is on my calendar,
therefore it is my plan"). That shortcut destroys exactly the information a planner needs:

- a *deadline* is a promise about the latest acceptable finish, not a stretch of time to work;
- a *calendar event* is time already taken by something else, and it belongs to no task;
- a *plan* is an intention to work on a task during a window that is not yet reserved;
- *actual work* is a fact about the past, and it frequently disagrees with the plan.

If two of these are stored as one thing, no later phase can separate them again, and the
assistant will plan against fiction.

## Decision

1. `Task`, `Deadline`, `CalendarEvent`, `PlanBlock` and `WorkSession` are distinct domain
   concepts with distinct tables.
2. A task cannot be represented as a calendar event.
3. A deadline is "the latest acceptable completion time"; it is never a work block.
4. A calendar event is time that is already occupied.
5. A plan block is a time interval planned for one task.
6. A work session is a record of work that actually happened.
7. A task has at most one *active* deadline.
8. A task may have no deadline at all.
9. A deadline is persisted as its own row, never as a JSON field inside a task.
10. All domain timestamps are timezone-aware.
11. The runtime database stays the authoritative state for this domain.
12. An LLM never owns task state; a future interpreter may only emit application commands.
13. A future planner may only change plan blocks through the repository/application API.
14. Actual effort comes from work sessions; it is never inferred from plan blocks.
15. This phase offers no physical delete of business objects: state changes and cancellation
    are the tools (`clear_deadline` removes an *active deadline* attachment, which the domain
    models as at-most-one rather than as history).
16. Phase 3A creates no reminders.
17. Phase 3A has no recurring tasks or events.
18. Phase 3A has no cross-timezone calendar UI; times remain aware internally.
19. Task effort is expressed in whole minutes.
20. Every mutation goes through an application service; the CLI never writes SQL.

Supporting rules:

- **Interval semantics are half-open**: `[start, end)`. `10:00-11:00` and `11:00-12:00` do not
  overlap, and range queries use `starts_at < query_end AND ends_at > query_start`.
- **Optimistic concurrency**: `updated_at` is the compare-and-set token for every task-scoped
  write. A stale writer gets `StaleTaskUpdate`, so two callers can never silently overwrite
  each other. Setting or clearing a deadline also advances the task's `updated_at`.
- **Atomic terminal transitions**: completing or cancelling a task and cancelling its
  unfinished plan blocks (`ends_at > terminal time`) happen in one transaction. A completed
  task with live future plan blocks is not a representable state.
- **Deadlines are historical context**: completing or cancelling a task keeps its deadline.
- **Only OPEN tasks change**: terminal tasks are history, so they cannot be edited, planned
  or re-deadlined in this phase. Work sessions are the deliberate exception, because logging
  what you did after the fact is normal.
- **The persistence boundary follows atomicity**: instead of one repository per entity, a
  single `CommitmentRepository` covers tasks, deadlines, calendar events and plan blocks, so
  the atomic terminal transition is a single transaction. Work sessions keep their own
  `WorkRepository`. Neither is a generic CRUD surface.
- **CLI input is structured**: explicit ISO 8601 timestamps with an offset, no natural-language
  dates, no assumption of machine-local time. Id arguments accept a full UUID or an
  unambiguous prefix, and refuse to guess when a prefix matches several objects.

## Alternatives Considered

- **Model every commitment as a calendar event**: one table, one mental model — and no way to
  distinguish "already occupied" from "planned" from "due", which is precisely what planning
  needs. Rejected.
- **Keep the deadline inside the task row**: fewer tables, but it hides the deadline's own
  lifecycle (rescheduling keeps its identity) and makes "tasks with deadlines" queries and
  future reminder logic awkward. Rejected.
- **Let a plan block double as the record of actual work**: convenient, and wrong: a plan is an
  intention that may never happen, while actual work is evidence. Effort statistics built on
  plans would silently lie. Rejected.
- **Store the whole task as a JSON blob**: flexible schema, no constraints, no queries worth
  having, and no database-level protection for terminal-state consistency. Rejected.
- **Implement recurring tasks and events immediately**: high value, high complexity (RRULE
  semantics, timezones, exceptions) and not needed to establish durable state. Deferred.
- **Let the LLM write to the database directly**: unverifiable, unauditable and impossible to
  reconcile with optimistic concurrency. Rejected: models will emit commands, services write.

## Consequences

- The planner gets honest inputs: occupied time, intended time, deadlines and actual results
  are all separately queryable, and a deadline can never be mistaken for busy time.
- `get_busy_intervals` reports its sources (`calendar_event` vs `plan_block`), so a lecture and
  a planned work session stay distinguishable.
- Finishing a task cannot leave stale future plans behind, and a failed transaction leaves
  both the task and its plan blocks untouched.
- Concurrent edits fail loudly instead of losing data; the caller reloads and retries.
- Effort reporting ("estimated 300 minutes, actually 150") becomes possible because the two
  numbers come from different, non-overlapping sources.
- There is no "reopen" and no delete: a mistaken completion must be corrected in a later phase
  with an explicit design, rather than by quietly mutating history.
- Terminal tasks accumulate as history; archiving and retention are future concerns.

