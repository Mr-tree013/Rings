"""End-to-end catalog tests: real directories, real symlink rules, real SQLite (ADR-0011)."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import CatalogPresence
from assistant.domain.errors import VaultNotInitialized
from assistant.domain.storage import StorageKind
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

START = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Iterator[Database]:
    db = Database.at(tmp_path / "host" / "assistant.db")
    apply_migrations(db, clock=clock)
    yield db


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "usb" / "archive"
    root.mkdir(parents=True)
    return root


def _service(database: Database, clock: FakeClock) -> StorageCatalogService:
    return StorageCatalogService(
        FilesystemScanner(clock),
        VaultManifestFile(clock),
        SqliteCatalogRepository(database),
        clock,
    )


async def _initialise(root: Path, clock: FakeClock, vault_id: str = "archive-main") -> None:
    await VaultManifestFile(clock).initialize(root, vault_id=vault_id, label="Personal Archive")


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def test_scan_vault_catalogues_files_under_the_manifest_identity(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / "Courses" / "OS" / "report.pdf")
    _write(vault / "README.md")
    service = _service(database, clock)

    result = await service.scan_vault(vault)

    repository = SqliteCatalogRepository(database)
    stored_root = await repository.get_root("archive-main")
    entries = await repository.list_by_root("archive-main")
    assert result.created == 2
    assert result.scan_complete
    assert stored_root is not None
    assert stored_root.root.kind is StorageKind.VAULT
    assert stored_root.last_known_path == str(vault)
    assert [str(item.logical_uri) for item in entries] == [
        "vault://archive-main/Courses/OS/report.pdf",
        "vault://archive-main/README.md",
    ]


async def test_scanning_an_uninitialised_directory_is_refused(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    plain = tmp_path / "usb" / "not-a-vault"
    plain.mkdir(parents=True)
    _write(plain / "a.txt")

    with pytest.raises(VaultNotInitialized):
        await _service(database, clock).scan_vault(plain)

    repository = SqliteCatalogRepository(database)
    assert await repository.list_by_root("not-a-vault") == []
    assert not (plain / ".pa").exists()


async def test_the_internal_pa_directory_never_reaches_the_catalog(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / ".pa" / "secret-internal.txt")
    _write(vault / "Courses" / "report.pdf")

    await _service(database, clock).scan_vault(vault)

    entries = await SqliteCatalogRepository(database).list_by_root(
        "archive-main", include_missing=True
    )
    assert [item.relative_path for item in entries] == ["Courses/report.pdf"]


async def test_an_offline_vault_keeps_its_catalog_metadata(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    """The core archive behaviour: unplugging a drive must not erase document identity."""
    await _initialise(vault, clock)
    _write(vault / "Courses" / "OS" / "report.pdf")
    service = _service(database, clock)
    await service.scan_vault(vault)
    repository = SqliteCatalogRepository(database)
    before = (await repository.list_by_root("archive-main"))[0]

    shutil.rmtree(vault)
    clock.advance(3600)

    entries = await repository.list_by_root("archive-main")
    stored_root = await repository.get_root("archive-main")
    assert entries[0].id == before.id
    assert entries[0].presence is CatalogPresence.PRESENT
    assert entries[0].size_bytes == before.size_bytes
    assert stored_root is not None
    assert stored_root.last_known_path == str(vault)


async def test_deleted_files_are_marked_missing_and_restored_with_the_same_identity(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / "a.txt")
    _write(vault / "b.txt")
    service = _service(database, clock)
    repository = SqliteCatalogRepository(database)
    await service.scan_vault(vault)
    stored = await repository.list_by_root("archive-main")
    original = next(item for item in stored if item.relative_path == "b.txt")

    (vault / "b.txt").unlink()
    clock.advance(60)
    deleted = await service.scan_vault(vault)
    missing = await repository.get_by_location(
        storage_kind=StorageKind.VAULT, root_id="archive-main", relative_path="b.txt"
    )

    assert deleted.marked_missing == 1
    assert missing is not None
    assert missing.presence is CatalogPresence.MISSING

    _write(vault / "b.txt", "back again")
    clock.advance(60)
    restored = await service.scan_vault(vault)
    revived = (await repository.list_by_root("archive-main"))[1]
    assert restored.restored == 1
    assert restored.created == 0
    assert revived.id == original.id
    assert revived.relative_path == "b.txt"
    assert revived.presence is CatalogPresence.PRESENT


async def test_an_incomplete_scan_leaves_unseen_entries_alone(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    await _initialise(vault, clock)
    _write(vault / "a.txt")
    locked = vault / "locked"
    _write(locked / "b.txt")
    service = _service(database, clock)
    repository = SqliteCatalogRepository(database)
    await service.scan_vault(vault)

    locked.chmod(0o000)
    clock.advance(60)
    try:
        result = await service.scan_vault(vault)
    finally:
        locked.chmod(0o700)

    entries = await repository.list_by_root("archive-main", include_missing=True)
    assert not result.scan_complete
    assert result.marked_missing == 0
    assert result.errors == 1
    assert {item.relative_path for item in entries} == {"a.txt", "locked/b.txt"}
    assert all(item.is_present for item in entries)


async def test_moving_the_vault_to_another_mount_path_keeps_document_identity(
    database: Database, clock: FakeClock, vault: Path, tmp_path: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / "Courses" / "OS" / "report.pdf")
    service = _service(database, clock)
    repository = SqliteCatalogRepository(database)
    await service.scan_vault(vault)
    before = (await repository.list_by_root("archive-main"))[0]

    second_mount = tmp_path / "usb2" / "archive"
    second_mount.parent.mkdir(parents=True)
    shutil.copytree(vault, second_mount, symlinks=True)
    clock.advance(3600)
    await service.scan_vault(second_mount)

    entries = await repository.list_by_root("archive-main", include_missing=True)
    stored_root = await repository.get_root("archive-main")
    assert len(entries) == 1
    assert entries[0].id == before.id
    assert str(entries[0].logical_uri) == before.logical_uri.__str__()
    assert stored_root is not None
    assert stored_root.last_known_path == str(second_mount)


async def test_local_roots_use_the_callers_identity(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    folder = tmp_path / "random-folder-name"
    _write(folder / "Lab1" / "report.pdf")
    service = _service(database, clock)
    repository = SqliteCatalogRepository(database)

    await service.scan_local(root_id="university", label="University", path=folder)
    first = (await repository.list_by_root("university"))[0]

    moved = tmp_path / "another-location"
    shutil.copytree(folder, moved)
    clock.advance(3600)
    await service.scan_local(root_id="university", label="University", path=moved)

    entries = await repository.list_by_root("university", include_missing=True)
    stored_root = await repository.get_root("university")
    assert len(entries) == 1
    assert entries[0].id == first.id
    assert str(entries[0].logical_uri) == "local://university/Lab1/report.pdf"
    assert stored_root is not None
    assert stored_root.root.kind is StorageKind.LOCAL
    assert stored_root.last_known_path == str(moved)


async def test_rescanning_an_unchanged_vault_is_idempotent(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / "a.txt")
    _write(vault / "b.txt")
    service = _service(database, clock)
    repository = SqliteCatalogRepository(database)
    await service.scan_vault(vault)
    before = await repository.list_by_root("archive-main")

    clock.advance(60)
    second = await service.scan_vault(vault)
    after = await repository.list_by_root("archive-main")

    assert second.created == 0
    assert second.unchanged == 2
    assert [item.id for item in after] == [item.id for item in before]


async def test_two_services_scanning_one_vault_concurrently_do_not_corrupt_the_catalog(
    database: Database, clock: FakeClock, vault: Path
) -> None:
    await _initialise(vault, clock)
    _write(vault / "a.txt")
    _write(vault / "b.txt")
    first = _service(database, clock)
    second = _service(database, clock)

    results = await asyncio.gather(first.scan_vault(vault), second.scan_vault(vault))

    repository = SqliteCatalogRepository(database)
    entries = await repository.list_by_root("archive-main", include_missing=True)
    with database.connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        roots = connection.execute("SELECT count(*) FROM storage_roots").fetchone()[0]
    assert len(results) == 2
    assert [item.relative_path for item in entries] == ["a.txt", "b.txt"]
    assert all(item.is_present for item in entries)
    assert integrity == "ok"
    assert roots == 1
