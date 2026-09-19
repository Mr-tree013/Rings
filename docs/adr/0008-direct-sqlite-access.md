# ADR-0008 — Direct `sqlite3` Access Behind Repository Ports

## Title

Use the Python standard library `sqlite3` driver directly, behind repository ports, with
hand-written SQL and hand-written migrations.

## Status

Accepted

## Context

Phase 1A needs durable storage for `InboundEvent` with a database-level idempotency
guarantee. The properties that make this storage trustworthy are SQLite features, not
Python features: a partial unique index for `(source, external_id)`, `CHECK` constraints
for status and attempts, WAL mode, `busy_timeout`, and — later — FTS5 for knowledge
retrieval (ADR-0007). An ORM would sit between the code and exactly those features, and
the project's stated priority is explainability over framework convenience.

## Decision

- The runtime store uses the standard library `sqlite3` module. No SQLAlchemy, no
  aiosqlite, no other ORM or driver in Phase 1.
- All SQL lives in the `store/` package. Domain and application code neither execute SQL
  nor import `sqlite3`; they depend on repository ports.
- Transactions are explicit: connections are opened with `isolation_level=None`, and
  every multi-statement operation runs inside an explicit `BEGIN IMMEDIATE` …
  `COMMIT`/`ROLLBACK` block.
- Schema changes are plain SQL files in `migrations/`, applied by a small forward-only
  runner with `schema_migrations` bookkeeping. No Alembic.
- If performance or complexity ever demands a different implementation, the
  `EventRepository` port is what gets swapped; the SQL is not spread through the codebase.

## Alternatives Considered

- **SQLAlchemy (Core or ORM)**: mapping convenience and dialect portability, but it hides
  the constraint and transaction semantics this phase exists to make explicit, and adds a
  large dependency for one user on one machine. Rejected.
- **aiosqlite**: keeps the asyncio event loop free, but adds a dependency and a second
  concurrency model; the daemon can offload blocking store calls to a thread instead.
  Rejected for now, reconsider only with measurements.
- **Raw SQL scattered at call sites**: no dependency, but deduplication and transition
  logic would be re-implemented per caller — the exact failure mode ADR-0002 warns about.
  Rejected.
- **A schema-migration framework**: heavier than a forward-only runner, and it would own
  a decision (transactional migration application) that the project wants to see plainly.
  Rejected.

## Consequences

- Zero new production dependencies; the SQL that enforces safety is readable in
  `migrations/0001_initial.sql` and reviewable in diffs.
- Every repository behaviour needs an integration test against a real database file;
  mocks cannot demonstrate uniqueness or rollback.
- No portability layer: moving off SQLite later means rewriting `store/`, which the port
  boundary makes a contained change.
- `sqlite3` is blocking, so the future asyncio daemon must not call the store directly on
  the event loop; store calls belong in a worker thread. This is recorded here so the
  constraint is not rediscovered later.

