"""The `.gab` archive adapter: a zip written and read under one narrow policy (ADR-0031).

The archive is a zip because the standard library already reads and writes one, and every safety
rule this project needs is a rule about *which members* it is willing to touch. Nothing here
extracts an archive wholesale: members are validated, hashed and written one at a time, into paths
that were checked first.
"""

from assistant.adapters.backup.archive import (
    ArchiveContents,
    ArchiveEntry,
    ZipBackupArchive,
    make_archive,
    open_archive,
)

__all__ = [
    "ArchiveContents",
    "ArchiveEntry",
    "ZipBackupArchive",
    "make_archive",
    "open_archive",
]
