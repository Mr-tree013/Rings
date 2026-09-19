"""The commitment revision: transactional fencing for plan proposals (ADR-0015).

Every mutation that can change planning input bumps the revision **inside the same
transaction** as the business change, and applying a proposal requires the revision it was
built from. That closes the race between "the service computed a fingerprint" and "the
database writes the plan".

Only synchronous, connection-level helpers live here: the connection belongs to the caller's
transaction, and no connection ever leaks out of the store layer.
"""

from __future__ import annotations

import sqlite3

from assistant.store.errors import CommitmentStoreError

REVISION_KEY = "revision"


def read_revision(connection: sqlite3.Connection) -> int:
    """Return the current commitment revision."""
    row = connection.execute(
        "SELECT value FROM commitment_meta WHERE key = ?", (REVISION_KEY,)
    ).fetchone()
    if row is None:
        raise CommitmentStoreError("commitment_meta has no revision row")
    try:
        return int(str(row["value"]))
    except ValueError as exc:  # pragma: no cover - defensive
        raise CommitmentStoreError(
            f"commitment revision is not an integer: {row['value']!r}"
        ) from exc


def increment_revision(connection: sqlite3.Connection) -> int:
    """Bump the revision once and return the new value.

    Raises:
        CommitmentStoreError: called outside a transaction, which would defeat the point of
            fencing a mutation.
    """
    if not connection.in_transaction:
        raise CommitmentStoreError(
            "the commitment revision must be incremented inside the mutation transaction"
        )
    updated = read_revision(connection) + 1
    connection.execute(
        "UPDATE commitment_meta SET value = ? WHERE key = ?", (str(updated), REVISION_KEY)
    )
    return updated


__all__ = ["REVISION_KEY", "increment_revision", "read_revision"]

