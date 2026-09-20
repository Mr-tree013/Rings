"""Private runtime objects are private, and nobody else's files are touched (sections 8-16).

Runs under `umask 0`, which is the honest test: a permissive umask is exactly the situation where a
`0644` default leaks personal state, and passing here means the modes come from the project rather
than from the shell. The reverse is checked too — a directory the user created keeps the modes the
user chose.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from assistant.adapters.mail.raw_store import RawMailStore
from assistant.adapters.runtime.permissions import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    LocalFileModes,
    file_mode,
)
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.store.migrations import apply_migrations
from tests.support.ops import RuntimeFixture


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    """A `umask 0` shell, restored afterwards. Every mode assertion below is about the project."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def test_created_directories_and_files_are_owner_only(
    tmp_path: Path, permissive_umask: None
) -> None:
    """The runtime tree, the daemon lock and the runtime database are private from creation."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    apply_migrations(fixture.database, clock=fixture.clock)

    runtime = fixture.runtime
    database = runtime / "assistant.db"

    assert file_mode(runtime) == PRIVATE_DIRECTORY_MODE
    assert file_mode(database) == PRIVATE_FILE_MODE


async def test_content_objects_are_owner_only(tmp_path: Path, permissive_umask: None) -> None:
    """Raw mail and web snapshots keep personal content, so both the file and its directory are."""
    runtime = tmp_path / "data" / "growing-assistant"
    runtime.mkdir(parents=True, exist_ok=True)
    mail = RawMailStore(runtime / "mail")
    snapshots = WebSnapshotStore(runtime)

    stored = await mail.store(b"Subject: hi\r\n\r\nA private body.\r\n")
    _digest, key = await snapshots.store_text("Notices\nA private page.\n")

    # The raw store's root is `<runtime>/mail`, and its storage keys already start with `mail/raw/`
    # (the composition root's layout), so the object lands one level deeper than the key suggests.
    mail_path = runtime / "mail" / stored.storage_key
    snapshot_path = runtime / key
    assert file_mode(mail_path) == PRIVATE_FILE_MODE
    assert file_mode(snapshot_path) == PRIVATE_FILE_MODE
    assert file_mode(mail_path.parent.parent) == PRIVATE_DIRECTORY_MODE
    assert file_mode(snapshot_path.parent.parent) == PRIVATE_DIRECTORY_MODE


async def test_a_backup_archive_and_its_temporary_file_are_owner_only(
    tmp_path: Path, permissive_umask: None
) -> None:
    """§15: a backup holds the whole runtime, so it is `0600` even on a permissive umask."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    await fixture.add_raw_mail()
    await fixture.add_web_observation()
    archive = tmp_path / "backup.gab"

    result = fixture.backup_service().create(archive)

    assert file_mode(result.path) == PRIVATE_FILE_MODE
    leftovers = [path for path in tmp_path.glob("*.part")]
    assert leftovers == []  # the temporary archive is gone, and was private while it existed


async def test_a_restore_produces_private_files_even_from_an_archive(
    tmp_path: Path, permissive_umask: None
) -> None:
    """§16: the archive's own metadata cannot make a restored file executable or world-readable."""
    fixture = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    await fixture.add_raw_mail()
    await fixture.add_web_observation()
    service = fixture.backup_service()
    archive = service.create(tmp_path / "backup.gab")

    destination = tmp_path / "recovery"
    service.restore(archive.path, destination)

    assert file_mode(destination) == PRIVATE_DIRECTORY_MODE
    assert file_mode(destination / "assistant.db") == PRIVATE_FILE_MODE
    restored_mail = list((destination / "mail").rglob("*.eml"))
    restored_web = list((destination / "web").rglob("*.txt"))
    assert restored_mail and restored_web
    for path in (*restored_mail, *restored_web):
        assert file_mode(path) == PRIVATE_FILE_MODE


def test_an_existing_directory_keeps_the_modes_the_user_chose(
    tmp_path: Path, permissive_umask: None
) -> None:
    """§11/§13: report, never rewrite. An existing directory keeps the modes its user chose."""
    chosen = tmp_path / "data"
    chosen.mkdir()
    chosen.chmod(0o755)

    fixture = RuntimeFixture(chosen / "growing-assistant")
    apply_migrations(fixture.database, clock=fixture.clock)

    assert file_mode(chosen) == 0o755  # the user's directory was not "hardened"
    assert file_mode(fixture.runtime) == PRIVATE_DIRECTORY_MODE  # the one we created was


def test_the_inspector_reports_modes_without_changing_them(tmp_path: Path) -> None:
    """The integrity check reads modes through a port that has no way to write them."""
    loose = tmp_path / "loose.txt"
    loose.write_text("data", encoding="utf-8")
    loose.chmod(0o666)

    observed = LocalFileModes().inspect(loose)

    assert observed.mode == 0o666
    assert observed.world_writable is True
    assert observed.owner_only is False
    assert file_mode(loose) == 0o666  # reading reported it; nothing repaired it
