# Planning, tasks and actual work

## What it does

Rings keeps five ideas apart instead of collapsing them into one calendar:

| Concept | Meaning |
| --- | --- |
| `Task` | Something you intend to do. |
| `Deadline` | The latest acceptable time for a task. |
| `CalendarEvent` | Time that is already occupied. |
| `PlanBlock` | Time you plan to spend on a task. |
| `WorkSession` | Time you actually spent, in exact seconds. |

**A deadline does not occupy calendar time, and a plan block is never treated as actual work.**
Remaining effort is the task estimate minus recorded work sessions; planning can never claim that
work happened.

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
- There is no repeating task or repeating event support, and no personal effort learning, in v1.

## Implementation notes

- Domain model: [ADR-0014](../adr/0014-commitment-domain-model.md)
- Planner: [ADR-0015](../adr/0015-deterministic-weekly-planner.md)
- Scheduling and replanning: [ADR-0016](../adr/0016-durable-scheduler-and-replanning.md)
- System design §6 and §7.2: [docs/specs/0001-system-design.md](../specs/0001-system-design.md)
