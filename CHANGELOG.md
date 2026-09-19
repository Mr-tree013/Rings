# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
