"""Hostile archives are refused, and nothing lands outside the destination (ADR-0031).

A `.gab` file may arrive from anywhere: a USB stick, a cloud folder, a colleague. So the reader is
built for the assumption that every member is hostile until proven otherwise, and these tests hand
it the classics — traversal, absolute paths, drive letters, duplicates, symlinks, unlisted files,
missing files, wrong hashes, wrong sizes, zip bombs and malformed manifests — and check both the
refusal and the absence of side effects.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from assistant.adapters.backup.archive import open_archive
from assistant.domain.backup import (
    DATABASE_MEMBER,
    MANIFEST_MEMBER,
    BackupCounts,
    BackupManifest,
    BackupObject,
    canonical_json,
)
from assistant.domain.errors import InvalidBackupArchive
from tests.support.ops import NOW, RuntimeFixture

DIGEST = "d" * 64
MAIL_KEY = f"mail/raw/{DIGEST[:2]}/{DIGEST}.eml"


def _manifest(**overrides: object) -> BackupManifest:
    values: dict[str, object] = {
        "application_version": "0.8.0",
        "created_at": NOW,
        "database_sha256": "e" * 64,
        "migration_files": ("0001_initial.sql",),
        "counts": BackupCounts(),
    }
    values.update(overrides)
    return BackupManifest(**values)  # type: ignore[arg-type]


def _write_archive(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    manifest: BackupManifest | None = None,
    duplicate_manifest: bool = False,
) -> Path:
    """Build an archive by hand, so a test can write a member the writer would refuse."""
    document = (manifest or _manifest()).to_json().encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, document)
        if duplicate_manifest:
            archive.writestr(MANIFEST_MEMBER, document)
        for name, payload in members:
            archive.writestr(name, payload)
    return path


# ------------------------------------------------------------------ name attacks


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "mail/../../escape.txt",
        "/absolute.txt",
        "C:\\escape.txt",
        "mail\\raw\\escape.txt",
        "mail/raw/aa/x.eml",
        "web/snapshots/cc/" + "c" * 64 + ".bak",
        "config.toml",
        ".env",
        "ehall/nju-profile/Cookies",
    ],
)
def test_a_hostile_member_name_is_refused(tmp_path: Path, name: str) -> None:
    archive = _write_archive(tmp_path / "hostile.gab", [(name, b"payload")])

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)

    assert not list(tmp_path.glob("escape*"))
    assert not (tmp_path / "absolute.txt").exists()


def test_a_duplicate_member_is_refused(tmp_path: Path) -> None:
    # `zipfile` warns when a writer deliberately repeats a name; that warning is the test's own
    # fixture speaking, not a product problem, so it is asserted rather than left to noise.
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _write_archive(
            tmp_path / "duplicate.gab",
            [(MAIL_KEY, b"a"), (MAIL_KEY, b"b")],
        )

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_a_duplicate_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _write_archive(
            tmp_path / "two-manifests.gab", [], duplicate_manifest=True
        )

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_a_symlink_member_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.gab"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(MANIFEST_MEMBER, _manifest().to_json())
        info = zipfile.ZipInfo(MAIL_KEY)
        info.external_attr = (0o120777 << 16)  # a symbolic link, in zip's Unix mode bits
        handle.writestr(info, "/etc/passwd")

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_an_unlisted_member_is_refused(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "unlisted.gab", [(MAIL_KEY, b"payload")])

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_a_listed_but_missing_member_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(
        mail_objects=(BackupObject(storage_key=MAIL_KEY, sha256=DIGEST, size_bytes=7),)
    )
    archive = _write_archive(tmp_path / "missing.gab", [], manifest=manifest)

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_a_member_with_a_wrong_hash_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(
        mail_objects=(BackupObject(storage_key=MAIL_KEY, sha256=DIGEST, size_bytes=7),)
    )
    archive = _write_archive(
        tmp_path / "wrong-hash.gab",
        [(DATABASE_MEMBER, b"payload"), (MAIL_KEY, b"payload")],
        manifest=manifest,
    )
    handle = open_archive(archive)

    with pytest.raises(InvalidBackupArchive):
        handle.extract(MAIL_KEY, tmp_path / "out.eml")

    assert not (tmp_path / "out.eml").exists()


def test_a_member_with_a_wrong_size_is_refused(tmp_path: Path) -> None:
    import hashlib

    payload = b"payload"
    manifest = _manifest(
        mail_objects=(
            BackupObject(
                storage_key=MAIL_KEY,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload) + 1,
            ),
        )
    )
    archive = _write_archive(
        tmp_path / "wrong-size.gab",
        [(DATABASE_MEMBER, b"payload"), (MAIL_KEY, payload)],
        manifest=manifest,
    )
    handle = open_archive(archive)

    with pytest.raises(InvalidBackupArchive):
        handle.extract(MAIL_KEY, tmp_path / "out.eml")

    assert not (tmp_path / "out.eml").exists()


def test_a_zip_bomb_ratio_is_refused(tmp_path: Path) -> None:
    huge = b"\0" * (8 * 1024 * 1024)
    archive = _write_archive(
        tmp_path / "bomb.gab",
        [("web/snapshots/cc/" + "c" * 64 + ".txt", huge)],
    )
    manifest = _manifest(
        web_snapshots=(
            BackupObject(
                storage_key="web/snapshots/cc/" + "c" * 64 + ".txt",
                sha256="c" * 64,
                size_bytes=len(huge),
            ),
        )
    )
    archive = _write_archive(
        tmp_path / "bomb.gab",
        [("web/snapshots/cc/" + "c" * 64 + ".txt", huge)],
        manifest=manifest,
    )

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


@pytest.mark.parametrize(
    "document",
    [
        b"not json",
        b"[]",
        canonical_json({"format_version": 99}).encode("utf-8"),
        canonical_json({"manifest": "without the right keys"}).encode("utf-8"),
    ],
)
def test_a_malformed_manifest_is_refused(tmp_path: Path, document: bytes) -> None:
    archive = tmp_path / "manifest.gab"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(MANIFEST_MEMBER, document)
        handle.writestr(DATABASE_MEMBER, b"not a database")

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "not-a-zip.gab"
    archive.write_bytes(b"this is not a zip archive")

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)
    with pytest.raises(InvalidBackupArchive):
        open_archive(tmp_path / "missing.gab")


def test_an_unsupported_format_version_is_refused(tmp_path: Path) -> None:
    document = json.loads(_manifest().to_json())
    document["format_version"] = 2
    archive = _write_archive(
        tmp_path / "future.gab",
        [(DATABASE_MEMBER, b"payload")],
        manifest=_manifest(),
    )
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(archive, "a") as handle,
    ):
        handle.writestr(MANIFEST_MEMBER, canonical_json(document))

    with pytest.raises(InvalidBackupArchive):
        open_archive(archive)


def test_nothing_is_written_outside_the_destination_by_a_restore_attempt(
    tmp_path: Path,
) -> None:
    """§66: whichever attack is used, the destination's parent gains no file."""
    runtime = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    service = runtime.backup_service()
    good = service.create(tmp_path / "good.gab")
    hostile = _write_archive(
        tmp_path / "hostile.gab", [("../escape.txt", b"payload")]
    )
    destination = tmp_path / "recovered"
    before = sorted(path.name for path in tmp_path.iterdir())

    with pytest.raises(InvalidBackupArchive):
        service.restore(hostile, destination)

    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not destination.exists()
    assert good.path.is_file()


def test_a_restore_of_a_damaged_database_leaves_no_destination(
    tmp_path: Path,
) -> None:
    """§68: a database that fails its pragmas never becomes a restored runtime."""
    import hashlib

    runtime = RuntimeFixture(tmp_path / "data" / "growing-assistant")
    runtime.backup_service().create(tmp_path / "good.gab")
    payload = b"SQLite format 3\x00 but truncated"
    manifest = _manifest(database_sha256=hashlib.sha256(payload).hexdigest())
    broken = _write_archive(
        tmp_path / "broken.gab",
        [(DATABASE_MEMBER, payload)],
        manifest=manifest,
    )
    destination = tmp_path / "recovered"

    with pytest.raises(InvalidBackupArchive):
        runtime.backup_service().restore(broken, destination)

    assert not destination.exists()
