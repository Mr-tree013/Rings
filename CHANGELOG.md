# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

+- **Conversation reliability and self-knowledge (Phase 10C, ADR-0035).** Terminal input is decoded
+  through an explicit boundary, so an undecodable byte sequence says so and keeps the session open
+  instead of ending it; benign structured-output variation (`operations: null`, missing fields) is
+  normalized, with exactly one bounded repair attempt before a friendly failure; raw schema text
+  never reaches a user; multi-operation plans are fully preflighted before the first mutation; and
+  capability answers (`你能做什么`, `/help`, `mail.accounts`) come from the runtime's own registry
+  and configuration instead of prose that had already drifted out of date.
+
+### Added
+
- **Conversational mail with exact human approval (Phase 10B, ADR-0034).** A conversation can read
  recent mail, resolve a message or thread, draft a reply with the existing draft service, and
  prepare an immutable `mail.send` action — then it stops and shows the exact payload. Sending needs
  the user's own explicit phrase (`确认发送`, `发送`, `发吧`, `confirm send`), parsed deterministically
  without a model call; `可以`/`好`/`ok` confirm a local plan and nothing else. The approval,
  challenge and execution are still the v1 services, invoked by a controller that has no model in
  its dependency graph.
- Migration `0017_conversation_external_reviews.sql`: one durable pointer per reviewed action
  (action id, type, fingerprint, status, expiry, execution run), at most one live review per
  conversation thread.
- **Tree Conversation (Phase 10A, ADR-0033).** `rings`, and `pw chat` for the same runtime, are the
  primary conversational surface: a sentence is interpreted into a closed set of typed local
  operations (tasks, calendar, work sessions, weekly planning, the notification inbox and grounded
  knowledge questions) and a deterministic runtime decides what is allowed, what needs an explicit
  confirmation and who executes it. `plan.apply_proposal` is the one operation in the
  conversational capability set that requires a confirmation, answered by a fixed vocabulary
  instead of a model call.
- Migration `0016_conversations.sql`: durable threads, messages, turns and operation outcomes,
  with `APPLYING` → `APPLIED` as the crash fence and `UNKNOWN_LOCAL` for a write whose outcome is
  unknown. `pw integrity check` audits the relational invariants.

### Changed

- The README's quick start now leads with `uv run rings`; the `pw` commands remain documented as
  the advanced/admin surface, unchanged.
- `pw model status` no longer claims that natural-language interpretation is unimplemented.

### Fixed

- The CLI planner tests derived "next week" from hardcoded dates, so they failed on any day after
  the week they were written; they now compute the window the way `pw plan week --next` does.
- Three `pw doctor` tests graded the developer's own machine instead of the code, so any real
  `config.toml` with a model section but no exported key made them fail.

### Not in this phase

Conversational external actions (mail send, eHall submission, approvals and executions) are
deliberately absent and need their own ADR and phase. Conversation statements do not become
`ConfirmedFact`s, conversation history is not indexed as knowledge, and there is no generic model
tool loop.

## [1.0.0] - 2026-09-26

Growing Personal Assistant v1.0: the release that freezes the system. Phase 9B adds **no**
capability — it makes the runtime single-instance, private by default, strict about the artifacts it
produces, honest about what "upgrade" means, installable outside a checkout, and covered by release
gates that fail if any of that drifts.

### Added

- Single-instance daemon (ADR-0032): `assistantd` holds `flock(LOCK_EX|LOCK_NB)` on
  `<runtime>/assistantd.lock` (mode `0600`) for its whole lifetime, so exactly one daemon may serve
  one runtime data directory. A second instance exits non-zero with
  `Another assistantd instance is already running for this runtime data directory.` *before* any
  supervisor starts; a lock file whose lock is not held (a machine that lost power) never blocks
  startup. `pw daemon status` answers from the same lock and labels the pid/start-time header as
  informational, and `assistantd --version` now exists, answers without opening anything and reports
  an unknown argument instead of starting.
- Private runtime permissions: directories the project creates (runtime root, mail raw, web
  snapshots, restore staging, eHall profile, knowledge index cache) are `0700`, and files it creates
  (runtime database, raw mail objects, web snapshots, backup archives and their temporaries, the
  daemon lock, restored content) are `0600` — verified under `umask 0`, so the modes come from the
  project rather than the shell. Existing files and user-owned directories are never rewritten:
  `pw integrity check` reports them in a new `permissions` section (`FAIL` for a world-writable
  database or content root, `WARN` for group/other bits, nothing repaired).
- Strict `.gab` end-of-archive: the final ZIP end-of-central-directory record must sit at the very
  end of the file with an empty comment, and the central directory must end exactly where it begins.
  `valid.gab + junk`, `valid.gab + another-archive` and an archive carrying a ZIP comment are now
  invalid for both `pw backup verify` and `pw backup restore` (this closes the known limit recorded
  in Phase 9A). Zip64 archives stay supported.
- Migration forward compatibility: a runtime whose `schema_migrations` contains a version this build
  does not ship — or a known version whose recorded name was rewritten — is refused with
  `DatabaseMigrationIncompatible` by bootstrap, by `assistantd` and by mutating commands. The
  read-only `pw integrity check` reports `INCOMPATIBLE`/`FAIL` and changes nothing. Nothing is
  ignored and no downgrade path exists.
- Upgrade matrix coverage: every migration prefix (`0001` … `0015`) is created and upgraded to the
  current schema, and six representative eras (`0001` events, `0003` storage, `0006` commitments and
  scheduler, `0009` mail and drafts, `0012` cases and mobile, `0015` facts and observations) are
  seeded with rows of their era, upgraded, and checked for preserved data, integrity, foreign keys
  and a usable schema.
- Installable release artifact: the wheel carries `assistant/migrations/0001–0015` and the mobile
  web assets, and resolves them from the installed package, so an installed `pw`/`assistantd` can
  migrate and serve a runtime with no checkout. Release tests build the wheel and sdist, check their
  contents (no tests, no `.env`, no runtime state, no fixture sentinels), install the wheel into a
  temporary target, and run all three console entry points, `pw status`, `pw doctor` and
  `pw integrity check` from outside the source tree.
- Safe-by-default sample configuration: `docs/examples/config.toml` now parses with no mail account,
  no watcher target, `ehall.enabled = false`, `mobile.enabled = false`, `mcp.enabled = false`,
  `mcp.write_scope = "none"` and `mcp.expose_knowledge = false`, and composes no external executor at
  all. Every capability it documents is commented out with the environment variable it would need.
- Release gates: `tests/release/` adds upgrade-matrix, future-database, config-safety, daemon
  subprocess lifecycle, permission, archive-boundary, package-smoke, CLI/version-surface,
  capability-freeze, stress-invariant, restart-safety, fresh/historical/backup-release acceptance and
  privacy-sweep coverage. The stress suites drive 1 000 events, 500 scheduled jobs and hundreds of
  commitments with a `FakeClock`, and assert dedup, leash/fencing, notification uniqueness and
  bounded read surfaces rather than timings.
- Documentation for a first release: README quick start, product boundary and known limitations;
  `docs/upgrade-to-v1.md`; `docs/releases/1.0.0.md`; Windows/WSL daemon startup example; and an
  ADR-0032 runtime/release contract that freezes the rules above.

### Changed

- `pw status` reports the v1 capability set explicitly (execution names both production executors,
  external inputs include watchers and manual text, and new `learning`/`mobile`/`operational` rows),
  without claiming autonomy, automatic execution, autofill or cloud sync.
- `pw doctor` now distinguishes an incompatible migration history (and fails) and points at
  `pw integrity check` for the full audit; it remains local and network-free.
- Duplicate-member warnings the archive tests provoke deliberately are asserted with
  `pytest.warns`, so a green test run has no unexpected warnings.

### Security

- Restoring an archive cannot widen permissions: extracted content is written `0600` inside `0700`
  staging, and ZIP external attributes are never applied.
- The release suite stays network-free: fake or in-process adapters only, and installing the built
  wheel uses `--no-deps --target` instead of a package index.

### Operational hardening (Phase 9A, first released in 1.0.0)

Phase 9A: operational hardening before v1.0. No new business capability, no new external
integration, no schema change — the runtime can now be inspected without touching it, backed up
consistently, and recovered into a fresh directory that comes back *without* the authorization it
held when the backup was taken.

### Added

- Operational integrity checker (ADR-0031): `pw integrity check` audits the runtime in a read-only,
  offline SQLite connection — it cannot create, migrate or repair anything. It reports
  `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, migration state (a pending migration is a
  finding, not an instruction) and cross-domain consistency: capability fingerprints re-hashed from
  the stored `ActionRequest` payload, challenge/approval/execution relationships, mail thread
  membership and event/draft/send links, fact-candidate provenance and the single-live-fact-per-key
  rule, playbook promotion provenance, web observation lineage and web/manual event links, plus a
  per-object hash re-check of every referenced raw mail and web snapshot. A configured knowledge
  root that is offline (an unplugged USB vault) is reported as offline rather than as corruption.
- Consistent backup archives (`.gab`): `pw backup create|verify|inspect` write and read one versioned
  format — a canonical `manifest.json`, a `runtime.sqlite3` produced by SQLite's own
  consistent-backup API (never a file copy of a live WAL database), and the content-addressed
  `mail/raw/…` and `web/snapshots/…` objects the snapshot references, each verified against the hash
  the database recorded. A missing object (`BackupSourceMissing`) or a mismatched one
  (`BackupSourceCorrupt`) fails the whole backup instead of producing an archive with a hole; the
  output is written to a temporary file, verified, and atomically renamed, and an existing file is
  never overwritten.
- Safe staging restore: `pw backup restore FILE --to DIR` requires a destination that does not exist
  or is empty, and refuses the active runtime directory or a parent/child of it. There is no
  `--in-place` and no `--force`. Members are checked before anything is written (no absolute paths,
  no `..`, no backslashes, no drive letters, no symlinks, no duplicates, no unlisted or missing
  members, with named bounds on member count, manifest size, database size, object size, total
  uncompressed size and compression ratio), extracted one by one with their size and SHA-256
  verified, finalized, re-checked, and only then renamed into place.
- Restore authorization invalidation: finalization spends unconsumed `ApprovalChallenge`s
  (`consumed_at`), supersedes still-valid `Approval`s (`superseded_at` — never `consumed_at`, which
  would claim they were used), consumes unredeemed mobile pairing tokens and revokes every mobile
  session, while deleting no history at all. `RUNNING` and `UNKNOWN` `ExecutionRun`s stay exactly as
  unresolved as they were, so `pw action execute` still refuses to blind-retry after recovery. A
  restore never restores credentials: SMTP/IMAP passwords, the model key and the eHall browser
  profile are not in the archive, and no `.env` is generated.
- Cross-system acceptance suite (`tests/acceptance/`): the full lifecycle over real SQLite stores,
  migration runner, `EventInbox`/`EventWorker`, scheduler and approval chain, with fakes only at the
  external edges (model, IMAP, SMTP, eHall page, HTTP watcher), temporary XDG roots, the outbound
  socket guard enabled throughout, and no mocked application service. It covers
  Observe → Understand → Commit → Plan → Review (manual input → analysis → explicit task → reminder
  → proposal → apply → work session → completion), the mail path through exactly one SMTP `DATA` to
  a reviewed playbook, knowledge grounding with a citation into a confirmed fact, eHall
  contract-change safety (zero submits, then exactly one), mobile/MCP boundaries, watcher
  baseline/change semantics, provider calls staying at exactly one across an analysis retry, restart
  across two bootstrap instances, lease expiry reclaim and fencing, `UNKNOWN` no-retry after restart,
  supervisor crash isolation and shutdown, and a seeded sentinel sweep across captured logs.
- `pw doctor` gained local operational rows (runtime writable, mail raw root, web snapshot root,
  migration state) and points at `pw integrity check` when a deeper audit is warranted. It still
  performs no network access.
- Exit codes for the operator commands: `0` valid, `1` a bad archive/runtime or a refused request
  (non-empty destination, the live runtime directory), `2` a usage problem such as a `FILE` argument
  that is not a file.

## [0.8.0] - 2026-09-20

Phase 8: the assistant can watch, listen and be looked at. It observes configured public pages and
text a person pastes in, and it serves a controlled local MCP surface to VS Code — the same
read-mostly view of open tasks, open cases and the current plan, with nothing that can approve,
execute, send, submit, confirm, promote or generalise anything.

Phase 8A (observation): the assistant can watch a page it was told to watch and can be handed
text by a person, turning both into durable, versioned observations that feed one bounded
analysis — without a generic HTTP client, without a browser, and without creating any work.

### Added

- Durable web observation (ADR-0029): `[watchers]` with `[[watchers.web]]` targets, each an id and a
  fixed HTTPS URL. Only `https://` is accepted, with no credentials, no IP-literal host and no
  explicit port; the model never chooses a URL, and there is no command that fetches an arbitrary
  one. Before a request the hostname is resolved and every address must be public — loopback,
  private, link-local, multicast, reserved and shared carrier-grade space are refused — and
  redirects are never followed (`304` is the only 3xx accepted).
- Read-only, bounded fetching: no cookies, no authentication, no JavaScript, no browser (and no use
  of the eHall Playwright capability), a finite timeout, a streaming `max_response_bytes` cap that
  abandons a response without writing a snapshot, and three content types (`text/html`,
  `text/plain`, `application/json`). Deterministic extraction drops `script`, `style` and
  `noscript`, never fetches a sub-resource, decodes invalid UTF-8 with replacement, and normalizes
  line by line so the same page always hashes the same; the normalized text is stored
  content-addressed under `web/snapshots/` and only the relative key reaches the database.
- Baseline semantics and honest reconciliation: the first successful fetch writes a snapshot and a
  baseline observation and emits **no** event; only a later content-hash change produces a change
  observation that names its predecessor, bridged to exactly one `web.page.changed` event whose
  payload is `{observation_id, target_id}` — never the page. `ETag`/`Last-Modified` are used only
  as an optimization: every `full_fetch_every` checks forces an unconditional fetch, so a server
  that answers `304` forever cannot hide a change. Both bridge crash windows are repaired by a
  bounded round, including rounds with no change at all.
- Manual input (ADR-0029): `pw ingest text TEXT --source manual|qq-forward|other` stores the text
  durably first and then queues one `manual.input.received` event naming `{manual_input_id,
  source}`. `pw ingest list` and `pw ingest show` read it back. The command calls no model and says
  what may happen next instead.
- One bounded analysis for both: a deterministic `difflib` change context (`target_id`, previous and
  current hashes, `added_text`, `removed_text`, `current_excerpt`, capped at 3000/3000/6000 and
  12000 total) or a quoted manual document (`source`, `input_source`, `text` capped at 12000). No
  knowledge, facts, tasks, calendar, mail or playbooks are attached, and no URL is ever sent. The
  closed schema returns only `category`, `summary` and `action_candidates`; a candidate that claims
  a deadline or an event start must carry the text it was read from or an instant with an explicit
  offset, and a deadline is never rewritten into an event start.
- `observation_analyses` is keyed by `inbound_event_id UNIQUE` and stores the analyzer version plus
  a fingerprint over the versions, the event identity, the source identities and the exact bounded
  context, so a retried `EventWorker` event reuses its analysis and does not call the provider
  again. The handler imports no service that could create a task, case, fact, playbook, action or
  approval, and a no-mutation test proves that only the analysis table grows.
- `web-watch` as one more supervised daemon service, started only when a target is enabled, with
  per-target failure isolation. `event-worker` now starts whenever a usable model is configured —
  manual input and watched pages produce events with no mail account — and without a model the
  observations and events stay durable and `RECEIVED` rather than being discarded.
- `pw watch targets|status|sync|observations|observation show`, where only `sync` uses the network
  and every other view reads local state. Observation output shows hashes, a bounded preview and
  the analysis, and never a filesystem path.

Phase 8B (MCP): VS Code can look at the assistant through a local stdio server whose surface is
fixed by code and narrowed by configuration — read-only by default, four bounded resources, two
read tools, and exactly two capabilities it can be granted: bounded local knowledge search and task
writes through `TaskService`.

### Added

- A controlled local MCP interface (ADR-0030), built on the official MCP Python SDK v2
  (`MCPServer`, `mcp>=2,<3`). `growing-assistant-mcp` is a separate console script that runs the
  server over **stdio only**: it is not hosted by `assistantd` or the mobile web service, it opens
  no socket, it works with the daemon stopped, and it refuses any argument other than `--version`
  (so `--transport http` fails loudly instead of quietly serving stdio). Logging goes to stderr, and
  a disabled server writes one line to stderr and exits non-zero, leaving stdout empty.
- Three configuration keys, all conservative: `[mcp] enabled` (default false),
  `write_scope = "none" | "tasks"` (default `none`) and `expose_knowledge` (default false). There is
  no transport, port, host, trusted-client or capability key, and unknown keys are refused.
- A bounded application surface in `application/mcp_facade.py` that contains no SDK import and no
  side-effect service at all: it wraps `TaskService`, `CaseService`, the commitment read model, the
  existing weekly planning window, the notification inbox and (optionally) the local knowledge
  search into small immutable DTOs.
- Four fixed resources: `assistant://status` (version, transport, write scope, knowledge flag, open
  task/case and unread notification counts, capability list), `assistant://tasks/open` (≤50 tasks
  with id, title, priority, status, estimate and deadline),
  `assistant://cases/open` (≤50 cases with their lifecycle fields — never their actions) and
  `assistant://plan/current` (the current week's blocks, ≤100, or `configured=false` when planning
  is unconfigured). Reading the plan never generates, applies or replans one.
- `assistant_get_task` and `assistant_get_case`, annotated `readOnlyHint`, accepting a full UUID or
  a unique prefix and returning only the entity's own fields, with typed refusals
  (`{"error": {"kind", "message"}}`) that carry no SQL, path or stack trace.
- Optional, independently opt-in knowledge exposure: `assistant_search_knowledge` exists only when
  `expose_knowledge = true`, calls the deterministic **local** full-text search (never
  `GroundedAnswerService`, never a provider), takes a query ≤1000 characters, an optional `root_id`
  and a limit of 1–8, and returns logical URIs, source spans and excerpts capped at 1200 characters
  each and 6000 in total. The tool description says the excerpts go to the connected MCP client.
- Optional task writes behind `write_scope = "tasks"`: `assistant_create_task` and
  `assistant_complete_task` call `TaskService` — so deadline reminders and rolling replan requests
  are materialized exactly as `pw task add` does — with no `force`, no bulk completion, a deadline
  that must carry an explicit offset, `destructiveHint = false` on creation and `destructiveHint =
  true` (never `idempotentHint`) on the terminal completion. With `write_scope = "none"` those tools
  are not registered at all, so a client that asks for one by name gets an unknown tool.
- `pw mcp status` (local, prints the surface this host would actually register, built from the same
  registration functions the server uses) and `pw mcp vscode-config` (prints a `servers` snippet
  with `type: stdio`, `command: uv`, `args` and a `cwd`, and writes nothing anywhere).
  `pw status` now names the integration as a local stdio surface with controlled capabilities.

### Notes

- There is no migration: MCP keeps no durable server state, no session and no authentication, so
  migrations still end at `0015_inbound_observations.sql`.
- The MCP surface cannot reach approval, execution, SMTP, eHall, facts, playbooks, filesystem,
  shell, HTTP, browser, sampling, elicitation, prompts or workspace roots. The absence is
  structural — the facade and the adapter do not import those services — and it is asserted by
  architecture tests, by a table-snapshot no-mutation test and by sentinel tests that seed a mail
  body, a draft body, a fact value, a playbook note, an eHall payload and an approval token and
  check that none of them appears anywhere on the default surface.

## [0.7.0] - 2026-09-20

Phase 7 (learning): the assistant can now remember — about the person, and about the work. Facts
become personal only when a human confirms them, a successful run becomes a playbook only when a
human names it, tests it and promotes it, and neither path can execute, approve or generalise
anything on its own. This release also carries Phase 6D's same-LAN mobile control plane.

Phase 6D (mobile): a phone on the same network can read progress, create and finish tasks, edit a
reply draft and approve one exact action. It cannot send, submit or execute anything — that stays
where it has always been, on the host, behind `pw action execute`.

### Added

- Same-LAN mobile control plane (ADR-0026): `[mobile]` configuration with exactly three keys
  (`enabled`, `bind = loopback | lan`, `port`), disabled by default, and no way to name a public
  host, trust a proxy header, list a CORS origin or skip TLS verification. `lan` binds `0.0.0.0`,
  and the middleware that actually keeps the control plane local decides from the **socket peer**
  with `ipaddress`; `X-Forwarded-For`, `Forwarded` and `X-Real-IP` are never consulted, so a public
  client is refused even when it claims a private address.
- Pairing and sessions that hold no usable secret: a ≥256-bit one-time code (`pw mobile pair`,
  printed once, never in a URL, ten-minute TTL) is redeemed for a revocable 30-day session. The
  database stores only SHA-256 values — pairing code, session token and CSRF token — behind `CHECK`
  constraints, and a mutation requires the session cookie *plus* the `X-CSRF-Token` header *plus*
  the CSRF cookie before anything happens. `pw mobile sessions` lists them and `pw mobile revoke`
  cuts one off.
- A narrow authenticated API: dashboard, tasks (create and complete through `TaskService`), cases,
  notifications, mail drafts (view, and edit with the existing optimistic concurrency — a stale
  version is `409` with the current one), actions, and approval. Every response carries a `'self'`
-only CSP, `no-referrer`, `nosniff`, `X-Frame-Options: DENY` and `no-store`, the schema endpoints
  are disabled, and the route table is pinned by a test so nothing can be added quietly.
- Approval that stops at approval: the challenge flow mints a Phase 6A token, shows the exact
  fingerprint and canonical payload, and posts it to `ApprovalService.approve`. The response says
  `executed: false`, the web adapter cannot import `ActionExecutionService`, and there is no
  execute, send, submit, retry or resend route anywhere. `pw mobile approval-link ACTION` prints
  `http://<lan-ip>:<port>/approve/<id>#token=…` — the token lives in the URL fragment, which never
  reaches the server, and the page strips it from the address bar immediately.
- A static UI that is genuinely static: three HTML pages, one stylesheet and one small vanilla
  script, no framework, no build step and not a single external URL. Every piece of user-controlled
  text reaches the page through `textContent`, and `innerHTML`, `eval` and `new Function` are
  absent from the assets (asserted by tests, alongside "no third-party assets").
- `mobile-web` as one more supervised daemon service, started only when `[mobile] enabled = true`.
  A crashed web server is retried on the usual backoff without disturbing `index-sync`, and the
  stop event shuts Uvicorn down cleanly with the server task always awaited.
- `pw mobile status|pair|sessions|revoke|approval-link`. All of them are local: nothing starts a
  server, nothing contacts the network, and the server logs no access lines, so a token cannot end
  up in a log file, a URL query string or a page.

Phase 7A (learning): the assistant can remember a personal fact — a student id, an office, a
signature — but only because a person wrote down what is true and then promoted it by hand. A
candidate is never a fact, nothing consumes a fact yet, and the sentence that justified it stays
on the record.

### Added

- Durable corrections, candidates and confirmed facts (ADR-0027), in three new tables
  (`0013_learning_facts.sql`): `corrections` holds what the user said in their own words,
  `fact_candidates` holds a proposal with the correction it came from, and `confirmed_facts` holds
  what a person promoted. Values are strings, keys are lowercase namespaced identifiers, and every
  candidate carries durable provenance because the foreign key says so.
- A human-only promotion boundary. `pw fact candidate confirm` is the single path that creates a
  `ConfirmedFact`; there is no `--force`, no `--edit-value` and no `--confirm-all`, and the service
  cannot be handed a value that differs from the candidate it is confirming. An architecture test
  walks every module in `src/` and fails if anything outside the learning path and the composition
  root even names `FactCandidate`, `ConfirmedFact`, `LearningService` or a confirmation call — so
  no model, worker, scheduler, mail handler, eHall pipeline or web route can promote anything.
- One current fact per key, with history kept. Confirming a new value runs in one `BEGIN IMMEDIATE`
  that re-reads the candidate, retires the key's current row with a compare-and-set, inserts the new
  fact and records the candidate's single status transition; a partial unique index on
  `(fact_key) WHERE superseded_at IS NULL` is the database's half of the guarantee, and a failed
  confirmation rolls the retirement back so the old fact stays current and the candidate stays
  pending. Superseded and rejected rows are never deleted.
- Expiry that needs no job: `active` is `superseded_at IS NULL AND (valid_until IS NULL OR
  valid_until > now)`, computed at read time. An expired-but-current row still owns its key, so the
  next confirmation retires it properly; a candidate whose proposed window has already closed
  cannot be confirmed at all.
- A credential ban expressed on the key, not guessed from the value: keys matching
  `^[a-z][a-z0-9_.-]{0,127}$` are accepted, and a segment (or one of its `_`/`-`-separated words)
  naming a password, secret, token, credential or private key is refused with `ForbiddenFactKey` —
  `mail.smtp_token` is refused, `profile.tokenizer` is an ordinary key. The fact store is not a
  credential store.
- `LearningService` and `SqliteLearningRepository` (`add_correction`, `propose_fact`, `confirm_fact`,
  `reject_fact`, `list_candidates`, `list_facts`, `get_active_fact`, `list_active_facts`), with the
  clock and the fact id injected at the composition root.
- `pw corrections|correction add|show`, `pw fact candidates|candidate add|show|confirm|reject`,
  `pw facts [--all]` and `pw fact show`, with the usual full-UUID-or-unique-prefix resolution and a
  review view that prints the user's own words as the source.

### Notes

- Confirmed facts are **not** injected into models, mail drafts or eHall forms in this phase, and
  eHall `--field` still requires explicit input. `get_active_fact` and `list_active_facts` exist as
  the single implementation of "current and unexpired" for whoever asks later.
- The mobile route table is unchanged (no `/api/facts`), and adding a fact leaves every other table
  — tasks, deadlines, plans, mail, actions, approvals, executions, notifications, sessions —
  byte-for-byte as it was; a test asserts exactly that.

Phase 7B (playbooks): a definitively successful action can be remembered as a reviewed reference
blueprint — named by a person, dry-run against the current local parser, and promoted only with a
pass that still applies. A playbook records what worked; it cannot run it again.

### Added

- Durable playbook candidates, replay tests and playbooks (ADR-0028), in three new tables
  (`0014_playbooks.sql`). A candidate stores a name, a note, the source action, the source
  `SUCCEEDED` run, the action type and the source fingerprint — a pointer to the immutable
  `ActionRequest`, never a copy of a mail body or a form's values.
- Explicit, human-only creation: `pw playbook candidate add ACTION --name ... --note ...`.
  Nothing creates a candidate by itself, and a test asserts that a successful execution leaves the
  candidate count unchanged. Creation reloads the action and the run, re-hashes the payload,
  requires `EXECUTED` plus `SUCCEEDED` plus `finished_at`, and refuses `FAILED`, `UNKNOWN`,
  `RUNNING` and never-executed actions with `PlaybookSourceNotEligible`. An action type with no
  replay validator is refused with `PlaybookSourceUnsupported`, so every pending candidate has a
  test it could pass, and `UNIQUE(source_action_id)` means one success seeds exactly one candidate.
- Side-effect-free replay: `PlaybookReplayValidator` is a separate protocol from `ActionExecutor`
  — `action_type`, `contract_version` and a pure `validate(action)`. Two validators are registered
  by name (`mail.send` re-runs `MailSendPayload.from_payload`, `ehall.submit-certificate` re-runs
  the typed certificate parser); there is no dynamic import, no plugin discovery and no generic
  executor. A dry run reads no credential, opens no socket or browser, searches no Sent folder,
  mints no Message-ID, creates no `ActionRequest`/`Approval`/`ExecutionRun`, and passes with SMTP
  unconfigured and the eHall pipeline disabled — tests assert the SMTP conversation and the eHall
  gateway step counts do not move.
- Honest audit rows: each `playbook_replay_tests` row stores the candidate, the action type, the
  registered contract version, an `input_fingerprint` over the candidate snapshot and the
  validator identity, the status, and a tuple of bounded issue codes (`payload-invalid`,
  `schema-version-unsupported`, `action-type-mismatch`). No payload, body, field value or exception
  text is persisted, and the schema rejects a passing row with codes or a failing row without one.
  Corruption — a payload that no longer re-hashes, a missing source run, a drifted candidate
  fingerprint — raises instead of being recorded as an ordinary result.
- Human promotion with a current pass: `pw playbook candidate promote` runs one transaction that
  requires the candidate to still be pending and a `PASSED` test at the current replay contract
  version with the exact input fingerprint, then inserts the playbook and records the transition.
  Without such a test it refuses with `PlaybookCandidateNotTested`; there is no `--force`, no
  `--skip-test` and no `--auto`. A contract-version bump invalidates old passes, while existing
  playbooks keep the version they were promoted under.
- `pw playbook candidates|candidate add|show|test|promote|reject` and
  `pw playbooks|playbook show|retire`, with the usual full-UUID-or-unique-prefix resolution. A dry
  run prints "Dry-run only. No external side effect was attempted." and, on a pass, says plainly
  that the payload is still accepted locally and that this proves nothing about the world today.

### Notes

- A playbook is a reference blueprint: it contains no approval, grants no capability, cannot
  create an `ActionRequest`, and the source has no `PlaybookExecutor`, no
  `run`/`execute`/`apply`/`instantiate` command and no `${...}`/`{{...}}` parameterisation —
  architecture tests fail if any of those appear. Rejected candidates and retired playbooks are
  kept as history, and the mobile route table is unchanged.
- The fact track and the playbook track stay separate: corrections feed `FactCandidate` and
  `ConfirmedFact`, successful executions feed `PlaybookCandidate` and `Playbook`.

## [0.6.0] - 2026-09-20

Phase 6C (eHall): the first whitelisted university errand — the certificate application — can be
inspected, prepared, approved and submitted, with a browser that only opens when a person asks and
only ever moves through the pipeline it was written for.

### Added

- Approved eHall certificate pipeline (ADR-0025): `ehall.submit-certificate` is the second
  production capability, registered only when `[ehall] enabled = true`, and it is the only eHall
  operation that exists — there is no drop-course, withdrawal, cancellation, deletion or
  arbitrary-form capability anywhere, and the port the application sees has exactly two typed
  operations (`inspect_form`, `submit_certificate`).
- Manual, headed, private browser session: `pw ehall login` opens Chromium at the eHall home page
  and lets the user complete SSO and MFA by hand. There is no code path that fills a username or a
  password, no configuration key that could hold one, and the profile lives in
  `$XDG_DATA_HOME/growing-assistant/ehall/nju-profile/` with owner-only permissions. Top-level
  navigation is allow-listed to the three NJU hosts; anything else fails closed, while sub-resources
  and CDNs load normally.
- Read-only inspection and a page contract: `pw ehall certificate inspect` reads the whitelisted
  service, its required materials and its field schema (text, textarea, select, radio) without
  typing anything, and computes a SHA-256 over the service identity, page markers, ordered field
  definitions, required materials and submit control. A required control the pipeline cannot fill
  (a file upload) blocks the errand instead of being faked.
- Explicit values and an immutable snapshot: `pw ehall certificate prepare --case CASE --field
  KEY=VALUE` validates every key, required field and option locally and freezes the contract
  fingerprint plus the exact values into one immutable `ActionRequest`. No knowledge lookup, mail
  analysis, model or confirmed fact takes part, and the remote form is never touched during
  preparation.
- Execution that re-verifies before it types: the executor re-opens the whitelisted service,
  re-reads the contract, requires the approved fingerprint, validates every value against the live
  options, fills only the approved values, reads each one back and requires an exact match, and then
  clicks the one whitelisted submit control. A changed page, an expired session, a missing field or
  a readback mismatch is a definite `FAILED` with nothing typed and nothing submitted; anything
  after the click that the page does not decide is `UNKNOWN`, which blocks a second attempt and is
  never retried automatically.
- `pw ehall login|status` and `pw ehall certificate inspect|prepare|show`, plus `pw doctor`
  reporting whether Playwright and a Chromium build are usable (locally, with no download). There is
  deliberately no `pw ehall submit`, no `click`, no `open` and no `fill`: the only submission path
  remains `pw action execute`, after a human approval bound to the exact action payload.
- No new durable state: the case, the immutable action, the approval and the execution run already
  carry the audit trail, so migrations still end at `0011_approved_mail_send.sql`.

## [0.5.0] - 2026-09-20

Phase 6 (mail workflows): the project can now read someone else's mail, understand it, draft a
reply, and — only after an explicit human approval bound to the exact bytes — send it, with an
honest answer for the one case email makes inevitable: not knowing whether it arrived. Everything
before this release was preparation for that boundary; nothing here can send, approve or execute
without a person.

### Added

- Durable IMAP inbound mail (ADR-0020): `[[mail.accounts]]` configuration (host, port, user,
  mailbox) with TLS-only transport (`IMAP4_SSL` and `ssl.create_default_context()`), read-only
  mailbox access (`select(readonly=True)`) and `BODY.PEEK` fetches, so a sync never marks mail as
  read. Credentials are environment-only (`GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD`) and a
  `password`-style config key is rejected.
- Mail identity that respects IMAP: a stable internal `MailMessage` UUID independent of any UID, a
  `MailMessageLocation` whose durable identity is `(account, mailbox, uidvalidity, uid)`, the
  Message-ID and threading headers preserved as evidence rather than identity, and attachment
  metadata (filename, content type, disposition, size, SHA-256) without materialising files.
- Raw RFC822 archive: content-addressed by SHA-256 under the runtime data directory, written
  atomically (temp file in the same directory, flush, `fsync`, rename) and verified/reused rather
  than overwritten. Database rows only ever reference a relative storage key.
- Bounded, resumable synchronization: the first sync imports the newest `initial_fetch_limit`
  messages (default 500) rather than whole history, each poll imports at most
  `max_messages_per_poll`, oversize messages are fetched header-only and recorded with
  `body_status = oversize`, and the cursor only advances to the highest UID the batch durably
  stored. Messages, locations, attachments and the cursor commit in one transaction.
- UIDVALIDITY reconciliation: when the server reports a new UIDVALIDITY, a bounded window is
  re-read and matched against stored raw hashes, Message-ID plus content fingerprints, or
  fingerprints with matching size/sender/date. Stable message ids are reused and new locations
  added; the same Message-ID with different content is never merged — a new message is created and
  the conflict is counted. Historical messages and locations are never deleted.
- Mail-to-event bridge: every stored message is bridged to exactly one `InboundEvent`
  (`source = mail:<account>`, `event_type = mail.message.received`, `external_id = message:<uuid>`)
  with identity-only content. The bridge is repaired every round (bounded), so both crash windows —
  message without event, and event without link — recover without duplicating an event. The
  `EventWorker` is still not started, because there is still no mail handler.
- Daemon and CLI: a supervised `mail-sync` service appears only when at least one account is
  configured, `pw mail accounts|status|messages|show` are read-only local views, and
  `pw mail sync [--account]` is the one command that connects to a server. `pw doctor` reports
  account credential presence without any network request, and `pw status` lists inbound mail.
- Source-grounded knowledge answers (ADR-0019): `pw ask "…"` answers only from indexed personal
  sources, with every answer segment citing evidence the user can check. The command is
  read-only: it does not modify files, does not write the index, creates no durable state, and
  executes nothing.
- Deterministic local retrieval for questions: the question is searched verbatim first, and when
  that phrase matches nothing it is split locally into its own keywords (stopwords and
  sub-trigram tokens dropped, deduplicated, in question order) which are searched individually
  and fused by reciprocal rank. No model writes, expands or reranks a query, and
  `derived_queries` records exactly what was issued.
- Bounded evidence context: full indexed chunk text (not snippets) with its logical URI and
  `SourceSpan`, deduplicated by `(root_id, chunk_id)`, numbered `S1..Sn` in retrieval-rank order,
  capped per chunk (4000 characters, deterministic prefix with `content_truncated`) and in total
  (24000 characters). Physical paths are never sent, and neither are task, calendar, work,
  notification or scheduler payloads.
- Strict grounded-answer schema: one answered branch (1-12 segments, each citing 1-8 supplied
  source ids) and one insufficient-evidence branch (no segments, short reason), closed to extra
  fields, with no place for a path, page, line, confidence or rationale. Every citation is then
  validated against the exact evidence supplied in that request — an invented `S999` is refused,
  never repaired.
- Local source resolution and rendering: the answer is displayed with locally appended citation
  markers, and the `Sources` section lists only the evidence actually cited, once each, in
  first-citation order as `[S1] <logical URI> - page N | lines X-Y`.
- Metadata-only matches and offline roots are reported separately and never treated as evidence:
  a matching file name is not a statement about a file's contents, and an unplugged vault's
  content is unavailable rather than guessed. Zero evidence means no provider call at all, and
  there is no general-knowledge fallback.
- Deterministic mail threading (ADR-0021): each stored message is placed in exactly one thread by
  code, never by a model. Parent resolution reads `In-Reply-To` first and then `References` from
  the nearest entry backwards, accepts only a Message-ID that matches exactly one stored message
  in the same account, and records why: `root`, `linked`, `unresolved` (the parent was never
  stored) or `ambiguous` (a duplicate Message-ID — never resolved by choosing). Membership is
  recorded once, is idempotent, is cycle-safe under a depth limit, and never crosses accounts.
- Structured mail analysis: the first real `EventHandler` (registered through a one-entry
  `InboundEventDispatcher`) turns a `mail.message.received` event into a durable `MailAnalysis`
  with a category (`ordinary_correspondence`, `receipt_result`, `actionable_notice`, `unknown`),
  a `requires_reply` verdict, a bounded summary and up to 10 action candidates. A deadline
  candidate and an event-start candidate are different kinds and are never merged; an
  `interpreted_at` must carry an explicit UTC offset, and a naive one is refused rather than
  assumed local.
- Bounded untrusted mail context: the request carries the current message plus at most 5 earlier
  messages of the same thread, 3000 characters per body and 12000 in total, with a deterministic
  truncation flag. `To`/`Cc`, attachment bytes and hashes, raw `.eml`, storage keys, credentials
  and all Task, Calendar, Scheduler, Notification and Knowledge state are absent by construction,
  and mail text only ever appears as quoted data inside the user message.
- Analysis idempotency: each analysis stores an input fingerprint (analyzer and schema versions,
  message id, content fingerprint, ordered thread context, planning timezone). An `EventWorker`
  retry that finds the same `(analyzer_version, input_fingerprint)` reuses the stored analysis
  without a second provider call, so a crash after the analysis costs nothing. A changed
  fingerprint replaces the row atomically and keeps its original `created_at`.
- Mail model failure handling: rate limiting, transient and unavailable errors, provider protocol
  failures and unusable structured output retry under the existing bounded event policy, while
  authentication, billing, invalid-request, missing-credential and configuration failures
  dead-letter immediately. A model failure never modifies or removes the stored mail, and an
  oversize body is never sent to a provider at all.
- Daemon and CLI: `event-worker` is supervised only when mail accounts and a usable model are both
  configured — without a model, mail keeps arriving as `RECEIVED` events and waits for one —
  and `pw mail threads`, `pw mail thread show` and `pw mail analysis` are read-only local views.
  `pw mail messages` and `pw mail show` now show the thread and the stored analysis category.

- Durable reply drafts (ADR-0022): `pw mail draft create MESSAGE` writes a local reply draft with
  the configured model, `pw mail drafts` / `pw mail draft show` inspect it, and
  `pw mail draft edit DRAFT` changes its subject and/or body. A draft is local content: there is no
  SMTP client, no approval, no `ActionRequest` and no send path anywhere in this phase.
- Deterministic reply addressing: `Reply-To` is parsed with the stdlib address parser, persisted
  on `mail_messages` (default `[]`, so existing rows are untouched) and preferred over `From`; the
  resolver extracts strict mailboxes in header order and refuses a message with no usable address
  before any model call. The reply subject is a pure function that preserves an existing `Re:`
  prefix and never accumulates `Re: Re:`.
- Explicit knowledge context: personal knowledge reaches a draft only when the user supplies
  `--context-query`, and the single call into the index sits behind that guard — a message asking
  to "search all my files" produces zero searches and zero evidence. Evidence reuses the existing
  bounded `GroundedContext` boundary, `used_source_ids` is validated against exactly the supplied
  ids (an unsupplied `S999` refuses the draft), and only identity and location are stored in
  `mail_draft_sources` — never the excerpt and never a filesystem path.
- Closed draft schema and local provenance: the model returns `body`, `used_source_ids` and
  `needs_user_input` and nothing else, so it cannot address the message, name the thread, invent a
  personal fact or send anything; unsupported personal facts come back as open questions instead
  of prose. Each draft records an audit fingerprint of the prompt/schema versions, recipients,
  subject, thread context, query and supplied evidence identity.
- Optimistic draft editing: `UPDATE … WHERE id = ? AND version = ?` with `StaleMailDraftUpdate` on
  a mismatch, a version bump and `origin = user_edited` on success, and recipients and generation
  provenance preserved. Reading and editing need no provider at all, and nothing in the daemon,
  the event worker or the sync path can create a draft.

- Case / ActionRequest / Approval / ExecutionRun safety foundation (ADR-0023): a durable `Case`
  container, an immutable `ActionRequest` whose payload is canonicalised once and whose SHA-256 is
  its identity, a hash-only single-use approval challenge, a human approval bound to one exact
  fingerprint, and one recorded execution attempt per approval. There is no SMTP, eHall, browser
  or shell capability anywhere: the production executor set is empty, so every action type that
  can be named today is one that cannot be performed.
- Canonical, tamper-resistant action payloads: only JSON data is accepted (`NaN`, infinities,
  `bytes`, datetimes and arbitrary objects are refused), the fingerprint is
  `SHA256(canonical JSON)`, and it is re-derived whenever an action is loaded — a row whose payload
  and fingerprint disagree cannot even be materialised, and an execution re-hashes before it
  touches an approval.
- Approval challenge secrets that cannot leak: a 256-bit random token is returned exactly once by
  `pw action challenge`, and only `sha256(token)` is stored. Tokens are single-use with a 600-second
  TTL, are never logged, are never echoed in an error, and are never shown again by a later command.
  At most one approval may be outstanding per action; re-approving after expiry supersedes the old
  record instead of deleting or overwriting it, so the audit trail keeps every decision.
- Atomic approval consumption and execution start: one transaction re-verifies the fingerprint,
  refuses an unresolved earlier attempt, consumes the exact approval with a compare-and-set and
  creates the `RUNNING` run, so two concurrent callers cannot both execute and the executor runs
  once. A successful outcome marks the action `EXECUTED` in the same transaction; a definite failure
  leaves it `PREPARED` with the approval spent; an `UNKNOWN` result — including an executor that
  raised or a process that was cancelled — blocks any further execution until a future
  executor-specific reconciliation, and is never retried automatically.
- `pw cases` / `pw case add|show|done|cancel` and `pw actions` / `pw action
  show|challenge|approve|execute|cancel`: the CLI shows the exact payload and fingerprint, renders
  approval and execution state honestly, refuses a wrong or expired token without echoing it, and
  answers `CapabilityUnavailable` (without consuming anything) for every action in a deployment with
  no registered executor. There is deliberately no `pw action create`, no `--force` and no
  `--approve-all`.

- Approved SMTP delivery (ADR-0024): `mail.send` is the first production executor capability, and
  the only one — it is registered only when an account configures an outbound SMTP block, so
  naming another action type still grants nothing. `pw mail send prepare DRAFT --case CASE`
  snapshots one draft version into an immutable `ActionRequest` whose payload carries the derived
  From, To, Subject, body, Date, Message-ID and reply headers; the approval chain is unchanged
  (`pw action show|challenge|approve|execute`), and `pw mail sends` / `pw mail send show` are
  read-only views of it.
- Exact approved content: the bytes that leave the machine come from the approved payload, never
  from the draft, so editing a draft after preparation leaves the action untouched and requires a
  new preparation (and a new approval) to change what is sent. `mail_send_links` makes "one draft
  version, one send action" and "one Message-ID, one send" database constraints, and a stable RFC
  Message-ID is minted before approval and reused in the transmitted bytes and in the Sent-folder
  lookup.
- TLS-only SMTP with environment-only credentials: `smtp_host`, `smtp_port`, `smtp_security`
  (`starttls` or `ssl`, never plaintext and no way to skip verification), `smtp_username`,
  `from_address` and `sent_mailbox` are optional as a group and must be complete when used; the
  secret is `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD`, separate from the IMAP password,
  and never stored, logged or placed in a payload.
- Capability preflight before approval consumption: `ActionExecutor.supports` answers "could this
  host perform this action right now?" purely and offline, and the execution service asks before
  consuming anything — so a missing SMTP credential produces `CapabilityUnavailable` with the
  approval still valid and no execution run created. Preparing a send deliberately needs no
  credential, so prepare, review and approve work without one.
- Honest send outcomes: the SMTP conversation is written out explicitly
  (`EHLO → [STARTTLS → EHLO] → LOGIN → MAIL FROM → RCPT TO → DATA → QUIT`) and the stage decides
  the result. Authentication, sender, recipient and `DATA` rejections are a definite `FAILED`;
  a transport or protocol failure at or after `DATA`, which may have been accepted, is `UNKNOWN`.
  Neither is ever retried automatically, and both spend the approval.
- Sent-mailbox reconciliation: `pw mail send reconcile ACTION` performs one read-only lookup for
  the exact approved Message-ID — comparing candidate headers exactly rather than trusting a
  server-side search — and records `FOUND`, `NOT_FOUND`, `AMBIGUOUS` or `UNAVAILABLE` in an
  append-only history. A `FOUND` result promotes an `UNKNOWN`/`RUNNING` attempt to `SUCCEEDED` and
  marks the action `EXECUTED` in one transaction; a missing message proves nothing and changes no
  state; two matches are never resolved by choosing one; and there is no resend command anywhere.
- Draft acknowledgement: `pw mail draft acknowledge DRAFT` records that the user has read a
  draft's open questions (the questions are kept as an audit trail), which is required before a
  send can be prepared; any later subject or body edit clears the acknowledgement again.

## [0.4.0] - 2026-09-20

Phase 4 (in progress): a model boundary the rest of the project can trust, and natural-language
interpretation that produces reviewable, typed, non-executing command drafts. The model can be
asked for validated structured data, and it can propose an action in the user's own words — but
it never holds durable state, never touches the database, and never runs anything.

### Added

- Natural-language interpreter (ADR-0018): `pw interpret "…"` interprets exactly one action into
  a typed `CommandDraft` over a closed set of seven commands (`create_task`, `complete_task`,
  `cancel_task`, `set_deadline`, `clear_deadline`, `create_calendar_event`,
  `request_week_plan`), and prints a human-readable preview plus the locally rendered equivalent
  structured CLI command. It does not execute the command, and there is no `--apply`/`--yes`/
  `--execute` flag to make it.
- Bounded, deterministic interpreter context: open task metadata only (id, title, priority,
  estimate, deadline, updated_at), capped at 50 tasks with an explicit `tasks_truncated` flag,
  ordered by deadline → priority → creation → id, serialized as canonical JSON in a single user
  message. Task descriptions, work sessions, calendar events, plan blocks, notifications,
  scheduler payloads, knowledge content, file paths and mail are never sent; the context type
  cannot even carry them.
- Strict interpreter schema: a closed top-level object whose `status` selects one of three
  mutually exclusive branches (`ready` / `needs_clarification` / `unsupported`), with every
  command a closed object whose `kind` is a `const` and whose fields are all required and
  nullable where the user stayed silent. An unknown command fails schema validation instead of
  degrading into "unsupported", and a `confidence` or reasoning field cannot appear at all.
- Semantic validation of schema-valid answers: drafts reuse the entity rules, datetimes must be
  timezone-aware ISO 8601 (naive values are rejected), a backwards calendar interval is refused
  rather than swapped, and every task reference must match a UUID from the exact context that was
  supplied — title matching, prefix resolution and re-querying the database are forbidden.
- Timezone policy: a time-bearing draft (deadline, calendar interval, weekly plan) without a
  configured `[planning].timezone` becomes a fixed clarification question instead of a guess, so
  the model cannot bypass the deterministic boundary by inventing an offset.
- Safe local preview rendering: the equivalent command is built from the typed draft with
  `shlex.quote` on every user-provided value, so a hostile title is one shell argument rather
  than an injected command. The model never produces shell text.
- Supported provider-independent model boundary (ADR-0017): `ModelPort` with a single `complete`
  operation, provider-neutral model value objects, and a `reasoning_effort` vocabulary
  (`none`/`low`/`high`/`max`) that adapters map to their own spelling.
- DeepSeek adapter (`adapters/model/deepseek.py`): the Responses API
  (`POST https://api.deepseek.com/responses`, non-streaming, stateless) with `deepseek-flash` as
  the default model name, `text.format` structured output, explicit output-item handling
  (reasoning discarded, `output_text` parts concatenated, unexpected tool calls refused),
  provider-neutral error mapping for 400/401/402/422/429/500/503 and for network failures, finite
  layered timeouts, sanitised error text, and no automatic retry.
- Locally validated structured output (`application/structured_model.py`): a JSON parse and a
  Draft 2020-12 schema validation performed by this project, so a provider's structured-output
  mode is a convenience rather than the only thing standing between a model and application code.
- `[model]` configuration (provider, model name, reasoning effort, `max_output_tokens`,
  `timeout_seconds`); credentials come from `DEEPSEEK_API_KEY` only, and an `api_key`-style key is
  rejected by the strict parser. A missing `[model]` section leaves every other capability
  unchanged.
- `FakeModelAdapter` for deterministic tests and future evals, plus `pw model status` (inspection
  only) and `pw model test` (a single live request the user chooses to make) with read-only model
  diagnostics in `pw doctor`.

## [0.3.0] - 2026-09-20

Phase 3: durable commitments, deterministic weekly planning, and time. Tasks, deadlines,
calendar events, plan blocks and work sessions are separate durable concepts; a deterministic
planner turns them into reviewable weekly proposals; and a supervised daemon scheduler delivers
deadline reminders into a durable inbox and asks for a fresh proposal after the plan's input
changes — without ever applying one.

### Added

- Durable scheduling (ADR-0016): `ScheduledJob` and `Notification` domains (migration
  `0006_scheduler_notifications.sql`), a supervisor-managed `scheduler` daemon service, and
  lease + claim-token fencing for at-least-once execution. Eligible jobs are `PENDING` and due
  (`next_attempt_at` or `due_at`), or `PROCESSING` with an expired lease; ordering is effective
  due, creation, id. Failures retry with deterministic exponential backoff (5 attempts, 30s
  base, 30min cap, no jitter) and then dead-letter; `CancelledError` propagates and leaves the
  job `PROCESSING` for lease recovery.
- Deadline reminders (ADR-0016): `[reminders] deadline_offsets_minutes` (defaults to 1440 and
  120) materializes one `DEADLINE_REMINDER` job per offset **in the same transaction as the
  deadline or task mutation**. Moving a deadline cancels the previous reminder jobs (including
  one already in flight) and stores a new generation; clearing a deadline, completing a task or
  cancelling a task cancels its unsent reminders. An offset whose time has already passed makes
  the reminder due immediately instead of dropping it.
- Durable notification inbox (ADR-0016): `notifications` with a `UNIQUE` dedup key per job, so a
  crash between "notification written" and "job completed" still yields exactly one message.
  Reminders are written as `DEADLINE_REMINDER` rows whose body carries the due instant and the
  remaining effort (`estimate − ceil(work seconds / 60)`), and rolling replans as `PLAN_READY`.
  Delivery means a row in this inbox; no OS, email or push channel exists yet.
- Rolling replanning (ADR-0016): every commitment change (task, deadline, calendar event, manual
  plan block, work session) requests one debounced `ROLLING_REPLAN` job
  (`[scheduler] replan_debounce_seconds`, default 60s) that coalesces repeated changes into a
  single trailing window. Running it creates a new **pending** `PlanProposal` and one
  `PLAN_READY` notification — never an apply, never a plan block. Nothing changed since the
  pending proposal was built (same commitment revision) means nothing is created; missing
  `[planning]` configuration completes the job with one `SCHEDULER_WARNING` instead of retrying.
- Scheduler CLI: `pw notifications [--all] [--limit]`, `pw notification show|read` (reading is
  idempotent) and read-only `pw scheduled [--all]`. `pw doctor` reports the scheduler store,
  reminder offsets and poll interval without running any job, and `pw status` lists the daemon's
  `index-sync` and `scheduler` services.
- Planning preferences (ADR-0015): `[planning]` config with a required IANA `timezone`,
  block-size bounds, a deadline buffer, and weekly `[[planning.availability]]` windows with
  strict `HH:MM` validation, weekday normalisation and no overnight windows.
- Planning domain: `PlanningWindow`, the `PlanningTask` read model, `PlanningIssue` codes,
  `ProposedPlanBlock`, `PlanProposal` with its status vocabulary, and half-open interval
  algebra (`merge_intervals`, `subtract_intervals`, `clip_interval`).
- Plan block provenance (migration `0005_planning_proposals.sql`): every block is now either
  `manual` (user-owned, never touched by the planner) or `planner` (carrying the proposal that
  created it), enforced by database constraints; existing blocks migrate to `manual`.
- Durable planning tables: `plan_proposals`, `proposed_plan_blocks`, `planning_issues`, plus
  `commitment_meta` holding the revision baseline used to fence stale proposals.
- Deterministic weekly planner (ADR-0015): a pure greedy scheduler (no LLM, no solver
  dependency) that orders tasks (deadline, priority, creation, id), first-fits them into weekly
  availability, subtracts busy time, honours deadlines as a hard finish constraint, prefers the
  deadline buffer and reports issues (`MISSING_ESTIMATE`, `ESTIMATE_EXHAUSTED`,
  `DEADLINE_ALREADY_PASSED`, `BUFFER_VIOLATED`, `INSUFFICIENT_CAPACITY`,
  `WINDOW_CAPACITY_EXHAUSTED`, `NO_AVAILABILITY`) in deterministic order.
- Reviewable plan proposals: `PlannerService` reads one consistent `PlanningSnapshot`, computes
  remaining effort as `estimate − ceil(work-session seconds / 60)`, expands weekly availability
  in the configured IANA timezone (stdlib `zoneinfo`, with an explicit DST policy) and stores a
  durable `PlanProposal` with its blocks and issues. Nothing is written to `plan_blocks` until
  the user applies it.
- Stale-proposal fencing: every planning-relevant mutation (task, deadline, calendar event,
  plan block, work session, apply) bumps `commitment_meta.revision` in its own transaction;
  proposals record both a canonical SHA-256 input fingerprint and the revision they were built
  from, and applying a proposal whose input moved marks it `STALE` without changing any plan
  block.
- Atomic proposal apply: cancelling the replaced planner blocks, inserting the new planner
  blocks with their `proposal_id`, marking the proposal `APPLIED` and bumping the revision once
  all happen in one transaction — manual blocks are never touched, and a failure rolls the
  whole replacement back.
- Planner CLI: `pw plan week [--next]` (propose only, never apply), `pw plan proposals`,
  `pw plan show` (renders the stored proposal), `pw plan apply`, and `pw task edit`
  (`--estimate`, `--clear-estimate`, `--priority`, `--title`, `--description`) so planner issues
  such as `MISSING_ESTIMATE` can be fixed. `pw calendar` now labels each plan block `manual` or
  `planner <proposal>`.
- Commitment domain (ADR-0014): `Task`, `Deadline`, `CalendarEvent`, `PlanBlock` and
  `WorkSession` as five distinct concepts, with explicit state machines, half-open interval
  semantics and timezone-aware timestamps throughout.
- Durable commitment persistence (migration `0004_commitment_core.sql`): five tables with
  database-level state, priority, estimate, terminal-consistency, positive-duration and
  foreign-key constraints, plus indexes for plan-block and work-session queries.
- `CommitmentRepository` and `WorkRepository` ports with SQLite adapters, optimistic
  concurrency via the `updated_at` compare-and-set token (`StaleTaskUpdate`), and an atomic
  terminal transition that completes/cancels a task and cancels its unfinished plan blocks in
  one transaction.
- `TaskService`, `CalendarService` and `WorkService`: structured commands, deadline rules for
  OPEN tasks only, plan blocks only for OPEN tasks, work sessions that may be back-filled
  after completion, busy-interval queries that keep their source kind, and id-prefix
  resolution that refuses to guess.
- Structured commitment CLI: `pw tasks`, `pw task add|show|done|cancel|deadline`,
  `pw calendar [--days]`, `pw calendar add`, `pw plan add|cancel`, `pw work add|list`, with
  ISO-8601-offset timestamps only (naive input is rejected, local time is never assumed).
- Regression tests for the core domain boundaries: a deadline never occupies busy time, plan
  duration never counts as actual work, completion cancels only unfinished plan blocks, and a
  failed terminal transition leaves both the task and its plan blocks untouched.

## [0.2.0] - 2026-09-20

Phase 2: personal knowledge and continuous storage indexing. Storage roots are configured,
catalogued, text-indexed and kept current by the daemon, with source-spanned search over the
result. No mail, LLM, task planning or eHall capability exists yet.

### Added

- Host configuration (ADR-0013): `~/.config/growing-assistant/config.toml` with explicit local
  and archive-vault roots, an indexing interval (10s-86400s) and `run_on_startup`. A missing
  file means "no roots configured" and the daemon still runs.
- `pw roots list` (config only, nothing is scanned) and `pw sync [--root] [--force-index]`,
  with exit codes that tell the difference between "all synced/offline", "needs attention"
  and "invalid configuration".
- `IndexSyncService`: periodic reconciliation as scan → catalog → knowledge index, with a
  single in-process lock, config declaration order, and root failure isolation.
- Incomplete-scan policy for automatic sync: catalog metadata is updated for what was seen,
  but knowledge reindexing is skipped, so a temporary permission problem cannot erase
  searchable content.
- Daemon integration: `assistantd` now composes services (config → runtime database →
  migrations → services) and supervises them with deterministic restart backoff, including
  root-level failures that stay inside the affected root.
- `IntervalWaiter` port with an asyncio adapter, so both the sync interval and the supervisor
  backoff wake immediately on shutdown instead of sleeping through it.
- `pw doctor` reports the config path, its validity and the number of configured roots;
  `pw status` describes the knowledge and daemon-service capability.
- Orchestration tests: config parsing, local/vault sync, offline and identity-mismatch
  handling, incomplete scans, multi-root isolation, infrastructure-failure propagation,
  concurrent sync serialisation, supervisor restart/backoff, daemon startup reconciliation and
  search-during-reindex consistency.
- Content extraction (ADR-0012): UTF-8 text (strict suffix whitelist) and PDF text layers via
  `pypdf`, with safe file access that revalidates relative paths, refuses symlinks and
  non-regular files, and verifies size/mtime before and after reading.
- Strong SHA-256 content fingerprints computed only for files that are actually extracted;
  the metadata scan still never reads file contents.
- Deterministic, source-traceable chunking (`chunk_lines`, `chunk_pages`) with inclusive line
  ranges and page numbers, plus explicit EMPTY / UNSUPPORTED / ERROR outcomes.
- Per-root, rebuildable knowledge index in its own SQLite database
  (`<vault>/.pa/index.sqlite3`, `$XDG_CACHE_HOME/growing-assistant/knowledge/<root-id>/`),
  bound to `root_id`, `storage_kind` and `schema_version`, using FTS5 `trigram`.
- Whole-document index replacement inside one transaction, so chunks, FTS rows and document
  state can never disagree; a failed attempt removes previously searchable text instead of
  serving it.
- `pw reindex [--root] [--force]` and `pw search QUERY [--root] [--limit]`, with content hits
  carrying `page N` / `lines A-B`, metadata-only hits, and explicit offline-root reporting.
- Literal search semantics for user input: quoted phrase queries with escaped quotes, and a
  `LIKE` fallback with `%`/`_`/`\` escaping for queries shorter than three characters.
- Cross-root result merging by Reciprocal Rank Fusion, because BM25 scores from different
  index databases are not comparable.
- `pw doctor` proves SQLite FTS5 and trigram tokenizer support and reports the pypdf version,
  without creating a database or touching user files.
- Architecture tests: application and domain never import `pypdf`, `tomllib` or the
  store/adapters, ports never import concrete adapters, and only `adapters/content/pdf.py`
  imports `pypdf`.
- Stable storage identity and metadata catalog (ADR-0011):

- Stable storage identity and metadata catalog (ADR-0011):
  `local://<root-id>/<relative-path>` and `vault://<vault-id>/<relative-path>` logical URIs,
  with strict rejection of traversal, absolute paths, backslashes and Windows drive prefixes.
- `.pa/vault.toml` archive vault manifest (`format_version`, `vault_id`, `label`,
  `created_at`) with explicit, never-overwriting, atomically written initialisation.
- `pw vault init|status|scan` nested command group: initialise a manifest, inspect it without
  touching the database, or scan a vault into the host catalog.
- Migration `0003_storage_catalog.sql` adding `storage_roots` and `catalog_entries` (foreign
  key, unique `(root_id, relative_path)`, presence/size/mtime constraints) without touching
  the event-core schema.
- Filesystem metadata scanner: relative paths, `size_bytes`, `mtime_ns` and media type only,
  with symlinks skipped and reported, `.pa` excluded, and structured errors instead of
  crashes.
- Incremental catalog semantics: stable entry ids per `(root_id, relative_path)`, unchanged
  versus updated detection by `size + mtime_ns`, `MISSING` only after a *complete* scan, and
  `restored` when a path reappears.
- `CatalogRepository` port and `SqliteCatalogRepository`; `pw vault scan` reports
  seen/created/updated/unchanged/restored/missing/complete/errors and exits non-zero when a
  scan is incomplete.
- Architecture tests: the application layer never imports adapters/store/sqlite3, the domain
  never touches `os` or `pathlib.Path`, and the metadata scanner never reads or hashes file
  contents.

### Fixed

- The unchanged-skip optimisation now performs a stat-only revalidation before skipping, so a
  file modified without a catalog rescan becomes an ERROR instead of silently keeping stale
  text searchable.

## [0.1.0] - 2026-09-20

Phase 1: the durable event core. Events are ingested, stored, claimed, retried and
dead-lettered durably. No external integration (mail, eHall, model, web) exists yet.

### Added

- Durable event worker (ADR-0010): `EventWorker.run_once`/`run_forever` claim one event,
  run a handler, then complete, retry or dead-letter it.
- Atomic `claim_next` with a lease (`worker_id`, `claim_token`, `claimed_at`,
  `lease_expires_at`) that selects and updates in a single `BEGIN IMMEDIATE` transaction,
  so two workers can never win the same event.
- Fencing: `complete_claim`/`fail_claim` require the current claim token and raise
  `StaleEventClaim` when a lease expired and the event was reclaimed.
- Crash recovery: an abandoned `PROCESSING` event becomes claimable again once its lease
  expires, with a new token and an incremented attempt count.
- Deterministic retry/backoff (`RetryPolicy`: `min(base * 2^(attempts-1), max)`, no jitter)
  and terminal dead lettering via `PermanentEventError` or an exhausted attempt budget.
- Cancellation semantics: `asyncio.CancelledError` propagates instead of being recorded as
  a failure; the event stays `PROCESSING` and is recovered by lease expiry.
- Bounded failure summaries (`format_event_failure`, max 2000 chars) instead of tracebacks
  in the database.
- Migration `0002_event_processing_leases.sql`: rebuilds `inbound_events` to add
  `next_attempt_at`, `claim_token`, `claimed_by`, `claimed_at`, `lease_expires_at` and
  `dead_lettered_at`, extends the status vocabulary with `DEAD_LETTERED`, keeps the partial
  unique index and adds a claim-scan index — preserving every existing row.
- `EventClaim` lease value object, `EventStatus.DEAD_LETTERED` and the `EventHandler` port.
- Async SQLite repository boundary (ADR-0009): `EventRepository` is an async port, the
  SQLite adapter runs each blocking operation in a worker thread, and connections are
  created and closed inside that thread (`Database` is a connection factory).
- `EventInbox` idempotent ingestion API (`application/event_inbox.py`): the single entry
  point for source adapters, returning `CREATED` or `DUPLICATE`.
- `EventRepository.get_by_external_identity`, the read side of the `(source, external_id)`
  identity used by idempotent ingestion.
- Direct SQLite persistence (ADR-0008): required pragmas (`foreign_keys`,
  `journal_mode=WAL`, `busy_timeout`) and explicit `BEGIN IMMEDIATE` transactions.
- Forward-only migration runner with `schema_migrations` bookkeeping, duplicate and
  out-of-order detection, and transactional rollback on failure.
- `InboundEvent` domain model with its state machine, plus the project's vocabulary errors.
- `Clock` port with `SystemClock`, and UTC ISO 8601 serialisation shared by all store modules.
- Architecture tests: layer import rules, async-only repository API, no `sqlite3` outside
  the store, and the sqlite3 thread guard never being disabled.

### Changed

- `pw status` no longer prints a development phase number, and `assistantd` logs
  "assistantd starting" instead of a frozen phase label.
- `Database` no longer holds a live connection: `Database.connect()` opens a configured
  connection in the calling thread and closes it on exit.

## [0.0.1] - 2026-09-19

### Added

- Repository bootstrap with `src/` layout, `assistant` package, `pw` CLI and `assistantd`
  daemon entries.
- Architecture v1 frozen in `docs/specs/0001-system-design.md` and ADRs 0001-0007.
- Project rules for future agent sessions in `AGENTS.md`.
- `uv`-managed environment pinning Python 3.13, with Ruff, mypy and pytest configured.
- Phase 0 tests covering CLI status, environment doctor and daemon lifecycle.
