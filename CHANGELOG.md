# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
