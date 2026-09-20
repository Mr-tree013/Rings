"""Errors raised by the SQLite store implementation.

These describe *implementation* failures (misconfiguration, migration trouble, an
unreadable row). Errors that are part of the project's vocabulary — duplicate events,
illegal transitions, missing records — live in `assistant.domain.errors` instead, so
callers never need to import the store to handle them.
"""

from __future__ import annotations


class StoreError(Exception):
    """Base class for runtime store failures."""


class DatabaseConfigurationError(StoreError):
    """The SQLite connection could not be configured as required."""


class MigrationError(StoreError):
    """A migration file is invalid, out of order, or failed to apply."""


class DatabaseMigrationIncompatible(MigrationError):
    """The database's migration history is newer or unknown to this binary (ADR-0032).

    Running today's queries against tomorrow's schema is how a mismatch becomes corrupt data, so
    this is a refusal rather than a warning: the host has to use the binary that wrote it.
    """


class CatalogStoreError(StoreError):
    """The metadata catalog could not be read or written."""


class CommitmentStoreError(StoreError):
    """Task, deadline, calendar or plan state could not be read or written."""


__all__ = [
    "CatalogStoreError",
    "CommitmentStoreError",
    "DatabaseConfigurationError",
    "DatabaseMigrationIncompatible",
    "MigrationError",
    "StoreError",
]
