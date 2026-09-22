# ADR-0044 — Planning Capacity and Replanning

## Title

The deterministic planner gains durable capacity *preferences* — a planning day, a daily minute
budget, a preferred sitting length and a maximum one — plus a replan that proposes a replacement
for what is left of the week and supersedes only the future automatic blocks the user confirms.

## Status

Accepted

## Context

v1.2's planner could already avoid classes, respect weekly availability and report what did not fit.
What it could not express was the shape of a person's day:

```text
每天晚上十点以后不要给我安排学习     →  a day that ends at 22:00
一天最多给我安排六小时               →  a daily budget
一个五小时的任务不要一次排五小时     →  a preferred sitting length
这周太满了，重新安排一下             →  the rest of this week, replanned
```

Availability rules are *admin* configuration: they live in `config.toml`, they are the same every
week, and they describe when work is possible. These new statements are different in kind. They are
the user's own preferences, they arrive mid-conversation, and they change. Putting them in the
config file would mean editing a file to say "not after ten"; putting them in the conversation would
mean re-interpreting them forever and forgetting them on restart.

The dangerous version of this phase is the obvious one: let the model choose the times. That would
make a proposal unreproducible, untestable, and — since a plan is what the user then acts on — a
thing nobody could review.

## Decision

1. **Hard time placement remains deterministic.** `GreedyPlanner` is still pure: no clock, no
   repository, no model, no randomness. The same request produces byte-identical blocks.
2. **The model never creates PlanBlocks.** It may ask for a proposal. It cannot write a block, and
   there is no operation through which one could arrive.
3. **PlanningPreferences are durable local user preferences**, stored in the runtime database.
4. **The planning timezone remains explicit.**
5. **Host timezone is never planning authority.** It is still `[planning].timezone`, and the
   preferences deliberately have no timezone field: one authority, not two.
6. **v1.3 preferences support a bounded set:** planning-day start, planning-day end, maximum planned
   minutes per day, preferred block duration, maximum block duration. Nothing else.
7. **Fixed Calendar and RecurringCalendar are hard busy time.** A preference narrows the day; it
   never overrides a commitment.
8. **Existing active PlanBlocks are considered unless a replan proposal explicitly supersedes
   future auto-planned blocks.** Manual blocks are busy time, so the planner works around them.
9. **Past PlanBlocks are history and are never rewritten.** A replan window starts at "now".
10. **Manual/fixed blocks are not silently removed.** `origin=manual` is never superseded by a
    proposal — it can only be edited by the user.
11. **Replanning creates a proposal first.**
12. **Replanning requires the existing explicit local apply confirmation.**
13. **No automatic reschedule merely because time passed.** The only thing that changes on its own
    is that a passed-but-open block becomes visible (item 14).
14. **A passed PlanBlock whose task is still open may generate attention.**
15. **That does not prove the user did no work.** The wording is "the planned time has passed and the
    task is not finished", never "you did not do this".
16. **Replanning may propose remaining future time.**
17. **Large task estimates may be split deterministically.**
18. **Daily maximum capacity is enforced.** It is a hard constraint in the planner, not a nudge.
19. **The planner may report an unschedulable remainder honestly**, as
    `DAILY_CAPACITY_REACHED` / `INSUFFICIENT_CAPACITY` / `WINDOW_CAPACITY_EXHAUSTED`.
20. **No model-based schedule scoring replaces deterministic constraints.**

### Rejected alternatives

- **An LLM chooses clock times.** Unreproducible, and a plan is something the user acts on.
- **Automatically moving plans without approval.** Silent mutation of the user's own week.
- **Deleting historical blocks.** A plan that was made is a fact about what was intended.
- **Treating an open task as proof no work occurred.** A WorkSession may exist; only the user knows.
- **Unlimited daily capacity.** The preference exists because the limit is the point.
- **Scheduling outside the configured work window.** Availability stays the outer bound.

## Consequences

### What changed in the planner

`PlanningRequest` gained two optional fields, both defaulted so that an unconfigured caller plans
exactly as before:

```text
preferred_block_minutes   0 means "use max_block_minutes" — not a silent shortening
daily_capacity            local days with their own minute budget; empty means "availability only"
```

The greedy planner now takes the *preferred* length as the normal chunk, never exceeds the maximum,
and refuses to place more than a day's budget inside that day. A fragment shorter than the minimum
block is still placed when it *finishes* the task and skipped when it would only be a fragment — the
v1.2 rule, unchanged.

### What changed in storage

`planning_preferences` is a singleton addressed by a constant id, so "which preferences apply" is
not a question with two answers. `plan_blocks` gained `superseded_at` and
`superseded_by_proposal_id` **beside** `cancelled_at`, never instead of it: every reader that
already filters `cancelled_at IS NULL` keeps working, and "the user cancelled this" stays
distinguishable from "a later plan replaced it". `plan_proposals` gained a `mode`, so a reviewer can
tell a fresh weekly plan from a replan of what is left.

A host with no stored row is completely unaffected: the migration adds columns and an empty table,
and the defaults describe the whole civil day with the planner's existing ceilings.

### Attention

`AttentionKind.PLAN_BLOCK_PASSED` already exists (ADR-0042) and reports a passed, uncancelled block
whose task is still open, in the words "计划时间已经过去，但任务仍未完成。" It resolves the moment the
task is completed.

## Implementation notes

- Domain: `domain/planning_preferences.py` (`PlanningPreferences`, `DailyWindow`, `ProposalMode`).
- Storage: `store/planning_preferences.py`, and the `mode`/supersession columns in
  `store/planning.py`. Blocking SQL runs in private `_*_sync` methods through `asyncio.to_thread`
  (ADR-0009).
- Availability keeps its frozen DST policy (ADR-0015): a spring-forward day is genuinely shorter
  and a fall-back day genuinely longer, and a window that never happens is skipped.
- Conversation operations: `planning.preferences.show`, `planning.preferences.update` and
  `plan.replan_week`. The first is `READ`; the other two are `LOCAL_WRITE`, and applying a proposal
  remains `CONFIRM_LOCAL`.
- `/settings` reports the effective preferences and the planning timezone read-only, naming
  `[planning].timezone` as the authority.
- Migration: `migrations/0022_planning_preferences.sql`.
