"""Content-addressed raw message storage (ADR-0020)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assistant.adapters.mail.raw_store import RawMailStore
from assistant.domain.errors import MailRawStorageError


async def test_a_message_is_stored_under_its_hash(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")
    raw = b"From: a@b\r\nSubject: stored\r\n\r\nbody"
    digest = hashlib.sha256(raw).hexdigest()

    stored = await store.store(raw)

    assert stored.sha256 == digest
    assert stored.storage_key == f"mail/raw/{digest[:2]}/{digest}.eml"
    assert stored.size_bytes == len(raw)
    assert (tmp_path / "mail" / stored.storage_key).read_bytes() == raw


async def test_the_storage_key_is_relative_never_an_absolute_path(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")

    stored = await store.store(b"bytes")

    assert not stored.storage_key.startswith("/")
    assert str(tmp_path) not in stored.storage_key


async def test_the_same_bytes_are_stored_once(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")
    raw = b"identical bytes"

    first = await store.store(raw)
    second = await store.store(raw)

    assert first.storage_key == second.storage_key
    files = list((tmp_path / "mail" / "mail" / "raw").rglob("*.eml"))
    assert len(files) == 1


async def test_different_bytes_get_different_keys(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")

    first = await store.store(b"one")
    second = await store.store(b"two")

    assert first.storage_key != second.storage_key


async def test_storing_creates_no_temporary_files(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")
    await store.store(b"bytes")

    assert list((tmp_path / "mail").rglob("*.tmp")) == []


async def test_a_message_can_be_read_back_and_verified(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")
    raw = b"From: a@b\r\n\r\nbody"
    stored = await store.store(raw)

    assert await store.read(stored.storage_key) == raw


async def test_reading_a_missing_object_is_reported(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")

    with pytest.raises(MailRawStorageError):
        await store.read("mail/raw/aa/" + "a" * 64 + ".eml")


async def test_an_object_that_disagrees_with_its_address_is_refused(tmp_path: Path) -> None:
    """A corrupted or colliding object is an error, never a silent replacement."""
    store = RawMailStore(tmp_path / "mail")
    raw = b"the real bytes"
    stored = await store.store(raw)
    path = tmp_path / "mail" / stored.storage_key
    path.write_bytes(b"tampered")

    with pytest.raises(MailRawStorageError):
        await store.read(stored.storage_key)


async def test_writing_never_rewrites_an_existing_object(tmp_path: Path) -> None:
    store = RawMailStore(tmp_path / "mail")
    raw = b"content"
    stored = await store.store(raw)
    path = tmp_path / "mail" / stored.storage_key
    before = path.stat().st_mtime_ns

    await store.store(raw)

    assert path.stat().st_mtime_ns == before


async def test_the_store_lives_under_the_configured_root_only(tmp_path: Path) -> None:
    """No repository writes, no cache writes: everything stays under the runtime directory."""
    root = tmp_path / "runtime" / "mail"
    store = RawMailStore(root)

    await store.store(b"payload")

    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written, "the store wrote nothing"
    assert all(root in path.parents for path in written)
