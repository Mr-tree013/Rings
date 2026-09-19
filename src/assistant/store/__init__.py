"""Store layer: the single SQLite runtime authority (ADR-0003).

This is deliberately a package, not a single module. Current contents:

- `db.py` — connection lifecycle, pragmas, explicit transactions
- `migrations.py` — forward-only migration runner plus `schema_migrations` bookkeeping
- `serialization.py` — UTC ISO 8601 datetime conversion shared by all repositories
- `events.py` — `SqliteEventRepository`
- `errors.py` — implementation-level failures

Later phases add `mail.py`, `cases.py`, `approvals.py`, `knowledge.py`, `audit.py`.
"""
