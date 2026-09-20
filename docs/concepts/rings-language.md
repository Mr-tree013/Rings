# Rings — the product language

**Rings** is the public name of this project. The name is a promise about how it is supposed to work:
it grows with you, in rings, from what you actually did and what you actually told it.

> 让每天的叶子，长成年轮。

This document freezes the vocabulary that goes with that name. It is **product language** — the words
you use when talking about the system — and it is **not** a replacement domain model. The frozen
technical terminology (`Task`, `Case`, `ActionRequest`, `Approval`, `ExecutionRun`, `FactCandidate`,
`ConfirmedFact`, `Playbook`, …) stays exactly as it is, and the architecture stays a modular
monolith of ports, adapters and explicit state machines.

## The six words

### Roots

**Long-lived, reliable, traceable personal sources of truth.**

In the current system:

- configured `local://` and `vault://` knowledge roots and their source-grounded retrieval
- the provenance attached to every `Correction`
- the append-only history of `ConfirmedFact` rows, including superseded ones
- durable personal source material that stays where it is instead of being copied

Roots are **not** a credential store, **not** model memory, and **not** unsourced belief. A Ring is
only worth its name if it grew from a Root you can still point at.

### Seeds

**The intentions, goals, commitments and things-to-do that you deliberately plant.**

In the current system: `Task`, `Deadline`, `CalendarEvent`, and planning intent.

> Seed is a product-language concept. There is no Seed domain entity in v1.

Do not add a `Seed` class, a `seed` table or a seed migration in order to make the metaphor literal.
The domain already has the right names, and they are frozen.

### Branches

**The controlled capability areas the Tree can call on.**

Typical Branches in v1:

```text
Mail
Planning / Scheduler
Knowledge / Librarian
Watchers
Execution
Learning
Operations
Mobile
MCP
```

> Branches are capability modules, not autonomous sub-agents.

A Branch is a bounded, typed capability with a port and an adapter behind it. It does not have its own
goals, its own memory, or the ability to approve anything. Do not create a `BranchAgent`, a
`SubAgent` or an `AgentRegistry` to express this vocabulary in code.

### Leaves

**The daily observations, messages, events and inputs that arrive in the system.**

In the current system: `MailMessage`, `WebObservation`, `ManualInput`, `InboundEvent`.

Leaves may be interpreted and classified, but they **do not automatically become commitments or
actions**. A Leaf that matters becomes a Seed only when you decide it does.

### Rings

The name carries two meanings, and both are deliberate:

1. **The whole product and repository.** Rings is what this system is called.
2. **The growth record that accumulates over time** — the audit history that a year of use leaves
   behind.

In the current system:

- `WorkSession` history (what actually happened, in seconds)
- `ExecutionRun` history (what was attempted, and how it ended)
- fact supersession and the history of confirmed values
- reviewed `Playbook`s and their dry-run evidence
- the longitudinal review state: proposals, notifications and decisions

> Rings is the product-language reading of existing durable history.

Rings are not a new table. Do not add a `Ring` entity or a `ring` migration; the Rings are already
being recorded, one row at a time.

### Tree

**The main assistant and coordinator that faces the user.**

The Tree talks to you through the surfaces that already exist: the conversation (`rings`, and
`pw chat` for the same runtime), the CLI (`pw`), the trusted-LAN mobile page and the local MCP
server. It coordinates Roots, Seeds, Branches, Leaves and Rings.

The conversation is the Tree's primary surface and still only a surface: a sentence is interpreted
into a closed set of typed local operations, and the deterministic runtime — never the model —
decides which of them is allowed, which one needs your confirmation, and which one executes. The
daemon and the other surfaces keep their existing roles.

> Tree is not an unrestricted autonomous agent.

The Tree never bypasses:

```text
ActionRequest
human Approval
ExecutionRun
Fact confirmation
Playbook review
capability registry
```

It proposes, prepares, drafts and remembers — and it asks.

## The mental model

```text
                    Tree
                     │
       ┌─────────────┼─────────────┐
       │             │             │
    Branches       Seeds         Leaves
       │             │             │
       └─────────────┼─────────────┘
                     │
                   Roots
                     │
                     ▼
                   Rings
```

This is a **product metaphor and a mental model**, not a call graph and not a data-flow diagram.
Nothing in it says that a Leaf flows through a Branch into a Seed; it says that these are the parts of
the product you are meant to hold in your head when you use it.

The technical architecture is unchanged and remains authoritative:

```text
Modular Monolith
Ports & Adapters
Event Driven
explicit state machines
```

Where the metaphor and the architecture disagree, the architecture wins and this document is wrong.

## The loop the Tree runs

The vocabulary above is static; the work moves through seven stages, and every stage has a
boundary.

```text
Observe
  ↓
Understand
  ↓
Commit
  ↓
Plan
  ↓
Execute
  ↓
Review
  ↓
Learn
```

| Stage | What happens there |
| --- | --- |
| Observe | Read-only IMAP receive, configured public HTTPS pages, and text you paste or forward. Nothing is fetched from an unconfigured source. |
| Understand | Deterministic parsing and threading, plus bounded model analysis that produces candidates only — never a durable mutation. |
| Commit | Tasks, deadlines, calendar events and work sessions are recorded as distinct domain concepts. |
| Plan | A deterministic planner writes a reviewable weekly proposal; reminders and rolling replans are durable scheduled jobs. |
| Execute | A `Case` holds prepared `ActionRequest`s. Execution happens only after one exact human `Approval`, and only for a registered capability. |
| Review | Every challenge, approval and execution is durable history. An ambiguous external result stays `UNKNOWN` instead of being retried. |
| Learn | Corrections become fact candidates until you confirm them; successful runs become playbook candidates until a dry run is replayed and you promote them. |

## How the vocabulary maps onto the frozen terminology

| Product language | Frozen technical terms |
| --- | --- |
| Roots | knowledge roots (`local://`, `vault://`), catalog + derived FTS index, `Correction` provenance, `ConfirmedFact` history |
| Seeds | `Task`, `Deadline`, `CalendarEvent`, `PlanBlock`, planning intent |
| Branches | capability modules: mail, planning/scheduler, knowledge, watchers, execution, learning, operations, mobile, MCP |
| Leaves | `MailMessage`, `WebObservation`, `ManualInput`, `InboundEvent` |
| Rings | `WorkSession`, `ExecutionRun`, fact supersession, reviewed `Playbook`s, proposals and notifications |
| Tree | the coordinator the user talks to, through CLI / mobile / MCP |

Nothing in the left column replaces anything in the right column. When you write code, a migration, a
test name or a CLI flag, use the frozen technical term.

The frozen names, all of which stay exactly as they are: `Task`, `Deadline`, `CalendarEvent`,
`PlanBlock`, `WorkSession`, `Case`, `ScheduledJob`, `InboundEvent`, `ActionRequest`, `Approval`,
`ExecutionRun`, `FactCandidate`, `ConfirmedFact`, `PlaybookCandidate` and `Playbook`.

## What the product language never changes

- No new domain entities, tables or migrations are created for these words.
- No capability is added, widened or renamed because of them.
- The safety boundaries are untouched: external effects still need an exact `ActionRequest`, one
  human `Approval` and an `ExecutionRun`; facts still need explicit confirmation; playbooks still
  never execute.

## Compatibility identifiers are frozen

Rings is the public project name. Some identifiers keep the historical `growing-assistant` naming,
because they are part of the v1 compatibility surface and renaming them would break working
installations, scripts and runtimes:

```text
python package            assistant
distribution name         growing-assistant
console scripts           pw · assistantd · growing-assistant-mcp
XDG directories           growing-assistant
environment variables     GROWING_ASSISTANT_*
SQLite tables             unchanged
migration names           unchanged
InboundEvent event types  unchanged
ActionRequest actions     unchanged
MCP resource/tool names   unchanged
```

For the same reason, the repository keeps its history: the frozen architecture specification and
the ADRs are written under the `growing-assistant` name, and `v1.0.0` is a published snapshot that is
never rewritten.
