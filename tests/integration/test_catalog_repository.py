"""Integration tests for the SQLite metadata catalog (ADR-0011).

Real SQLite files, no mocks: incremental update rules, missing/restored detection,
incomplete-scan protection and mount-path independence are exactly the guarantees a mock
would fabricate. The catalog must also survive being scanned by two workers at once.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.domain.catalog import (
    CatalogPresence,
    FileSnapshotEntry,
    FilesystemSnapshot,
    ScanError,
)
from assistant.domain.errors import StorageRootConflict
from assistant.domain.storage import StorageKind, StorageRoot, StorageUri
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

START = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class EntryIdFactory:
    """Deterministic catalog entry ids: 1, 2, 3, ..."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


@pytest.fixture
def repository(database: Database) -> SqliteCatalogRepository:
    return SqliteCatalogRepository(database, new_entry_id=EntryIdFactory())


def entry(
    relative_path: str,
    *,
    size: int = 10,
    mtime: int = 1_000_000,
    media_type: str | None = "text/plain",
) -> FileSnapshotEntry:
    return FileSnapshotEntry(
        relative_path=relative_path,
        name=relative_path.rsplit("/", 1)[-1],
        size_bytes=size,
        mtime_ns=mtime,
        media_type=media_type,
    )


def snapshot(
    *entries: FileSnapshotEntry, complete: bool = True, errors: tuple[ScanError, ...] = ()
) -> FilesystemSnapshot:
    return FilesystemSnapshot(
        entries=entries,
        complete=complete,
        errors=errors,
        skipped_symlinks=(),
        started_at=START,
        finished_at=START,
    )


def vault_root(root_id: str = "archive-main", label: str = "Personal Archive") -> StorageRoot:
    return StorageRoot(root_id=root_id, kind=StorageKind.VAULT, label=label)


async def _apply(
    repository: SqliteCatalogRepository,
    clock: FakeClock,
    *snapshots: FilesystemSnapshot,
    root: StorageRoot | None = None,
    physical_path: str = "/mnt/e/archive",
): 
    results = []
    for item in snapshots:
        results.append(
            await repository.apply_snapshot(
                root=root if root is not None else vault_root(),
                physical_path=physical_path,
                snapshot=item,
                scan_id=uuid4(),
                scanned_at=clock.now(),
            )
        )
    return results


async def test_first_scan_registers_the_root_and_creates_entries(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    result = (
        await _apply(repository, clock, snapshot(entry("a.txt"), entry("notes/b.md")))
    )[0]

    stored_root = await repository.get_root("archive-main")
    entries = await repository.list_by_root("archive-main")
    assert result.created == 2
    assert result.seen == 2
    assert result.marked_missing == 0
    assert result.scan_complete
    assert stored_root is not None
    assert stored_root.root.kind is StorageKind.VAULT
    assert stored_root.root.label == "Personal Archive"
    assert stored_root.last_known_path == "/mnt/e/archive"
    assert stored_root.last_scanned_at == START
    assert [item.relative_path for item in entries] == ["a.txt", "notes/b.md"]
    assert str(entries[0].logical_uri) == "vault://archive-main/a.txt"
    assert entries[0].presence is CatalogPresence.PRESENT


async def test_identical_rescan_is_unchanged_and_keeps_ids_and_metadata_time(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    first = (await _apply(repository, clock, snapshot(entry("a.txt"))))[0]
    before = await repository.list_by_root("archive-main")

    clock.advance(3600)
    second = (
        await _apply(repository, clock, snapshot(entry("a.txt")))
    )[0]
    after = await repository.list_by_root("archive-main")

    assert first.created == 1
    assert second.unchanged == 1
    assert second.created == 0
    assert second.updated == 0
    assert after[0].id == before[0].id
    assert after[0].metadata_updated_at == before[0].metadata_updated_at
    assert after[0].last_seen_at == clock.now()


async def test_changed_metadata_updates_the_entry_but_keeps_its_identity(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    (await _apply(repository, clock, snapshot(entry("a.txt", size=10))))[0]
    before = (await repository.list_by_root("archive-main"))[0]

    clock.advance(60)
    result = (
        await _apply(repository, clock, snapshot(entry("a.txt", size=99, mtime=2_000_000)))
    )[0]
    after = (await repository.list_by_root("archive-main"))[0]

    assert result.updated == 1
    assert result.created == 0
    assert after.id == before.id
    assert after.size_bytes == 99
    assert after.mtime_ns == 2_000_000
    assert after.metadata_updated_at == clock.now()


async def test_a_complete_scan_marks_unseen_entries_missing(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt"), entry("b.txt")))

    clock.advance(60)
    result = (await _apply(repository, clock, snapshot(entry("a.txt"))))[0]

    present = await repository.list_by_root("archive-main")
    everything = await repository.list_by_root("archive-main", include_missing=True)
    assert result.marked_missing == 1
    assert [item.relative_path for item in present] == ["a.txt"]
    assert [item.relative_path for item in everything] == ["a.txt", "b.txt"]
    missing = next(item for item in everything if item.relative_path == "b.txt")
    assert missing.presence is CatalogPresence.MISSING
    assert not missing.is_present
    assert missing.metadata_updated_at == clock.now()


async def test_a_reappearing_file_is_restored_with_the_same_identity(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("b.txt")))
    original = (await repository.list_by_root("archive-main"))[0]
    await _apply(repository, clock, snapshot())

    clock.advance(60)
    result = (await _apply(repository, clock, snapshot(entry("b.txt"))))[0]
    restored = (await repository.list_by_root("archive-main"))[0]

    assert result.restored == 1
    assert result.created == 0
    assert restored.id == original.id
    assert restored.presence is CatalogPresence.PRESENT
    assert str(restored.logical_uri) == "vault://archive-main/b.txt"


async def test_an_incomplete_scan_never_marks_anything_missing(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt"), entry("b.txt")))
    seen_before = await repository.list_by_root("archive-main")

    clock.advance(60)
    incomplete = snapshot(
        entry("a.txt"),
        complete=False,
        errors=(ScanError(relative_path="b.txt", error_type="PermissionError", message="denied"),),
    )
    result = (await _apply(repository, clock, incomplete))[0]

    after = await repository.list_by_root("archive-main")
    stored_root = await repository.get_root("archive-main")
    assert result.marked_missing == 0
    assert not result.scan_complete
    assert result.errors == 1
    assert [item.relative_path for item in after] == ["a.txt", "b.txt"]
    assert all(item.is_present for item in after)
    assert stored_root is not None
    assert stored_root.last_scanned_at == START  # only a complete scan counts as a scan
    assert seen_before[0].id == after[0].id


async def test_entries_are_scoped_per_root(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(
        repository, clock, snapshot(entry("shared/report.pdf")), root=vault_root("archive-main")
    )
    await _apply(
        repository,
        clock,
        snapshot(entry("shared/report.pdf")),
        root=StorageRoot(root_id="university", kind=StorageKind.LOCAL, label="University"),
        physical_path="/home/mrtree/University",
    )

    vault_entry = await repository.get_by_location(
        storage_kind=StorageKind.VAULT, root_id="archive-main", relative_path="shared/report.pdf"
    )
    local_entry = await repository.get_by_location(
        storage_kind=StorageKind.LOCAL, root_id="university", relative_path="shared/report.pdf"
    )
    assert vault_entry is not None
    assert local_entry is not None
    assert vault_entry.id != local_entry.id
    assert str(local_entry.logical_uri) == "local://university/shared/report.pdf"
    assert await repository.get_by_uri(
        StorageUri(
            kind=StorageKind.LOCAL, root_id="university", relative_path="shared/report.pdf"
        )
    ) is not None
    assert await repository.get_by_uri(
        StorageUri(
            kind=StorageKind.VAULT, root_id="university", relative_path="shared/report.pdf"
        )
    ) is None


async def test_a_root_id_cannot_change_kind(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt")))

    with pytest.raises(StorageRootConflict, match="cannot become local"):
        await _apply(
            repository,
            clock,
            snapshot(entry("a.txt")),
            root=StorageRoot(root_id="archive-main", kind=StorageKind.LOCAL, label="Local"),
        )

    stored_root = await repository.get_root("archive-main")
    assert stored_root is not None
    assert stored_root.root.kind is StorageKind.VAULT


async def test_label_and_physical_path_may_change_without_touching_identity(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt")), physical_path="/mnt/e/archive")
    before = (await repository.list_by_root("archive-main"))[0]

    clock.advance(60)
    await _apply(
        repository,
        clock,
        snapshot(entry("a.txt")),
        root=vault_root(label="Archive (renamed)"),
        physical_path="/mnt/f/archive",
    )

    stored_root = await repository.get_root("archive-main")
    after = (await repository.list_by_root("archive-main"))[0]
    assert stored_root is not None
    assert stored_root.root.label == "Archive (renamed)"
    assert stored_root.last_known_path == "/mnt/f/archive"
    assert after.id == before.id
    assert after.relative_path == "a.txt"


async def test_list_by_root_supports_missing_and_limit(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt"), entry("b.txt"), entry("c.txt")))
    await _apply(repository, clock, snapshot(entry("a.txt")))

    limited = await repository.list_by_root("archive-main", limit=1)
    assert [item.relative_path for item in limited] == ["a.txt"]
    assert len(await repository.list_by_root("archive-main", include_missing=True)) == 3
    with pytest.raises(ValueError, match="limit"):
        await repository.list_by_root("archive-main", limit=0)
    assert await repository.get_root("unknown-root") is None


async def test_scan_result_counters_partition_the_snapshot(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("keep.txt"), entry("change.txt", size=1)))

    clock.advance(60)
    result = (
        await _apply(
            repository,
            clock,
            snapshot(entry("keep.txt"), entry("change.txt", size=2), entry("new.txt")),
        )
    )[0]

    assert (result.created, result.updated, result.unchanged, result.restored) == (1, 1, 1, 0)
    assert result.created + result.updated + result.unchanged + result.restored == result.seen
    assert result.marked_missing == 0


async def test_a_changed_entry_that_reappears_counts_as_restored(
    repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    """Restored wins over updated: an entry is counted once, and coming back is the story."""
    await _apply(repository, clock, snapshot(entry("gone.txt", size=1)))
    original = (await repository.list_by_root("archive-main"))[0]
    deleted = (await _apply(repository, clock, snapshot()))[0]

    clock.advance(60)
    result = (await _apply(repository, clock, snapshot(entry("gone.txt", size=2))))[0]
    restored = (await repository.list_by_root("archive-main"))[0]

    assert deleted.marked_missing == 1
    assert (result.restored, result.updated, result.created) == (1, 0, 0)
    assert restored.id == original.id
    assert restored.size_bytes == 2
    assert restored.metadata_updated_at == clock.now()


async def test_concurrent_scans_of_one_root_do_not_corrupt_the_catalog(
    database: Database, clock: FakeClock
) -> None:
    first = SqliteCatalogRepository(database, new_entry_id=EntryIdFactory())
    second = SqliteCatalogRepository(database, new_entry_id=EntryIdFactory())
    items = (entry("a.txt"), entry("b.txt"))

    results = await asyncio.gather(
        first.apply_snapshot(
            root=vault_root(),
            physical_path="/mnt/e/archive",
            snapshot=snapshot(*items),
            scan_id=uuid4(),
            scanned_at=clock.now(),
        ),
        second.apply_snapshot(
            root=vault_root(),
            physical_path="/mnt/e/archive",
            snapshot=snapshot(*items),
            scan_id=uuid4(),
            scanned_at=clock.now(),
        ),
    )

    entries = await first.list_by_root("archive-main", include_missing=True)
    assert len(results) == 2
    assert [item.relative_path for item in entries] == ["a.txt", "b.txt"]
    assert all(item.is_present for item in entries)
    with database.connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        duplicate_rows = connection.execute(
            "SELECT count(*) FROM (SELECT root_id, relative_path FROM catalog_entries "
            "GROUP BY root_id, relative_path HAVING count(*) > 1)"
        ).fetchone()[0]
    assert integrity == "ok"
    assert duplicate_rows == 0


async def test_database_constraints_are_enforced(
    database: Database, repository: SqliteCatalogRepository, clock: FakeClock
) -> None:
    await _apply(repository, clock, snapshot(entry("a.txt")))

    def raw_insert(
        *,
        presence: str = "present",
        size_bytes: int = 1,
        root: str = "archive-main",
    ) -> None:
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO catalog_entries (
                    id, root_id, relative_path, name, suffix, size_bytes, mtime_ns,
                    media_type, presence, first_seen_at, last_seen_at,
                    metadata_updated_at, last_seen_scan_id
                ) VALUES (?, ?, 'dup.txt', 'dup.txt', '.txt', ?, 1, NULL, ?, ?, ?, ?, 'scan')
                """,
                (
                    str(uuid4()),
                    root,
                    size_bytes,
                    presence,
                    "2026-09-20T09:00:00.000000+00:00",
                    "2026-09-20T09:00:00.000000+00:00",
                    "2026-09-20T09:00:00.000000+00:00",
                ),
            )

    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(presence="something-else")
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(size_bytes=-1)
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(root="not-a-root")

    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'a.txt', 'a.txt', '.txt', 1, 1, NULL, 'present', ?, ?, ?, 's')",
            (
                str(uuid4()),
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
                "2026-09-20T09:00:00.000000+00:00",
            ),
        )


async def test_mount_path_change_keeps_document_identity(
    database: Database, clock: FakeClock
) -> None:
    """The same vault seen at a different mount point must not create a second identity."""
    repository = SqliteCatalogRepository(database, new_entry_id=EntryIdFactory())
    await _apply(repository, clock, snapshot(entry("Courses/OS/report.pdf")))
    before = (await repository.list_by_root("archive-main"))[0]

    clock.advance(timedelta(hours=1).seconds)
    await _apply(
        repository, clock, snapshot(entry("Courses/OS/report.pdf")), physical_path="/mnt/g/archive"
    )

    entries = await repository.list_by_root("archive-main", include_missing=True)
    stored_root = await repository.get_root("archive-main")
    assert len(entries) == 1
    assert entries[0].id == before.id
    assert str(entries[0].logical_uri) == "vault://archive-main/Courses/OS/report.pdf"
    assert stored_root is not None
    assert stored_root.last_known_path == "/mnt/g/archive"
