# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
