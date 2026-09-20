# Rings

A local-first personal operations system that grows with you.

[![CI](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml/badge.svg)](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml)

让每天的叶子，长成年轮。

**Version:** 1.0.0
**Reference environment:** Linux / WSL + Python 3.13

The system runs on your machine. Its durable state is one SQLite database plus the immutable objects
that database references; credentials live in environment variables and never in configuration files
or in a backup; and nothing that changes the outside world happens until you approve one exact,
fingerprinted action.

It is not a general computer agent. There is no shell, no filesystem control, no generic browser and
no generic HTTP client in it, and the high-risk university errands are absent from its capability set
rather than forbidden by a prompt.

## The Tree Model

Rings grows in rings. The words below are the product language for that idea; the technical
architecture, and the frozen domain names behind it, are unchanged. The full vocabulary lives in
[docs/concepts/rings-language.md](docs/concepts/rings-language.md).

| Word | Meaning |
| --- | --- |
| **Roots** | Long-lived, traceable personal sources of truth: your knowledge roots, the provenance of every correction, and the confirmed facts they produced. |
| **Seeds** | The intentions and commitments you plant deliberately — tasks, deadlines, calendar events and planning intent. |
| **Branches** | The controlled capability areas the Tree can call on (mail, planning, knowledge, watchers, execution, learning, operations, mobile, MCP). Branches are capability modules, not autonomous sub-agents. |
| **Leaves** | What arrives each day: mail, observations, pasted or forwarded text, and the events they become. A Leaf may be interpreted, but it never becomes a commitment or an action by itself. |
| **Rings** | The growth record that accumulates over time: work sessions, executions, fact history, reviewed playbooks and proposals. |
| **Tree** | The coordinator you talk to, through the CLI, the LAN mobile page, the local MCP server and future conversational surfaces. It is not an unrestricted autonomous agent. |

The Tree coordinates Roots, Seeds, Branches, Leaves and Rings, and never bypasses an `ActionRequest`,
a human `Approval`, an `ExecutionRun`, fact confirmation, playbook review or the capability registry.

Rings is the public project name. Some v1 command, package and runtime identifiers retain the
historical `growing-assistant` naming for compatibility: the Python package is still `assistant`, the
distribution is still `growing-assistant`, and the `pw`, `assistantd` and `growing-assistant-mcp`
entry points, the XDG directories and the `GROWING_ASSISTANT_*` environment variables are unchanged.

## Product model

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

| Stage | What happens |
| --- | --- |
| Observe | Read-only IMAP receive, configured public HTTPS pages, and text you paste or forward. Nothing is fetched from an unconfigured source. |
| Understand | Deterministic parsing and threading, plus bounded model analysis that produces candidates only — never a durable mutation. |
| Commit | Tasks, deadlines, calendar events and work sessions are recorded as distinct domain concepts. |
| Plan | A deterministic planner writes a reviewable weekly proposal; reminders and rolling replans are durable scheduled jobs. |
| Execute | A `Case` holds prepared `ActionRequest`s. Execution happens only after one exact human `Approval`, and only for a registered capability. |
| Review | Every challenge, approval and execution is durable history. An ambiguous external result stays `UNKNOWN` instead of being retried. |
| Learn | Corrections become fact candidates until you confirm them; successful runs become playbook candidates until a dry run is replayed and you promote them. |

**Chat, the CLI, the mobile page and MCP are interaction surfaces. Durable local state is
authoritative.** No surface owns state; every surface reads and writes through the same application
services, and closing one changes nothing about what the system knows.

## Core capabilities

| Capability | What v1 does |
| --- | --- |
| Tasks / deadlines / calendar / work sessions | Five distinct concepts (`Task`, `Deadline`, `CalendarEvent`, `PlanBlock`, `WorkSession`) with a structured CLI; deadlines never occupy calendar time, and actual effort comes only from work sessions. |
| Deterministic weekly planning | `pw plan week` writes a durable `PlanProposal` you review and apply. The planner is code, not a model, and applying is always your explicit action. |
| Durable scheduler / reminders | Reminder and rolling-replan intent lives in runtime SQLite and is recovered after a restart; timers are only a wake-up mechanism. |
| Local knowledge indexing + source-grounded answers | Configured local and vault roots are catalogued and indexed into per-root FTS5 indexes; `pw ask` answers only from that evidence, with citations resolved locally. |
| IMAP mail ingestion | Incremental, credential-from-environment, read-only sync that never marks a message as read and keeps raw RFC822 content-addressed under the runtime data directory. |
| Mail threading / classification | Threading is deterministic code; each message gets at most one durable analysis whose deadline and event-start candidates stay distinct. |
| Reply drafts | `pw mail draft create` writes a local reply draft. Recipients and subjects come from local data, never from the model. |
| Explicit knowledge-assisted drafts | Personal knowledge is used only when you pass an explicit `--context-query`; mail content alone can never trigger retrieval. |
| Exact `ActionRequest` | Immutable JSON payloads with a SHA-256 fingerprint that is recomputed at load and at execution time. |
| Human `Approval` | Minted by a single-use, short-lived challenge token that is stored only as a hash and bound to one exact action fingerprint. |
| `ExecutionRun` | Every attempt is a durable record with an explicit terminal status; a failure after the effect may be ambiguous and is recorded as such. |
| Approved SMTP delivery | `mail.send` is one of the two production capabilities: TLS-only, sent from the approved payload snapshot, with a Message-ID fixed before approval. |
| Ambiguous-result reconciliation | `pw mail send reconcile` looks for the exact Message-ID in the Sent mailbox and never resends. |
| Approved NJU eHall certificate application | `ehall.submit-certificate` is the only eHall capability: a typed pipeline with a page contract that fails closed. |
| LAN mobile control plane | A trusted-LAN HTTP page for review and approval, off by default, with hashed single-use pairing tokens and no execution endpoint. |
| Configured public HTTPS watchers | Fixed URLs from configuration only, no redirects, no JavaScript, no credentials, no automatic task creation. |
| Manual / QQ-forwarded observations | `pw ingest text` stores pasted or forwarded text durably and queues it for bounded analysis. |
| Human-confirmed personal facts | `Correction` → `FactCandidate` → `ConfirmedFact`, where only an explicit CLI confirmation makes a fact trusted. |
| Reviewed non-executing Playbooks | A successful run can be named as a candidate, dry-run replayed, and promoted into a reference blueprint that does not execute. |
| Controlled local MCP / VS Code integration | A local stdio server that exposes read-only resources by default, with optional bounded task writes and optional bounded knowledge excerpts. |
| Integrity checking | `pw integrity check` audits the runtime database, its content objects and the configured roots without repairing anything. |
| Backup / verify / staging restore | Consistent `.gab` archives, read-only verification, and a restore that only ever writes into a new empty staging directory. |

## Safety model

External side effects are never authorized by the model. Every production external effect requires:

```text
ActionRequest
→ exact payload SHA-256 fingerprint
→ explicit human Approval
→ ExecutionRun
```

- The approval is bound to one exact payload. **Changing the payload requires a new `ActionRequest`
  and a new `Approval`** — there is no edit path for an existing request, and the fingerprint is
  recomputed rather than trusted.
- The challenge token is single-use, expires after 10 minutes, and is stored only as a SHA-256 hash.
  Plaintext is printed once by `pw action challenge`, never again, and never into a log.
- An approval is consumed in the same transaction that creates the `RUNNING` `ExecutionRun`, so it can
  never authorize a second execution. A `FAILED` attempt also spends it.
- **Ambiguous external outcomes are never blindly retried.** `UNKNOWN`, and a `RUNNING` run left by a
  crash, block further execution of that action until a human resolves it.
- Capability checks run before an approval is consumed. If this deployment cannot execute the action
  type, `pw action execute` reports `CapabilityUnavailable` and the approval stays unused.

Who can do what:

| Actor | Approve | Execute |
| --- | --- | --- |
| Model / interpreter / grounded answers | No | No |
| EventWorker | No | No |
| Scheduler | No | No |
| Mobile web | Yes, for one exact action | No — there is no execute route |
| MCP / VS Code | No | No |
| Playbooks | No | No — a promoted playbook is a reference blueprint |
| `pw action` (you, on the host) | Yes, with a challenge token | Yes, only for a registered capability |

Only two production capabilities exist: `mail.send` and `ehall.submit-certificate`. Generic shell,
generic HTTP, generic browser automation, and destructive university errands such as course
withdrawal or dorm checkout do not exist in the executor set.

## Installation

Requirements:

- **Python 3.13** (`requires-python = ">=3.13,<3.14"`; the reference runtime is 3.13)
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **Linux or WSL**. Windows-native execution of the daemon is not a v1 reference target.

```bash
git clone https://github.com/Mr-tree013/Rings.git
cd Rings

uv sync --frozen
```

`uv sync --frozen` installs exactly the versions in `uv.lock` and does not update the lock file.

Playwright Chromium is **not** assumed to be present. It is only needed for the eHall pipeline; see
[eHall](#ehall-approved-certificate-application) for the one-time install command.

## Configuration

Start from the sample configuration, which is safe by default and contains no secrets:

```bash
mkdir -p ~/.config/growing-assistant

cp docs/examples/config.toml \
  ~/.config/growing-assistant/config.toml
```

The sample is deliberately inert until you change it:

- no mail accounts — mail disabled
- no watcher targets — watchers empty
- `[ehall] enabled = false`
- `[mobile] enabled = false`
- `[mcp] enabled = false`, `write_scope = "none"`, `expose_knowledge = false`

It does configure one example knowledge root and a planning timezone, because those are the parts
that are harmless to switch on. Edit the paths before using them:

```toml
format_version = 1

[[storage.roots]]
kind = "local"
id = "documents"
label = "Documents"
path = "/home/user/Documents"
enabled = true

[planning]
timezone = "Asia/Shanghai"
```

The strict parser **rejects** credential-shaped keys: a `password`, `secret` or `api_key` key in
`config.toml` is an error, not an override. Nothing in a configuration file is treated as a secret.

Where the runtime keeps things:

```text
~/.local/share/growing-assistant/   runtime state (SQLite, cursors, jobs, audit rows)
~/.config/growing-assistant/        configuration
~/.cache/growing-assistant/         derived knowledge indexes for local roots
```

Each location follows `XDG_DATA_HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME`. Your documents never
move: a vault is referenced where it is, and an unplugged vault is reported as offline rather than as
corruption.

## Credentials

**All credentials are environment variables.** Do not put them in `config.toml`, do not commit an
`.env` that holds them, and do not expect a backup to contain them.

| Credential | Environment variable |
| --- | --- |
| Model provider API key | `DEEPSEEK_API_KEY` |
| IMAP password for account `<ACCOUNT_ID>` | `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD` |
| SMTP password for account `<ACCOUNT_ID>` | `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD` |

`<ACCOUNT_ID>` is the `id` you gave the account in `[[mail.accounts]]`, upper-cased with hyphens
turned into underscores: an account with `id = "smail"` reads
`GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD` and `GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD`. The IMAP
and SMTP passwords are separate: receiving mail does not require the sending credential, and
preparing a send requires neither.

```bash
export DEEPSEEK_API_KEY='...'
export GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD='...'
```

Check what the host sees without printing a secret:

```bash
uv run pw mail accounts     # whether a credential is present, never its value
uv run pw model status      # configured model boundary; never contacts the provider
uv run pw ehall status      # eHall capability state; never contacts the server
```

The eHall login is different in kind: a university password exists nowhere in this project. You log
in by hand in a headed browser, and the session cookie stays in a private browser profile under the
runtime data directory.

## First run

```bash
uv run pw doctor
uv run pw integrity check
uv run assistantd
```

In a second terminal:

```bash
uv run pw status
uv run pw daemon status
```

`pw doctor` is a read-only environment check, and `pw integrity check` audits the runtime database,
its content objects and the configured roots without creating or repairing anything. Neither needs
the daemon to be running.

**`assistantd` is single-instance per runtime data root.** It takes an advisory `flock` on
`assistantd.lock` inside the runtime data directory; a second instance exits before starting any
service and says why. The kernel releases the lock when the process ends, so a lock file left behind
by a power cut does not block the next start and nothing has to be deleted by hand. The pid recorded
in that file is diagnostic only — `pw daemon status` reports the lock, not the file.

The daemon supervises independent services, each with its own failure boundary:

| Service | Starts when | What it does |
| --- | --- | --- |
| `index-sync` | always | reconciles configured roots (scan → catalog → index), once at startup and then on the configured interval |
| `scheduler` | always | executes durable reminders and debounced rolling replans |
| `mail-sync` | at least one mail account is configured | receives mail incrementally |
| `event-worker` | mail and a usable model are both available | claims and analyzes events; without a model nothing is lost, events simply stay `RECEIVED` |
| `web-watch` | at least one watcher target is enabled | polls the configured public pages |
| `mobile-web` | `[mobile] enabled = true` | serves the LAN control plane |

A root that is offline, whose identity does not match, or whose index is corrupt affects only that
root. A failure of the host runtime database is the only kind that is escalated to the supervisor.

## Tasks, planning and actual work

Five concepts stay separate: `Task` (what to do), `Deadline` (by when), `CalendarEvent` (time already
occupied), `PlanBlock` (planned time for a task) and `WorkSession` (time actually spent). **A
deadline does not occupy calendar time, and a plan block is never treated as actual work.**

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

uv run pw plan add <TASK> \
  --start 2026-09-23T19:00:00+08:00 \
  --end 2026-09-23T21:00:00+08:00

uv run pw work add <TASK> \
  --start 2026-09-22T20:00:00+08:00 \
  --end 2026-09-22T21:00:00+08:00

uv run pw work list <TASK>           # exact seconds of actual effort
```

Planning is proposal-based, and the planner is deterministic code:

```bash
uv run pw plan week                  # writes a PENDING proposal; never applies it
uv run pw plan week --next
uv run pw plan proposals
uv run pw plan show <PROPOSAL>       # exactly what was proposed, with no re-planning
uv run pw plan apply <PROPOSAL>      # the only path that writes plan blocks
uv run pw plan cancel <BLOCK>
```

Applying a proposal validates the commitment revision it was planned against, so a stale proposal is
marked `STALE` and writes nothing. Only planner-origin blocks inside the proposal window are
replaced; blocks you added by hand are yours and are left alone.

Reminders and rolling replans are durable jobs, not in-process timers:

```bash
uv run pw notifications              # durable inbox
uv run pw notifications show <NOTIFICATION>
uv run pw notifications read <NOTIFICATION>
uv run pw scheduled                  # job visibility, read-only
uv run pw scheduled --all
```

Rolling replanning can create a new `PENDING` proposal and a `PLAN_READY` notification. It never
applies one.

Identifiers accept a full UUID or a unique prefix; an ambiguous prefix is refused rather than
guessed. Every time argument must be ISO 8601 with an explicit UTC offset — `tomorrow` and
`next Friday` belong to the interpreter, not to the structured CLI.

## Knowledge

Configure the roots you want indexed, then let the daemon keep them fresh:

```toml
[[storage.roots]]
kind = "local"
id = "university"
label = "University documents"
path = "/home/user/Documents/University"
enabled = true
```

```bash
uv run pw roots list                 # configuration only; scans nothing
uv run pw sync                       # one reconciliation: scan → catalog → index
uv run pw sync --root university --force-index
uv run pw reindex --root university
uv run pw search "important deadline"
uv run pw search "deadline" --root university --limit 5
```

An archive vault lives on removable storage and carries its own identity:

```bash
uv run pw vault init /mnt/e/archive --id archive-main --label "Personal Archive"
uv run pw vault status /mnt/e/archive
uv run pw vault scan /mnt/e/archive
```

Answers are grounded, or they are absent:

```bash
uv run pw ask "What is the deadline for my SE lab?"
uv run pw ask "When is my SE lab due?" --root university --limit 6
```

```text
# Answer
# The submission deadline is October 23 at 23:59. [S1]
#
# Sources
# [S1] local://university/notice.md - lines 18-31
```

- **v1 uses local FTS5 indexes, not a vector database.** Retrieval is term-based: it finds the words
  that are there and shows where they came from. It does not understand meaning or paraphrase.
- **Answers do not fall back to unsourced world knowledge.** With no evidence the model is not called
  at all; with insufficient evidence the answer is `insufficient_evidence` rather than a
  general-knowledge guess.
- Every content hit carries a source span — `page N` or `lines A-B`. A metadata-only hit (a file name
  or path) is not evidence for a content claim, and an offline vault cannot be cited.
- Source URIs and spans come from local evidence metadata, never from model output. A citation to an
  identifier that was not supplied in that exact request is rejected deterministically.
- Indexes are derived data. The originals stay authoritative, and any index can be rebuilt with
  `pw reindex`.

## Mail

Inbound mail is receive-only and read-only with respect to the server:

```bash
uv run pw mail accounts
uv run pw mail sync
uv run pw mail sync --account smail
uv run pw mail status
uv run pw mail messages
uv run pw mail show <MESSAGE>
```

Each message then has a deterministic place in a thread and at most one stored analysis:

```bash
uv run pw mail threads
uv run pw mail thread show <THREAD>
uv run pw mail analysis <MESSAGE>
```

- A message's identity is `(account, mailbox, UIDVALIDITY, UID)`; a UID alone is never a durable
  identity. A cursor only advances to UIDs that are already durably stored, so a crash replays rather
  than skips.
- Threading is code, not a model: the parent is resolved from `In-Reply-To` and then `References`,
  only within the same account, and only when it matches exactly one stored message. A duplicate
  `Message-ID` is `AMBIGUOUS` and is never guessed.
- Sync uses a read-only mailbox select and `BODY.PEEK`, so your mail is never marked as read.
- Classification produces candidates only. `DEADLINE` (by when) and `EVENT_START` (when it happens)
  are different values and are never collapsed into one date field.
- A stored analysis is reused across worker retries when its input fingerprint is unchanged, so a
  retry does not pay for the same answer twice.

The full path from received mail to a message that actually leaves your outbox:

```text
mail sync
  ↓
thread / analysis
  ↓
draft
  ↓
Case
  ↓
mail.send Action
  ↓
challenge
  ↓
Approval
  ↓
execute
```

Drafting is explicit and local. Nothing in the background ever creates a draft:

```bash
uv run pw mail draft create <MESSAGE>
uv run pw mail draft create <MESSAGE> --context-query "my office hours" --root university
uv run pw mail drafts
uv run pw mail draft show <DRAFT>
uv run pw mail draft edit <DRAFT> --subject "Re: lab slot" --body "..."
uv run pw mail draft acknowledge <DRAFT>
```

- The recipient comes from `Reply-To` (falling back to `From`) and the subject is derived by a pure
  local function. The draft schema has no `to`, `cc`, `bcc` or `subject` field, so the model can only
  write the body.
- Personal knowledge is read only when you pass `--context-query`. Without it the knowledge search is
  never called, and an instruction inside a mail body cannot make it start.
- Personal facts the sources do not support must be listed as `needs_user_input` instead of being
  invented.
- Editing is optimistically concurrent (`UPDATE … WHERE id = ? AND version = ?`); a stale edit fails
  instead of overwriting.

Preparing and approving the send, as a case:

```bash
uv run pw case add "Reply to supervisor"
uv run pw cases
uv run pw case show <CASE>

uv run pw mail send prepare <DRAFT> --case <CASE>
uv run pw mail sends
uv run pw mail send show <ACTION>

uv run pw actions
uv run pw action show <ACTION>
uv run pw action challenge <ACTION>
uv run pw action approve <ACTION> <TOKEN>
uv run pw action execute <ACTION>
uv run pw action cancel <ACTION>

uv run pw case done <CASE>
uv run pw case cancel <CASE>
```

`prepare` freezes that draft version — sender, recipient, subject, body, date, reply headers and
Message-ID — into an immutable payload. The bytes sent come only from that payload; the draft is
never re-read at execution time. Editing the draft afterwards affects only a future action, and
`pw mail send show` warns you when the draft has moved on.

## SMTP: `UNKNOWN` is not a retry

**SMTP cannot guarantee exactly-once delivery.** A connection that drops after the message data was
accepted leaves both sides unsure whether the message was delivered, and no local code can settle
that by trying again.

```text
ExecutionRun UNKNOWN  →  DO NOT blindly resend
```

```bash
uv run pw mail send show <ACTION>
uv run pw mail send reconcile <ACTION>
```

`reconcile` searches the Sent mailbox for the one exact Message-ID that was fixed before approval,
and reports one of:

| Result | Meaning |
| --- | --- |
| `FOUND` | Exactly one header-identical match; the run is promoted to `SUCCEEDED` in one transaction. |
| `NOT_FOUND` | **This does not prove the message was not sent.** Only an audit row is recorded; the execution status is unchanged. |
| `AMBIGUOUS` | Two or more exact matches. No UID is chosen. |
| `UNAVAILABLE` | The Sent mailbox could not be read. The status is unchanged. |

There is no resend command. The only way to send again is to cancel the action, move the draft
forward, prepare a new action, and approve it as a human.

Failures are classified by where they happened: authentication, sender rejection or recipient
rejection before the message data is accepted is a definite `FAILED` (and the approval is already
spent), while anything unclear afterwards is `UNKNOWN`.

## eHall: approved certificate application

Only one production eHall workflow exists: the NJU certificate application
(`ehall.submit-certificate`). There is no generic browser automation, and no course-drop, withdrawal,
cancellation, deletion or dorm-checkout capability — those do not exist in the code.

One-time browser runtime install (this downloads Chromium; it does not download a model):

```bash
uv run playwright install chromium
```

Log in by hand. The project never asks for, types or stores a university password:

```bash
uv run pw ehall status
uv run pw ehall login
```

Inspect, prepare, then use the same approval chain as everything else:

```bash
uv run pw ehall certificate inspect

uv run pw case add "Apply for enrollment certificate"
uv run pw ehall certificate prepare --case <CASE> \
  --field applicant-name=张三 \
  --field certificate-type=在读证明

uv run pw ehall certificate show <ACTION>
uv run pw action show <ACTION>
uv run pw action challenge <ACTION>
uv run pw action approve <ACTION> <TOKEN>
uv run pw action execute <ACTION>
```

- `inspect` reads the live form and submits nothing. `prepare` validates locally and freezes the
  service identity, the ordered field definitions and their options, the required materials, the
  submit control and the values you supplied. It does not fill the page, so a page autosave cannot
  happen before you approve anything.
- Every writable value comes from `--field KEY=VALUE`. No knowledge base, mail analysis, model or
  confirmed fact participates in filling the form.
- **A page-contract change invalidates the prepared action.** Execution re-inspects first and requires
  an identical fingerprint; a mismatch raises `EHallPageChanged` and fails closed, typing nothing and
  submitting nothing.
- The submit click happens once. A failure before it is a clear `FAILED`; anything unclear after it is
  `UNKNOWN`, which blocks retries of that action and asks you to check eHall by hand.
- The browser only runs when you run a CLI command. No daemon service can open one.

## Mobile

The phone page is a trusted-LAN control plane for reviewing and approving. It is off by default and
it is not an Internet service.

```toml
[mobile]
enabled = true
bind = "lan"
port = 8765
```

```bash
uv run assistantd
uv run pw mobile pair
```

Open `http://<LAN-IP>:8765/pair` on the phone and paste the pairing code by hand.

```bash
uv run pw mobile status
uv run pw mobile sessions
uv run pw mobile revoke <SESSION>
uv run pw mobile approval-link <ACTION>
```

- **Trusted LAN only, HTTP, not public.** `bind = "lan"` listens on `0.0.0.0`, but the real gate is a
  private-client check on the socket peer: loopback, private and link-local addresses pass, anything
  else is refused. `X-Forwarded-For`, `Forwarded` and `X-Real-IP` are never trusted, so a spoofed
  header cannot get in. There is no cloud relay, no VPN and no third-party login, and the session
  cookie does not pretend to be `Secure` on plain HTTP.
- **Pairing tokens are single-use and stored as hashes.** The code is printed once by
  `pw mobile pair` and is never placed in a URL. Sessions last 30 days and can be revoked.
- **Mobile may approve but cannot execute.** The approval link carries its token in the URL fragment,
  which the page strips from the address bar as soon as it loads. There is no execute, send, submit,
  retry or resend route; `pw action execute` exists only on the host.
- Mutations require an authenticated session cookie plus a matching CSRF header and cookie. The page
  is self-contained — no CDN and no third-party script — and all user content is written with
  `textContent`.

## Watchers, manual input and QQ forwards

Watchers observe fixed public pages. The URL comes from configuration, never from the model and never
from a command argument:

```toml
[watchers]
poll_interval_seconds = 300
timeout_seconds = 20
max_response_bytes = 2097152
full_fetch_every = 24

[[watchers.web]]
id = "course-notices"
url = "https://example.edu/notices"
enabled = true
```

```bash
uv run pw watch targets
uv run pw watch sync
uv run pw watch sync --target course-notices
uv run pw watch status
uv run pw watch observations
uv run pw watch observation show <OBSERVATION>
```

Text you paste or forward travels the same bounded analysis path:

```bash
uv run pw ingest text "Forwarded notice..." --source qq-forward
uv run pw ingest list
uv run pw ingest show <INPUT>
```

- **Public, unauthenticated HTTPS only.** A target URL must be `https://` with no userinfo, no
  IP-literal host and no explicit port, and every resolved address must be public.
- **No redirects, no JavaScript, no credentials.** A redirect is an error rather than something to
  follow automatically, and the page is parsed as text rather than rendered in a browser.
- **No automatic task creation.** A changed page or a pasted note produces a durable observation and
  an event; analysis writes candidates only. Nothing becomes a `Task`, `Case`, `Action`, `Approval`,
  fact or playbook on its own.
- The first fetch is only a baseline, so enabling a watcher does not treat the whole site as new. A
  periodic full fetch makes sure a server that always answers `304` cannot hide a change.

## Personal facts

Facts begin as proposals and become facts only because you said so:

```text
Correction
  ↓
FactCandidate
  ↓
explicit human confirmation
  ↓
ConfirmedFact
```

```bash
uv run pw correction add "My office moved to Room 302"
uv run pw corrections
uv run pw correction show <CORRECTION>

uv run pw fact candidate add profile.office "Room 302" \
  --note "My office is Room 302 in the CS building"
uv run pw fact candidates
uv run pw fact candidate show <CANDIDATE>
uv run pw fact candidate confirm <CANDIDATE>
uv run pw fact candidate reject <CANDIDATE>

uv run pw facts
uv run pw facts --all
uv run pw fact show <FACT>
```

- A candidate is not a fact. It cannot be used to fill anything in, it is not part of any model
  context, and no consumer reads it. Only a `ConfirmedFact` can be considered by future logic, and
  only while it is active and unexpired.
- Only you can confirm. The single entry point is `pw fact candidate confirm`, which the CLI reaches
  through the learning service. No model and no background worker has a path to it, and there is no
  `--force` or `--confirm-all`.
- Provenance is durable: every candidate cites the correction that produced it, stored in your own
  words, and history is append-only. Confirming a new value for the same key supersedes the old row in
  the same transaction rather than deleting it.
- Expiry is derived at read time. `--valid-until` takes an aware ISO 8601 timestamp; nothing is
  scheduled and no background job mutates the fact store.
- The fact store is not a credential store. Keys containing credential-like segments (`password`,
  `secret`, `token`, `credential`, `api_key`, `private_key`) are rejected, and values are never
  scanned in the hope of spotting something that looks like a password.
- **ConfirmedFacts are NOT auto-filled in v1.** They are stored, reviewed and queried, but they are
  not injected into eHall forms, mail drafts, the interpreter or grounded answers.

## Playbooks

A playbook records that something worked. It does not repeat it:

```text
successful Action
  ↓
explicit PlaybookCandidate
  ↓
side-effect-free dry-run
  ↓
explicit promotion
  ↓
Playbook
```

```bash
uv run pw playbook candidate add <ACTION> \
  --name "Approved certificate workflow" \
  --note "Reviewed successful run"
uv run pw playbook candidates
uv run pw playbook candidate show <CANDIDATE>
uv run pw playbook candidate test <CANDIDATE>
uv run pw playbook candidate promote <CANDIDATE>
uv run pw playbook candidate reject <CANDIDATE>

uv run pw playbooks
uv run pw playbooks --all
uv run pw playbook show <PLAYBOOK>
uv run pw playbook retire <PLAYBOOK>
```

- A successful execution never becomes a playbook by itself. The candidate table only grows when you
  run `pw playbook candidate add`, at most once per source action.
- The source must be an unambiguous success: the action is `EXECUTED`, the run is `SUCCEEDED` and
  finished, and the payload fingerprint is re-verified. `FAILED`, `UNKNOWN`, `RUNNING` and
  never-executed actions are refused.
- The dry run is local parsing only. It re-reads the historical payload with the current strict parser
  and does not read credentials, open a socket, open a browser, check Sent, generate a new Message-ID
  or call an executor. It passes even with no password, no browser and eHall disabled.
- `PASS` means the current code still understands that payload. It does not mean the credential is
  still valid, the remote page is unchanged, or the action is worth doing again.
- **Playbooks DO NOT execute.** Promotion requires a passing dry run under the current replay contract
  version and an explicit human command; nothing may be parameterised, instantiated or replayed into
  a new action. A playbook contains no approval and does not bypass one.

## MCP / VS Code

The MCP server is a local stdio adapter for your editor. Using it requires no daemon.

```toml
[mcp]
enabled = false
write_scope = "none"
expose_knowledge = false
```

```bash
uv run pw mcp status
uv run pw mcp vscode-config
uv run pw mcp vscode-config --project-root /path/to/project
```

Paste the printed snippet into your VS Code MCP configuration (a workspace or user `mcp.json`), then
start and trust the server and inspect the read-only surface first.

- **Local stdio only.** The server is spawned by the editor, is not hosted in `assistantd`, and
  listens on no port. stdout carries protocol messages and all logging goes to stderr.
- **Read-only by default.** With `write_scope = "none"` the surface is four resources (status, open
  tasks, open cases, current plan) and two read-only tools. Cases expose lifecycle fields only, not
  their actions.
- **Optional task writes** are registered only when you set `write_scope = "tasks"`, and they call the
  same task service as the CLI. When they are not enabled the tools do not exist at all.
- **Optional bounded knowledge exposure** requires `expose_knowledge = true`, and returns logical
  URIs, source spans and bounded excerpts through local full-text search — never physical paths and
  never a whole document.
- **No approval and no execution.** MCP cannot create an approval, execute an action, send mail,
  submit eHall, confirm a fact, promote a playbook, read arbitrary files, run a shell, browse, or make
  arbitrary HTTP requests. When it is disabled, the server explains why on stderr and exits.

## Integrity, backup and restore

The runtime authority is one SQLite database plus the immutable objects it references (raw mail and
web snapshots). A backup takes a consistent copy of exactly those.

```bash
uv run pw integrity check

uv run pw backup create ~/assistant-backup.gab
uv run pw backup verify ~/assistant-backup.gab
uv run pw backup inspect ~/assistant-backup.gab

uv run pw backup restore \
  ~/assistant-backup.gab \
  --to ~/restored-assistant-data
```

- `create` uses the SQLite backup API, so it never copies a live database file or its `-wal`/`-shm`
  sidecars. Referenced objects are re-hashed: a missing or corrupt object fails the whole backup
  instead of producing a "successful" archive that is short of content.
- The archive shape is fixed: a manifest carrying identities and hashes, the runtime database, raw
  mail, and web snapshots. Nothing else is a member.
- `verify` and `inspect` write nothing. Verification checks member names, the manifest, per-member
  sizes and hashes, the database's own integrity and foreign keys, and migration compatibility.
- **Restore is staging-only.** `--to` must be an absent or empty directory that is not the live
  runtime (or a parent or child of it). There is no in-place restore and no `--force`. Content is
  written to a temporary sibling directory and moved into place only after verification passes.
- Restoring invalidates outstanding authorization: unconsumed challenges, valid approvals, unused
  pairing codes and all mobile sessions are invalidated, and you pair the phone again. Any
  `RUNNING`/`UNKNOWN` execution is preserved — a restore is not reconciliation and does not claim
  that an external effect was rolled back.
- Exit codes distinguish the two failure kinds: `0` success or valid, `1` a problem with the archive
  or the runtime (or a policy refusal), `2` a command usage problem.

### What a backup excludes

```text
model API keys
IMAP passwords
SMTP passwords
eHall authenticated browser profile
derived knowledge indexes
external original knowledge files
```

The configuration file also stays where it is. Credentials and sessions belong to the environment,
and indexes are derived data that can always be rebuilt.

After a restore:

```text
reconfigure credentials     (model, IMAP, SMTP)
re-login eHall              (the authenticated browser profile is not in the archive)
re-pair mobile              (every session was revoked by the restore)
rebuild indexes             (pw sync / pw reindex from the original roots)
inspect unresolved actions  (pw actions, especially RUNNING and UNKNOWN)
```

## Windows / WSL

**The Windows-native daemon is not a v1 reference target. WSL is.** Unix permission bits and the
advisory lock are used where the platform provides them, so run the daemon inside WSL.

If you want it to start at login, let Windows Task Scheduler enter WSL and start exactly one daemon:

```text
Windows Task Scheduler
  → WSL
  → one assistantd
```

```text
wsl.exe -d <DISTRO> --cd <PROJECT_DIR> -- /bin/bash -lc 'exec uv run assistantd'
```

This project does not install a service and does not create or edit a scheduled task. If a task is
triggered twice, the single-instance lock makes the second process exit immediately instead of two
daemons writing the same runtime.

## Architecture

```text
Interaction
  CLI / Mobile / MCP

Observation
  Mail / Web / Manual

Intelligence
  Interpreter / Structured Analysis / Grounded Answers

Domain
  Tasks / Planning / Cases / Actions / Learning

Execution
  SMTP / approved eHall only

Data
  SQLite / content stores / external knowledge roots
```

The system is a **Modular Monolith**: one deployable process, strict internal layers, and no service
that can reach around another. Domain code has no I/O and no adapter imports; application code
depends on ports; adapters implement them; SQL lives in one store package, with blocking calls
confined to worker threads.

It is **Event Driven**: external input enters as a durable `RECEIVED` event through one inbox, and a
worker claims work atomically with a lease and a fencing token. Processing is at-least-once, so
handlers are idempotent or produce durable downstream intent, and cancellation propagates instead of
being recorded as a failure.

It is **Ports & Adapters**: the model provider, mail transport, browser, web surface, store and clock
are all boundaries. The model port accepts a prompt and returns validated data — it has no tool
calls, no filesystem, no database and no schedule, and its reasoning output is discarded inside the
adapter rather than persisted or logged.

Two external authorities are deliberately never copied anywhere: your original documents and your
credentials.

## Known limitations

- **Mobile is trusted-LAN HTTP**, not a public service: no TLS, no cloud relay, no VPN, no
  third-party login, no push notifications.
- **SMTP is not exactly-once.** An interrupted session becomes `UNKNOWN`, and delivery is confirmed by
  Sent reconciliation or by you, never by an automatic resend.
- **eHall page-contract changes fail closed.** If the portal changes, execution stops before typing
  anything instead of adapting to the new page.
- **eHall `UNKNOWN` requires manual inspection.** The system does not guess whether the submission
  happened.
- **Watchers are public HTTPS pages only.** No authenticated page, no JavaScript rendering, and no
  redirect following.
- **Knowledge retrieval is local FTS5, not a vector store.** It is term-based and does not understand
  meaning or paraphrase.
- **ConfirmedFacts are not auto-filled.** They are not injected into forms, drafts or model context in
  v1.
- **Playbooks are non-executing.** Promotion records that a payload is still understood; it does not
  make the action replayable.
- **MCP is local stdio only**, with a bounded surface and no approval or execution capability.
- **A backup excludes credentials and the eHall authenticated profile**, so a restore always needs
  manual reconfiguration, a fresh eHall login and a new mobile pairing.
- **There is no filesystem watcher acceleration.** Freshness comes from periodic reconciliation, so a
  change is noticed within one interval rather than instantly.
- **Linux / WSL is the reference runtime** for permissions and the daemon lock.
- **There is no database downgrade.** A runtime written by a newer schema version fails closed rather
  than being opened by an older build.

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

The **tests are network-free**: an autouse guard makes any attempt to open a socket fail loudly, and
provider behaviour is exercised through mocked transports. That guard is intentional — a test that
needs the network is a test that fails for the wrong reason, and a test that spends provider credit
is not a unit test.

Every pull request and push to main runs the repository quality gates in GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

Release gates additionally build the artifact and check the lock file:

```bash
uv lock --check
rm -rf dist && uv build
git diff --check
```

`tests/release/` covers the release contract itself: the upgrade matrix, package contents and an
installed-wheel smoke test, runtime permissions, the archive boundary, daemon lifecycle, the
capability freeze, version consistency, and the acceptance scenarios. All of it runs offline.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and [SECURITY.md](SECURITY.md)
before reporting a security issue.

## Repository structure

```text
src/assistant/domain          domain model and rules; no I/O, no adapters
src/assistant/application     use-case orchestration; depends on domain and ports
src/assistant/ports           interfaces (model, store, mail, approval, …)
src/assistant/adapters        DeepSeek, IMAP/SMTP, Playwright eHall, web, MCP, knowledge
src/assistant/store           SQLite persistence: the only runtime store
migrations                    immutable historical migrations
docs/adr                      architecture decision records
docs/specs                    architecture specification
docs/releases                 release notes
docs/examples                 sample configuration
tests                         unit / integration / contract / acceptance / release suites
```

## Release state

**Current release: v1.0.0**

- [Releases](https://github.com/Mr-tree013/Rings/releases)
- [Issues](https://github.com/Mr-tree013/Rings/issues)

The version is written in one place — `pyproject.toml` — and everything else agrees with it:
`assistant.__version__`, `pw --version`, `assistantd --version`, the changelog and the release notes.

```bash
uv run pw --version          # pw 1.0.0
uv run assistantd --version  # assistantd 1.0.0
```

Rings has no coverage, license or package-registry badge, because no such service is configured. For
the full v1 contract and how the project got here, see
[docs/releases/1.0.0.md](docs/releases/1.0.0.md) and [CHANGELOG.md](CHANGELOG.md); for moving an older
runtime forward, see [docs/upgrade-to-v1.md](docs/upgrade-to-v1.md).
