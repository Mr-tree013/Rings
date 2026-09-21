# ADR-0039 — Deterministic Today Brief

## Title

"What is going on today?" is answered by a read-only, deterministic aggregation of the user's own
local state, computed in their planning timezone and never written anywhere.

## Status

Accepted

## Context

By the end of Phase 10E a user could talk to Tree about tasks, a calendar, weekly classes, plans,
reminders, mail and long-term facts. The question they then ask most often — "我今天有什么事？" —
was the one thing no capability answered: it spans all of those domains, and the model cannot be
the thing that assembles it.

Six shortcuts were rejected before this phase started:

* **a model-written summary.** A brief that a model composes from whatever it happens to remember
  is not a report about the user's day; it is prose that may be wrong in ways nobody can check;
* **a dashboard framework.** A generic widget/panel abstraction would be a new product surface with
  its own configuration, storage and drift — for one deterministic read;
* **a stored snapshot.** A cached brief is a second copy of state that can disagree with the state
  it summarises;
* **the host timezone.** "Today" is a civil day. On a laptop in another timezone the same instant
  can belong to a different day, and a brief for the wrong day is worse than no brief;
* **the whole mailbox or the whole task list.** A brief is a bounded answer, not a dump;
* **calling an unknown send a failure.** An external outcome nobody can prove must be reported as
  unknown, because a user who believes a message failed might send it again.

## Decision

1. `TodayBriefService` is a deterministic application service. It has no `ModelPort` dependency.
2. It reads through existing ports and services. It never issues SQL itself, and it never writes:
   no repository mutation, no approval, no execution, no snapshot.
3. "Today" is the user's civil day in `[planning].timezone`, derived from the existing `Clock`.
   A request that depends on "today" without a configured planning timezone gets a clarification;
   the host timezone is never used as a fallback.
4. The brief aggregates, bounded: today's one-off calendar events, today's derived recurring
   occurrences, today's applied plan blocks, overdue / due-today / due-soon / high-priority open
   tasks, unread reminders, stored mail that asks for a reply, prepared-but-unsent mail, pending
   plan proposals, pending local conversational confirmations, pending long-term fact reviews, and
   unresolved external execution outcomes.
5. Mail appears as metadata only — sender, subject, time, "requires reply" — never as a body.
6. An unresolved external outcome is labelled as unknown, with an explicit statement that it will
   not be retried automatically. It is never called a failure.
7. Nothing in the brief is executed or confirmed on the user's behalf: a prepared letter stays
   prepared, a proposal stays pending, a fact stays unconfirmed.
8. Every section is bounded (schedule ≤ 10, tasks ≤ 10, mail ≤ 5, reminders ≤ 5, waiting ≤ 5,
   checks ≤ 5) and says how many items it dropped.
9. The brief is discussed in the conversation as one read operation, `brief.today`, whose text the
   runtime renders from the returned data.
10. No durable state is added: no migration, no table, no integrity section.

## Alternatives considered

* A model-composed summary of the day. Rejected: unverifiable, and it would sometimes invent.
* A cached or stored brief. Rejected: derived state that can disagree with its sources.
* A generic dashboard/widget framework. Rejected: a new surface for one deterministic read.
* Reading the host timezone when no planning timezone is configured. Rejected: silent wrongness.
* Showing full mail bodies. Rejected: the brief is a pointer to attention, not a reader.
* Reporting an unknown send as failed. Rejected: it invites a duplicate send.

## Consequences

* The same local state always produces the same brief, in the user's own timezone.
* A brief can be produced with no provider configured at all (the runtime still routes the request
  through the interpreter, which needs a model; the *brief itself* needs none).
* `pw integrity check` needs nothing new, because nothing new is stored.
* A restored runtime produces a brief from the restored rows, with no snapshot to reconcile.

## Implementation notes

* `application/today_brief.py` (`TodayBriefService`, `TodayBrief`, `BriefEntry`).
* Conversation: `brief.today` (READ) in ADR-0033's registry, rendered by
  `application/conversation_render.py`.
* Sources: ADR-0014/0015 (tasks, plans), ADR-0016 (reminders), ADR-0020–0024 (mail),
  ADR-0033/0034 (conversation state and reviews), ADR-0036 (weekly commitments),
  ADR-0038 (pending fact reviews).
