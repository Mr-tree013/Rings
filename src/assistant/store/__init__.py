"""Store layer: the single SQLite runtime authority (ADR-0003).

This is deliberately a package, not a single module: later phases split it into
`db.py`, `schema.py`, `mail.py`, `cases.py`, `approvals.py`, `knowledge.py`, `audit.py`.
Phase 0 creates no schema and no database logic.
"""

