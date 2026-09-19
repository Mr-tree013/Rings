# ADR-0015 — Deterministic Weekly Planning with Reviewable Proposals

## Title

Plan the week with a deterministic greedy scheduler that emits a durable, reviewable proposal
instead of mutating plan blocks directly.

## Status

Accepted (design frozen; implementation in progress — see "Implementation status")

## Context

Phase 3A gave the project honest inputs: tasks, deadlines, occupied time, planned time and
actual work are separate, comparable facts. Phase 3B turns them into a plan.

Two failure modes have to be designed out from the start:

- a planner that writes plan blocks as it thinks makes every automated decision unreviewable
  and destroys the user's own manual plan;
- an LLM asked to "figure out my week" cannot be held to a hard deadline constraint, cannot be
  reproduced, and cannot be tested.

## Decision

1. The V1 planner is a deterministic greedy scheduler.
2. The planner never calls an LLM.
3. Planner input comes from authoritative commitment state only.
4. A deadline is a hard temporal constraint.
5. Calendar events and active **manual** plan blocks are busy time.
6. A deadline itself never occupies busy time.
7. Work sessions only reduce remaining effort; they never occupy future time.
8. Planner-generated and manual plan blocks are distinguishable (`origin`).
9. Replanning may replace planner-generated future blocks, never manual blocks.
10. The planner first creates a durable `PlanProposal`; it writes no plan blocks.
11. Only an explicit apply turns a proposal into planner-generated plan blocks.
12. A proposal is bound to a fingerprint of its planning input; changed input makes it stale.
13. Applying a proposal and replacing old planner blocks happen in one transaction.
14. Planning intervals are half-open `[start, end)`.
15. Local availability is formed in the configured IANA timezone, then converted to instants.
16. DST is handled by stdlib `zoneinfo`, never by hand-computed offsets.
17. The user's timezone is never guessed; without `[planning]` the CLI refuses to plan.
18. Tasks without an estimate are not scheduled (`MISSING_ESTIMATE`).
19. Remaining effort = estimate − actual work (from work sessions).
20. The planner never edits a task's estimate.
21. If actual work already meets the estimate while the task is still OPEN, the planner reports
    `ESTIMATE_EXHAUSTED` instead of inventing more time.
22. The deadline buffer is a soft preference, not a hard deadline.
23. If buffer space is short but the real deadline is still reachable, buffer time may be used
    and `BUFFER_VIOLATED` is reported.
24. If the real deadline cannot be met, `INSUFFICIENT_CAPACITY` reports required vs scheduled
    minutes.
25. A task without a deadline may still be planned into remaining capacity.
26. A proposal is deterministic: same input, same blocks, same ordering, same issues.
27. No OR-Tools; the greedy algorithm is replaceable behind a stable planner interface.
28. No recurring availability exceptions or calendar recurrence.
29. No daemon-driven automatic replanning.
30. No reminders.

Additional frozen details:

- **Ordering**: tasks with deadlines first (`due_at`, then priority, then `created_at`, then
  id), then tasks without deadlines (priority, `created_at`, id). No hidden "AI priority".
- **First fit**: sorted tasks are placed into free slots by ascending start time; blocks never
  cross an availability gap.
- **Block sizing**: `min_block_minutes`/`max_block_minutes` bound each block; a slot smaller
  than the minimum is skipped unless that is all the task has left; zero-length blocks never
  exist.
- **Busy subtraction**: calendar events and manual plan blocks are merged (adjacent ranges
  merged) and subtracted from availability; planner blocks inside the planning window are
  ignored because the proposal replaces them.
- **Fencing**: proposals carry both an input fingerprint (audit, reproducibility) and the
  commitment revision they were built from (transactional fencing). Applying requires the
  current revision to match; otherwise the proposal is marked stale and nothing is written.
- **Manual blocks are never modified**: apply cancels only active planner blocks that intersect
  the proposal window, and inserts the new planner blocks with their proposal id.
- **Current week**: Monday 00:00 local → next Monday 00:00 local, with the effective start
  clamped to "now" so nothing is planned into the past.

## Alternatives Considered

- **Let an LLM pick the times**: unreproducible, untestable, and unable to guarantee a hard
  deadline constraint. Rejected.
- **Write plan blocks immediately**: efficient, and it silently rewrites the user's week. The
  proposal step exists so automation stays reviewable. Rejected.
- **Treat a deadline as a calendar event**: the most tempting shortcut, and it destroys the
  distinction between "must finish by" and "occupied". Rejected.
- **Let the planner move manual plan blocks**: manual blocks are the user's own decisions;
  silently moving them is the fastest way to make the planner untrustworthy. Rejected.
- **Infer actual effort from plan blocks**: reported effort would describe intentions, not
  reality. Rejected; work sessions remain the only source.
- **Treat the buffer as a hard deadline**: it would refuse to plan tasks that are still
  achievable. Rejected; the buffer is a preference that reports when it is violated.
- **Start with OR-Tools**: a large dependency and a solver whose output is harder to explain
  than the problem it solves. Rejected for V1.
- **Allow applying a stale proposal**: silently overwrites newer deadlines, work sessions or
  manual plans. Rejected; the only path is a fresh proposal.

## Implementation status

Delivered on this branch: planning preferences and IANA timezone validation, the planning domain
(window, read model, issues, proposed blocks, proposal), half-open interval algebra, `PlanBlock`
provenance (`manual` / `planner` + proposal id) with database constraints, and migration `0005`
(proposal tables, `commitment_meta` revision baseline, rebuilt `plan_blocks`).

Still to implement before Phase 3B is complete: commitment revision increments inside every
planning-relevant mutation, the planning repository (atomic apply, stale fencing, supersede),
the greedy planner and availability generation, `PlannerService`, and the CLI
(`pw plan week|proposals|show|apply`, `pw task edit`).

