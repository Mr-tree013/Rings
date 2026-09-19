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


class CatalogStoreError(StoreError):
    """The metadata catalog could not be read or written."""


class CommitmentStoreError(StoreError):
    """Task, deadline, calendar or plan state could not be read or written."""


__all__ = [
    "CatalogStoreError",
    "CommitmentStoreError",
    "DatabaseConfigurationError",
    "MigrationError",
    "StoreError",
]
