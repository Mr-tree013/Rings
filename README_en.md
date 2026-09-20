# Rings

A local-first personal operations system that grows with you.

让每天的叶子，长成年轮。

[![CI](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml/badge.svg)](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml)

**Language:** [中文](README.md) ｜ English

**Version:** 1.0.0 · **Reference environment:** Linux / WSL + Python 3.13

Rings is a local-first personal operations system. It runs on your machine, keeps its state in one
SQLite database plus the files that database references, and reaches the outside world only through
the integrations you configure.

It observes what arrives — mail, public notices, text you forward — turns the intentions you state
into commitments and plans, prepares carefully bounded external actions, and keeps a reviewed
history that grows over time.

It is not a general autonomous computer agent. There is no shell, no filesystem control, no generic
browser and no generic HTTP client in it, and every production external effect waits for one exact
human approval.

## The Tree Model

| Word | Meaning |
| --- | --- |
| **Roots** | Long-lived, traceable personal sources of truth: your indexed documents and the facts you confirmed, with their provenance. |
| **Seeds** | The intentions and commitments you plant deliberately: tasks, deadlines, calendar events and planning intent. |
| **Branches** | The controlled capability areas the Tree can call on. Branches are capability modules, not autonomous sub-agents. |
| **Leaves** | What arrives each day: mail, observations, pasted or forwarded text. A Leaf may be interpreted, but never becomes a commitment or an action by itself. |
| **Rings** | The growth record that accumulates over time: work sessions, executions, fact history and reviewed playbooks. |
| **Tree** | The coordinator you talk to, through the CLI, the mobile page and your editor. It is not an unrestricted autonomous agent. |

The Tree coordinates all of it and never bypasses an `ActionRequest`, a human `Approval`, an
`ExecutionRun`, fact confirmation, playbook review or the capability registry. The full vocabulary
lives in [docs/concepts/rings-language.md](docs/concepts/rings-language.md).

## What Rings Can Do

| Branch | What it does |
| --- | --- |
| **Planning** | Tasks, deadlines, calendar events, work sessions, and deterministic weekly proposals you review and apply. |
| **Knowledge** | Indexes the local and vault roots you configure and answers only from them, with citations. |
| **Mail** | IMAP ingestion, deterministic threads, bounded analysis, local reply drafts, explicitly approved SMTP delivery. |
| **Observation** | Configured public HTTPS watchers, plus manual and QQ-forwarded input. |
| **Actions** | Exact `ActionRequest` → human `Approval` → `ExecutionRun`, bound to an immutable payload fingerprint. |
| **eHall** | One narrow, approved NJU certificate workflow — no generic browser automation. |
| **Mobile** | A trusted-LAN surface for review and approval. |
| **Learning** | `Correction` → `FactCandidate` → `ConfirmedFact` confirmation, and reviewed non-executing playbooks. |
| **MCP** | A controlled local stdio integration for VS Code. |
| **Operations** | Integrity checks, backup, verify and staging restore. |

## Safety by Design

```text
ActionRequest
→ exact payload fingerprint
→ human Approval
→ ExecutionRun
```

- The model cannot approve external actions, and neither can a background worker: no service has a
  path to creating an approval.
- An approval is bound to one exact payload, is single-use, and is consumed before execution begins.
- Changing the payload requires a new `ActionRequest` and a new `Approval`.
- Ambiguous external results are never blindly retried; they stay unresolved until a human resolves
  them.
- Mobile can approve but cannot execute, and eHall submissions need the same approval chain as mail.
- Playbooks are reviewed references rather than executable workflows, and confirmed facts do not
  auto-fill anything in v1.

The full boundaries are in [docs/specs/0001-system-design.md](docs/specs/0001-system-design.md) and
the [architecture decision records](docs/adr/).

## Quick Start

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), and Linux or WSL.

```bash
git clone https://github.com/Mr-tree013/Rings.git
cd Rings

uv sync --frozen

mkdir -p ~/.config/growing-assistant
cp docs/examples/config.toml \
  ~/.config/growing-assistant/config.toml

uv run pw doctor
uv run pw integrity check
uv run assistantd
```

In a second terminal:

```bash
uv run rings
uv run pw status      # the advanced surface: a status overview
```

The everyday entry point is a conversation, not a command:

```text
Tree > Hello. I am Tree. What are we arranging today?
You > remind me to submit the SE report tomorrow at 3pm
Tree > Created the task "submit the SE report", due 2026-09-21 15:00 (+08:00).
You > plan my week
Tree > A weekly proposal is ready (3 blocks). Apply it? Reply yes and I will write it down.
You > yes
Tree > Applied the proposal: 3 blocks created, 0 replaced.
```

Tree handles local daily work in natural language: tasks, calendar, work sessions, weekly planning
and questions about your indexed sources. External effects are **not** reachable from the
conversation; they still go through the `pw` approval chain.

| Entry point | What it is for |
| --- | --- |
| `uv run rings` | The conversational surface, for everyday use |
| `uv run pw chat` | The same runtime, from the existing CLI |
| `pw …` | The advanced/admin surface: the full command set |

`assistantd` keeps running the background work (indexing, reminders, mail, watchers, mobile), and
`growing-assistant-mcp` keeps serving the editor.

The sample configuration is inert until you change it: mail, eHall, mobile and MCP are off and no
watcher is configured. Credentials are supplied through environment variables, not committed to
config: `DEEPSEEK_API_KEY`, `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD` and
`GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD`. `uv run pw mail accounts` and
`uv run pw model status` report what the host can see without printing a secret.

Full setup, every configuration field, credential rules and WSL startup:
[docs/guides/getting-started.md](docs/guides/getting-started.md).

## Everyday Examples

Create a task and see your list:

```bash
uv run pw task add "Write the SE lab report" --estimate 300 --deadline 2026-10-20T23:59:00+08:00
uv run pw tasks
```

Forward something you received elsewhere:

```bash
uv run pw ingest text "Forwarded notice..." --source qq-forward
```

Check and sync mail:

```bash
uv run pw mail status
uv run pw mail sync
```

See what a watched page changed:

```bash
uv run pw watch observations
```

Check the system and take a backup:

```bash
uv run pw status
uv run pw integrity check
uv run pw backup create ~/assistant-backup.gab
uv run pw backup verify ~/assistant-backup.gab
```

The full mail approval chain, eHall, mobile pairing, facts and playbooks are in the guides below.

## Documentation

| Topic | Where |
| --- | --- |
| The conversational entry point: Tree Conversation | [docs/guides/conversation.md](docs/guides/conversation.md) |
| Getting started, configuration, credentials, WSL startup | [docs/guides/getting-started.md](docs/guides/getting-started.md) |
| The Rings / Tree vocabulary | [docs/concepts/rings-language.md](docs/concepts/rings-language.md) |
| Tasks, calendar, work sessions, weekly planning | [docs/guides/planning.md](docs/guides/planning.md) |
| Knowledge indexing and grounded answers | [docs/guides/knowledge.md](docs/guides/knowledge.md) |
| Mail: sync, analysis, drafts, approved sending | [docs/guides/mail.md](docs/guides/mail.md) |
| eHall certificate application | [docs/guides/ehall.md](docs/guides/ehall.md) |
| Mobile control plane | [docs/guides/mobile.md](docs/guides/mobile.md) |
| Watchers and manual / QQ-forwarded input | [docs/guides/watchers-and-manual-input.md](docs/guides/watchers-and-manual-input.md) |
| Facts and playbooks | [docs/guides/learning.md](docs/guides/learning.md) |
| MCP / VS Code | [docs/guides/mcp.md](docs/guides/mcp.md) |
| Integrity, backup and recovery | [docs/guides/backup-and-recovery.md](docs/guides/backup-and-recovery.md) |
| Upgrading an older runtime | [docs/upgrade-to-v1.md](docs/upgrade-to-v1.md) |
| System architecture | [docs/specs/0001-system-design.md](docs/specs/0001-system-design.md) |
| Architecture decisions | [docs/adr/](docs/adr/) |
| Release notes (current release: v1.0.0) | [docs/releases/1.0.0.md](docs/releases/1.0.0.md) |
| Contributing rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |

## Architecture at a Glance

```text
Interaction
    ↓
Observation
    ↓
Intelligence
    ↓
Domain
    ↓
Execution
    ↓
Data
```

Modular Monolith · Event Driven · Ports & Adapters · SQLite durable state · Explicit state machines.

One daemon (assistantd) supervises isolated services; the CLI, the LAN mobile page and the local MCP
server are interaction surfaces over the same durable state. Model provider, mail transport,
browser, web surface and clock sit behind ports, and the model port has no tool calls, no filesystem
and no database.

Rings is the public project name; the v1 compatibility surface keeps its historical identifiers —
the Python package is still `assistant`, the distribution is still `growing-assistant`, and the
`pw`, `assistantd` and `growing-assistant-mcp` entry points, the XDG `growing-assistant` directories
and the `GROWING_ASSISTANT_*` environment variables are unchanged.

Read more: [docs/specs/0001-system-design.md](docs/specs/0001-system-design.md) and
[docs/adr/](docs/adr/).

## Known Limitations

- Mobile is a trusted-LAN HTTP page, not a public Internet service.
- SMTP is not exactly-once: an interrupted send becomes `UNKNOWN`.
- eHall page changes fail closed instead of adapting.
- eHall `UNKNOWN` requires manual inspection.
- Watchers observe public, unauthenticated HTTPS pages only.
- Knowledge retrieval is local FTS5, not a vector database.
- Confirmed facts do not auto-fill anything in v1.
- Playbooks do not execute.
- MCP is local stdio only, and read-only by default.
- A backup excludes credentials and the eHall browser session.
- Linux / WSL is the reference runtime.
- There is no database downgrade.

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Tests are network-free by design: an autouse guard fails any attempt to open a socket.

Every pull request and push to main runs the repository quality gates in GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

No license has been selected yet. Until one is added, this repository is not licensed for reuse.
