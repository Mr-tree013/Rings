# ADR-0036 — Weekly Recurring Calendar Rules

## Title

Weekly recurring calendar rules: a weekly class is authoritative calendar-domain state, its
occurrences are derived, and the deterministic planner treats them as busy time.

## Status

Accepted

## Context

The first real user workflow Rings had to support is the one every student already has: a term of
fixed classes. "周一早上十点到十二点是计算机系统基础课" is not an event, and it is not a scheduled
job either. It is a standing weekly commitment that repeats until the term ends, and the weekly
plan has to be arranged *around* it.

Three tempting shortcuts were rejected before this phase started:

* **materialising a `CalendarEvent` per week.** Unbounded rows, a horizon nobody can name, and an
  edit that has to rewrite the future — and then reconcile it with plans already applied;
* **reusing `ScheduledJob`.** A job is a *thing the system does* (send a reminder, replan, run an
  import). A class is a *fact about the user's week*. Merging them would let a reminder scheduler
  change a calendar authority, and would make "what is my week" unanswerable from calendar state;
* **an RRULE string.** A general recurrence grammar is an interpreter with an unbounded surface,
  and it is exactly the kind of free-form payload the conversation schema exists to refuse. It
  would also let the model express "every other week" in a build that cannot honour it.

## Decision

1. `RecurringCalendarRule` is authoritative calendar-domain state. It is stored once and read as
   authority, in the same sense as a `CalendarEvent` or a task deadline.
2. It is **not** a `ScheduledJob`. A rule never triggers work, never carries a schedule, and is
   never executed; the two live in separate tables with separate owners.
3. Occurrences are derived deterministically from the rule and a requested range. Nothing caches
   them, nothing stores them, and the same rule over the same range always yields the same
   instants.
4. Do not materialise unbounded `CalendarEvent` rows for a recurring commitment. A rule is one
   row; a term is one row.
5. Weekly recurrence uses an explicit IANA timezone carried by the rule. Expansion is pure
   arithmetic over `zoneinfo`; the host timezone is never read.
6. A new conversational recurring rule is interpreted in `planning.timezone`.
7. A missing `planning.timezone` produces a clarification, not a guess. Zero rules are created.
8. Never guess the host timezone: `datetime.now().astimezone()`, `TZ` and implicit UTC are all
   forbidden as a source of meaning.
9. v1.1 supports `WEEKLY` only, one weekday per rule, same day, non-overnight.
10. "Monday and Wednesday" is **two** rules with the same title, time and timezone. It is not one
    rule with a weekday list, and the conversation proposes two explicit typed operations rather
    than a recurrence string.
11. The supported surface of one rule is exactly: title, weekday, local start and end time,
    timezone, `starts_on`, optional `ends_on`.
12. The unsupported surface is refused rather than approximated: every-N-weeks, odd/even weeks,
    monthly or yearly recurrence, the first week of a month, holiday exceptions, exam-week
    exceptions, month-end recurrence, overnight recurrence and external calendar synchronisation.
13. The planner consumes recurring occurrences as busy time. It merges them with calendar events
    and manual plan blocks, so an overlapping class is never double-counted.
14. The model never creates `PlanBlock`s. It may ask the deterministic planner for a proposal;
    only an explicit human confirmation applies one.
15. Recurring calendar writes are **local** writes. They are not `ActionRequest`s, need no
    `Approval`, produce no `ExecutionRun`, and touch no external system.
16. Editing a rule preserves its logical identity: same `id`, same `created_at`, new fingerprint,
    new meaning for the future.
17. Historical work sessions and applied plans are not rewritten when a rule changes. They record
    what was true when they were written.
18. Retiring a rule removes future derived occurrences. It never deletes the rule and never
    touches historical records.
19. A declarative statement of a weekly commitment ("我每周一十点到十二点有课。") is not by itself
    a request to create durable state. Tree asks before it writes; an explicit instruction
    ("记下来", "加到日历", "放进固定安排") is a mutation and may be written directly.
20. One exact weekly commitment is one rule. Repeating the same request is idempotent and adds
    nothing.

## Alternatives considered

* **A materialised occurrence table with a rolling horizon.** Rejected: it turns a derivation into
  state that has to be kept consistent with the rule, the planner and every applied plan.
* **`ScheduledJob` reuse.** Rejected: two different authorities with one table, and a scheduler
  that could change the user's calendar by "running" a class.
* **An RRULE-style field.** Rejected: unbounded grammar in the one place the build keeps a closed
  vocabulary, and it makes unsupported recurrence expressible.
* **Guessing the timezone from the host.** Rejected: the same rule would mean different instants on
  a laptop, a server and a CI runner.
* **Materialising a `CalendarEvent` per weekday.** Rejected for the same reason as the occurrence
  table, plus it makes "which class is this?" unanswerable.

## Consequences

* `calendar.list` answers with one-off events **and** derived occurrences, so a week reads the way
  the user thinks about it.
* The weekly planner cannot place a plan block over a class, because the class is busy time.
* A term of classes is a handful of rows, and a change to one class is an update to one row whose
  effect on the future is immediate and on the past is nil.
* The conversation vocabulary grows by four closed operations (`calendar.recurring.list`,
  `calendar.recurring.create_weekly`, `calendar.recurring.edit`, `calendar.recurring.retire`) and
  by no free-form recurrence argument.
* Unsupported recurrence is a refusal with zero mutation, so the build never quietly records a
  class that does not exist.

## Implementation notes

* Migration: `migrations/0018_recurring_calendar_rules.sql`.
* Domain: `domain/recurring_calendar.py` (rule, occurrence, fingerprint, `expand`, `expand_rules`).
* Port and store: `ports/recurring_calendar_repository.py`, `store/recurring_calendar.py`.
* Application: `application/recurring_calendar_service.py`.
* Planner: `application/planner_service.py` (`busy_intervals` merges derived occurrences).
* Conversation: ADR-0033 (runtime), ADR-0035 (preflight and capability metadata).
* Timezone policy for conversational civil times: ADR-0018 and ADR-0033 §20-§21.
* Integrity: `pw integrity check` re-derives every rule's fingerprint and invariants read-only.
