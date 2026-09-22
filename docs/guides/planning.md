# Planning, tasks and actual work

## What it does

Rings keeps five ideas apart instead of collapsing them into one calendar:

| Concept | Meaning |
| --- | --- |
| `Task` | Something you intend to do. |
| `Deadline` | The latest acceptable time for a task. |
| `CalendarEvent` | Time that is already occupied. |
| `RecurringCalendarRule` | Time that is occupied **every week** until it ends. |
| `PlanBlock` | Time you plan to spend on a task. |
| `WorkSession` | Time you actually spent, in exact seconds. |

**A deadline does not occupy calendar time, and a plan block is never treated as actual work.**
Remaining effort is the task estimate minus recorded work sessions; planning can never claim that
work happened.

A recurring rule is authoritative calendar state, not a scheduled job and not a pile of events: it
is stored once, its occurrences are derived for whatever range is asked about, and the planner
treats them as busy time — so a plan block is never proposed on top of a class.

`我今天有什么事？` returns a deterministic **today brief** — the day's events, weekly classes and
planned blocks, plus overdue and due-soon tasks — computed in `[planning].timezone` and rendered by
the runtime ([ADR-0039](../adr/0039-deterministic-today-brief.md),
[conversation guide](conversation.md#today-in-one-answer)).

## Enable / configure

Planning needs a timezone and at least one availability window:

```toml
[planning]
timezone = "Asia/Shanghai"
min_block_minutes = 30
max_block_minutes = 120
deadline_buffer_minutes = 120

[[planning.availability]]
days = ["mon", "tue", "wed", "thu", "fri"]
start = "09:00"
end = "22:00"
```

Reminder offsets are configured separately:

```toml
[reminders]
deadline_offsets_minutes = [1440, 120]
```

Every time argument you type must be ISO 8601 with an explicit UTC offset. `tomorrow` and
`next Friday` belong to the interpreter (`pw interpret`), not to the structured CLI.

## Common workflow

```bash
uv run pw task add "Write the SE lab report" \
  --estimate 300 --priority high \
  --deadline 2026-10-20T23:59:00+08:00

uv run pw tasks                      # OPEN by default
uv run pw tasks --all                # include completed and cancelled
uv run pw task show <TASK>           # deadline, plan blocks and work sessions
uv run pw task edit <TASK> --estimate 240
uv run pw task deadline <TASK> --clear
uv run pw task done <TASK>           # cancels unfinished plan blocks atomically
uv run pw task cancel <TASK>
```

Time that is already taken is recorded rather than planned over:

```bash
uv run pw calendar add "SE lecture" \
  --start 2026-09-21T10:00:00+08:00 \
  --end 2026-09-21T12:00:00+08:00

uv run pw calendar --days 30
```

Time that is taken **every week** is a recurring rule. In the conversation, one sentence is enough
(`每周一 10 点到 12 点有课，记下来，再帮我安排下周`); the same rule is one weekday, so
"周一和周三" is two rules. Unsupported recurrence is refused rather than approximated: odd/even
weeks, every-N-weeks, monthly or yearly recurrence and holiday exceptions are not recorded in this
version.

Plan a block by hand, or record what actually happened:

```bash
uv run pw plan add <TASK> \
  --start 2026-09-23T19:00:00+08:00 \
  --end 2026-09-23T21:00:00+08:00

uv run pw work add <TASK> \
  --start 2026-09-22T20:00:00+08:00 \
  --end 2026-09-22T21:00:00+08:00

uv run pw work list <TASK>           # exact seconds of actual effort
```

## Weekly proposals

Planning is proposal-based, and the planner is deterministic code rather than a model:

```bash
uv run pw plan week                  # writes a PENDING proposal; never applies it
uv run pw plan week --next
uv run pw plan proposals
uv run pw plan show <PROPOSAL>       # exactly what was proposed, with no re-planning
uv run pw plan apply <PROPOSAL>      # the only path that writes plan blocks
uv run pw plan cancel <BLOCK>
```

- Applying validates the commitment revision the proposal was planned against. A proposal that no
  longer matches is marked `STALE` and writes nothing.
- Only planner-origin blocks inside the proposal window are replaced. Blocks you added by hand are
  yours and are left alone.
- `pw plan show` re-displays the stored proposal instead of planning again, so what you review is
  what was decided at that moment.

## Capacity: how much, and how long

The planner is deterministic, and it works inside rules you own. Two kinds of rules exist, and they
are deliberately different things:

```toml
[planning]
timezone = "Asia/Shanghai"     # the one authority on what "today" means
```

```text
You > 每天早上九点开始，晚上十点以后不要安排任务。
You > 一天最多给我安排六小时。
You > 一个任务最多一次排两个小时。
```

The second kind is a durable *preference*, stored in the runtime database and changeable from the
conversation at any time: the planning day's start and end, the maximum planned minutes per day, the
preferred sitting length and the maximum one. `planning.preferences.show` reports what is in effect;
`planning.preferences.update` changes only what you named. The timezone is not one of those fields —
it lives in the host configuration and has exactly one authority.

Availability rules and fixed weekly commitments remain the outer bound: a preference narrows the
day, it never overrides a class or an appointment.

## Replanning what is left

```text
You > 这周太满了，重新安排一下。
You > 今天没做完的往后排。
```

`plan.replan_week` proposes a replacement for the remaining week. It is still a proposal: the
current plan stays authoritative until you apply the new one, and applying it is a local "可以".
Three properties matter:

- **Only future automatic blocks are replaced.** A replan supersedes `origin=planner` blocks it
  overlaps from "now" forward; past blocks are history and manual blocks are yours — neither is ever
  rewritten or removed.
- **Superseded is recorded beside cancelled.** A replaced block keeps `cancelled_at` *and*
  `superseded_at` / `superseded_by_proposal_id`, so "the user cancelled this" stays distinguishable
  from "a later plan replaced it".
- **Large estimates are split deterministically.** A five-hour task becomes sittings of the
  preferred length, never longer than the maximum, and a remainder that does not fit is reported as
  an unschedulable case rather than hidden.

## Reminders and rolling replans

```bash
uv run pw notifications              # durable inbox
uv run pw notifications show <NOTIFICATION>
uv run pw notifications read <NOTIFICATION>
uv run pw scheduled                  # job visibility, read-only
uv run pw scheduled --all
```

Reminder and replan intent lives in runtime SQLite and is recovered after a restart; in-process
timers are only a wake-up mechanism. Rolling replanning may create a new `PENDING` proposal and a
`PLAN_READY` notification, and it never applies one.

## Commands

```text
pw task add "Write the SE lab report" --estimate 300 --priority high
pw task show <TASK>
pw task edit <TASK> --estimate 240
pw task deadline <TASK> --clear
pw task done <TASK>
pw task cancel <TASK>
pw tasks [--all]
pw calendar add "SE lecture" --start 2026-09-21T10:00:00+08:00 --end 2026-09-21T12:00:00+08:00
pw calendar --days 30
pw plan add <TASK> --start 2026-09-23T19:00:00+08:00 --end 2026-09-23T21:00:00+08:00
pw plan week
pw plan week --next
pw plan proposals
pw plan show <PROPOSAL>
pw plan apply <PROPOSAL>
pw plan cancel <BLOCK>
pw work add <TASK> --start 2026-09-22T20:00:00+08:00 --end 2026-09-22T21:00:00+08:00
pw work list <TASK>
pw notifications
pw notifications show <NOTIFICATION>
pw notifications read <NOTIFICATION>
pw scheduled [--all]
```

Identifiers accept a full UUID or a unique prefix; an ambiguous prefix is refused rather than
guessed.

## Safety behavior

- Completing or cancelling a task cancels its unfinished plan blocks in the same transaction.
- A deadline reminder job is materialized or cancelled together with the deadline or task change,
  never in a second step.
- The planner cannot mutate anything: its output is a durable proposal that you apply explicitly.
- Nothing scheduled can execute an external action; a reminder is a notification row, not a side
  effect.

## Troubleshooting / limitations

- `pw plan week` complains about `MISSING_ESTIMATE`: fix it with `pw task edit <TASK> --estimate N`.
- No `[planning].timezone` means the interpreter refuses time-bearing requests instead of guessing
  the machine timezone.
- Recurring support is weekly and same-day only: one rule is one weekday, no overnight rule, no
  every-N-weeks, no odd/even weeks, no monthly or yearly recurrence and no holiday exceptions.
  A weekly rule is never materialized as `CalendarEvent` rows.
- There is no repeating *task* support, and no personal effort learning, in v1.

## Implementation notes

- Domain model: [ADR-0014](../adr/0014-commitment-domain-model.md)
- Planner: [ADR-0015](../adr/0015-deterministic-weekly-planner.md)
- Scheduling and replanning: [ADR-0016](../adr/0016-durable-scheduler-and-replanning.md)
- Weekly recurring rules: [ADR-0036](../adr/0036-weekly-recurring-calendar-rules.md)
- Capacity preferences and replanning: [ADR-0044](../adr/0044-planning-capacity-and-replanning.md)
- System design §6 and §7.2: [docs/specs/0001-system-design.md](../specs/0001-system-design.md)
