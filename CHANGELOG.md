# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Async SQLite repository boundary (ADR-0009): `EventRepository` is an async port, the
  SQLite adapter runs each blocking operation in a worker thread, and connections are
  created and closed inside that thread (`Database` is now a connection factory).
- `EventInbox` idempotent ingestion API (`application/event_inbox.py`): the single entry
  point for source adapters, returning `CREATED` or `DUPLICATE` rather than leaking the
  store's `DuplicateInboundEvent` into adapter code.
- `EventRepository.get_by_external_identity`, the read side of the `(source, external_id)`
  identity used by idempotent ingestion.
- Architecture tests for the new boundaries: the application layer never imports the
  store, only infrastructure imports `sqlite3`, the repository API is async, and the
  sqlite3 thread guard is never disabled.

### Changed

- `Database` (Phase 1A) no longer holds a live connection; `Database.connect()` opens a
  configured connection in the calling thread and closes it on exit.

- Direct SQLite persistence (ADR-0008): `store/db.py` owns connection lifecycle, the
  required pragmas (`foreign_keys`, `journal_mode=WAL`, `busy_timeout`) and explicit
  `BEGIN IMMEDIATE` transactions.
- Forward-only migration runner (`store/migrations.py`) with `schema_migrations`
  bookkeeping, duplicate/out-of-order detection and transactional rollback on failure;
  `migrations/0001_initial.sql` creates `inbound_events`.
- `InboundEvent` domain model with its frozen state machine (`RECEIVED`, `PROCESSING`,
  `PROCESSED`, `FAILED`) and the project's vocabulary errors.
- `EventRepository` port plus `SqliteEventRepository`: database-level deduplication of
  `(source, external_id)`, compare-and-set transitions and `list_pending`.
- `Clock` port with `SystemClock`, and UTC ISO 8601 datetime serialisation shared by all
  store modules.

## [0.0.1] - 2026-09-19

### Added

- Repository bootstrap with `src/` layout, `assistant` package, `pw` CLI and `assistantd` daemon entries.
- Architecture v1 frozen in `docs/specs/0001-system-design.md` and ADRs 0001-0007.
- Project rules for future agent sessions in `AGENTS.md`.
- `uv`-managed environment pinning Python 3.13, with Ruff, mypy and pytest configured.
- Phase 0 tests covering CLI status, environment doctor and daemon lifecycle.

### Notes

- No mail, eHall, LLM, indexing, web or scheduling capability exists yet. Everything beyond the
  process skeleton is intentionally deferred to later phases.
