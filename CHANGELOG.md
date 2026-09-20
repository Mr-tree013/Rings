# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
