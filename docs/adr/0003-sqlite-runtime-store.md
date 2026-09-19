# ADR-0003 — SQLite as the Single Runtime Authority

## Title

Use SQLite as the single runtime store for state and full-text search.

## Status

Accepted

## Context

The assistant runs on one machine for one user. State includes event inbox, cases,
approvals, outbox and audit records; the knowledge index needs full-text search over
personal documents. The state must survive crashes, be inspectable, and be trivial to
back up.

## Decision

SQLite is the only runtime store. WAL mode will be enabled when the schema lands.
Full-text search uses SQLite FTS5 (see ADR-0007). No external database server, no ORM
framework in Phase 1 (schema and migrations are explicit SQL).

## Alternatives Considered

- **PostgreSQL**: stronger concurrency and richer types, but a server to install, run
  and back up for a single-user assistant. Rejected.
- **Plain files / JSON state**: human-readable, but no transactions, no atomic
  compare-and-set for approvals and no full-text search. Rejected as the authority;
  Markdown remains the human-readable source for the personal Vault (ADR-0004).
- **Embedded key-value store (e.g. LMDB)**: fast, but pushes query and index
  construction into application code. Rejected.

## Consequences

- Runtime state lives outside the repository, at
  `~/.local/share/growing-assistant/`.
- `store/` stays a package with focused modules so it cannot become a God Object.
- Migration discipline is required: `migrations/` holds explicit, ordered SQL.

