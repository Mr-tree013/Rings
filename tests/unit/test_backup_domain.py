"""The archive format's values: member safety, manifest shape and finite bounds (ADR-0031).

The format is deliberately narrow. Four top-level kinds exist, every member name is checked before
anything is opened, the manifest is canonical and strict, and every limit is a number rather than an
intention. A malformed or hostile archive is refused, not interpreted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.domain.backup import (
    DATABASE_MEMBER,
    FORMAT_VERSION,
    MAIL_MEMBER_PREFIX,
    MANIFEST_MEMBER,
    MAX_OBJECT_BYTES,
    WEB_MEMBER_PREFIX,
    BackupCounts,
    BackupManifest,
    BackupObject,
    canonical_json,
    is_allowed_member,
    is_safe_member_name,
    sha256_hex,
)
from assistant.domain.errors import InvalidBackupArchive

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _object(key: str = MAIL_MEMBER_PREFIX + "aa/" + DIGEST + ".eml") -> BackupObject:
    return BackupObject(storage_key=key, sha256=DIGEST, size_bytes=128)


def _manifest(**overrides: object) -> BackupManifest:
    values: dict[str, object] = {
        "application_version": "0.8.0",
        "created_at": NOW,
        "database_sha256": "b" * 64,
        "migration_files": ("0001_initial.sql", "0015_inbound_observations.sql"),
        "mail_objects": (_object(),),
        "web_snapshots": (
            _object(WEB_MEMBER_PREFIX + "cc/" + "c" * 64 + ".txt"),
        ),
    }
    values.update(overrides)
    return BackupManifest(**values)  # type: ignore[arg-type]


# ----------------------------------------------------------------------- hashing


def test_hashing_is_lowercase_sha256() -> None:
    digest = sha256_hex(b"hello")

    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert digest == digest.lower()


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"text": "héllo"}) == '{"text":"héllo"}'


# ------------------------------------------------------------------ member names


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "runtime.sqlite3",
        MAIL_MEMBER_PREFIX + "aa/" + "a" * 64 + ".eml",
        WEB_MEMBER_PREFIX + "bb/" + "b" * 64 + ".txt",
    ],
)
def test_an_allowed_member_is_accepted(name: str) -> None:
    assert is_safe_member_name(name)
    assert is_allowed_member(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "/etc/passwd",
        "../escape",
        "mail/../../escape",
        "mail\\raw\\x",
        "C:\\escape",
        "mail//raw/x",
        "mail/./raw/x",
        "mail/raw/x\x00y",
    ],
)
def test_an_unsafe_member_name_is_refused(name: str) -> None:
    assert not is_safe_member_name(name)
    assert not is_allowed_member(name)


@pytest.mark.parametrize(
    "name",
    [
        "config.toml",
        ".env",
        "secrets/token.txt",
        "cache/knowledge/index.db",
        "ehall/nju-profile/cookies",
        "mail/raw/../profile/cookies",
        "web/snapshots/ok.txt.bak",
        "knowledge/index.sqlite3",
    ],
)
def test_a_member_outside_the_format_is_refused(name: str) -> None:
    """Nothing that looks like an index, a profile, a config file or a secret has a place here."""
    assert not is_allowed_member(name)


def test_an_object_must_be_a_listed_member_with_a_hash_and_a_size() -> None:
    with pytest.raises(InvalidBackupArchive):
        BackupObject(storage_key="config.toml", sha256=DIGEST, size_bytes=1)
    with pytest.raises(InvalidBackupArchive):
        BackupObject(storage_key=DATABASE_MEMBER, sha256=DIGEST, size_bytes=1)
    with pytest.raises(InvalidBackupArchive):
        BackupObject(storage_key=MAIL_MEMBER_PREFIX + "aa/x.eml", sha256="short", size_bytes=1)
    with pytest.raises(InvalidBackupArchive):
        BackupObject(
            storage_key=MAIL_MEMBER_PREFIX + "aa/x.eml",
            sha256=DIGEST,
            size_bytes=MAX_OBJECT_BYTES + 1,
        )


# ---------------------------------------------------------------------- manifest


def test_the_manifest_has_a_fixed_key_set() -> None:
    assert set(_manifest().to_document()) == {
        "format_version",
        "application_version",
        "created_at",
        "database_member",
        "database_sha256",
        "migration_files",
        "mail_objects",
        "web_snapshots",
        "counts",
    }
    assert DATABASE_MEMBER == "runtime.sqlite3"
    assert MANIFEST_MEMBER == "manifest.json"
    assert FORMAT_VERSION == 1


def test_the_manifest_round_trips_canonically() -> None:
    manifest = _manifest()

    raw = manifest.to_json()
    parsed = BackupManifest.from_json(raw)

    assert parsed == manifest
    assert parsed.to_json() == raw  # canonical: writing it again changes nothing


def test_the_manifest_carries_no_content() -> None:
    document = _manifest().to_document()
    blob = canonical_json(document)

    assert "SENTINEL" not in blob
    for forbidden in ("password", "token", "body", "payload", "/home/", "api_key"):
        assert forbidden not in blob.lower()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {**document, "extra": 1},
        lambda document: {key: value for key, value in document.items() if key != "counts"},
        lambda document: {**document, "format_version": 99},
        lambda document: {**document, "database_member": "somewhere-else.sqlite3"},
        lambda document: {**document, "created_at": "not-a-date"},
        lambda document: {**document, "mail_objects": [{"storage_key": "mail/raw/x"}]},
        lambda document: {
            **document,
            "web_snapshots": [
                {"storage_key": "mail/raw/aa/x.eml", "sha256": DIGEST, "size_bytes": 1}
            ],
        },
    ],
)
def test_a_malformed_manifest_is_refused(mutate: object) -> None:
    document = _manifest().to_document()
    broken = mutate(document)  # type: ignore[operator]

    with pytest.raises(InvalidBackupArchive):
        BackupManifest.from_json(canonical_json(broken))


def test_a_non_json_manifest_is_refused() -> None:
    with pytest.raises(InvalidBackupArchive):
        BackupManifest.from_json("not json at all")
    with pytest.raises(InvalidBackupArchive):
        BackupManifest.from_json("[]")


def test_a_manifest_needs_migrations_and_an_aware_timestamp() -> None:
    with pytest.raises(InvalidBackupArchive):
        _manifest(migration_files=())
    with pytest.raises(InvalidBackupArchive):
        _manifest(created_at=datetime(2026, 9, 25))
    with pytest.raises(InvalidBackupArchive):
        _manifest(migration_files=("../escape.sql",))
    with pytest.raises(InvalidBackupArchive):
        _manifest(database_sha256="SHORT")


def test_a_manifest_refuses_one_object_twice() -> None:
    shared = _object()

    with pytest.raises(InvalidBackupArchive):
        _manifest(mail_objects=(shared, shared))


def test_counts_are_fixed_and_non_negative() -> None:
    counts = BackupCounts(tasks=3, mail_messages=2)

    assert counts.to_document()["tasks"] == 3
    assert BackupCounts.from_document(counts.to_document()) == counts
    with pytest.raises(InvalidBackupArchive):
        BackupCounts.from_document({**counts.to_document(), "ghosts": 1})
    with pytest.raises(InvalidBackupArchive):
        BackupCounts.from_document({**counts.to_document(), "tasks": -1})


def test_a_manifest_recorded_for_another_format_version_is_refused() -> None:
    document = _manifest().to_document()
    document["format_version"] = 0

    with pytest.raises(InvalidBackupArchive):
        BackupManifest.from_json(canonical_json(document))


def test_the_clock_is_not_read_by_the_domain() -> None:
    """A manifest's timestamp comes from the caller's clock, and this module proves it."""
    manifest = _manifest(created_at=NOW + timedelta(days=1))

    assert manifest.created_at == NOW + timedelta(days=1)
    assert manifest.created_at.tzinfo is UTC
