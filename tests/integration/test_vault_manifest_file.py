"""Integration tests for `.pa/vault.toml` on a real filesystem."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.filesystem.vault_manifest import VaultManifestFile, manifest_path_for
from assistant.domain.errors import (
    InvalidVaultManifest,
    VaultAlreadyInitialized,
    VaultNotInitialized,
)
from tests.support.fakes import FakeClock

CREATED_AT = datetime(2026, 9, 20, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=CREATED_AT)


@pytest.fixture
def store(clock: FakeClock) -> VaultManifestFile:
    return VaultManifestFile(clock)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "usb"
    root.mkdir()
    return root


async def test_initialize_creates_a_readable_manifest(
    store: VaultManifestFile, vault: Path
) -> None:
    manifest = await store.initialize(vault, vault_id="archive-main", label="Personal Archive")

    manifest_path = manifest_path_for(vault)
    assert manifest_path.is_file()
    assert manifest.created_at == CREATED_AT
    assert await store.read(vault) == manifest


async def test_initialize_leaves_no_temporary_files_behind(
    store: VaultManifestFile, vault: Path
) -> None:
    await store.initialize(vault, vault_id="archive-main", label="Personal Archive")

    leftovers = [path.name for path in (vault / ".pa").iterdir() if path.name != "vault.toml"]
    assert leftovers == []


async def test_manifest_holds_identity_only(store: VaultManifestFile, vault: Path) -> None:
    await store.initialize(vault, vault_id="archive-main", label="Personal Archive")

    text = manifest_path_for(vault).read_text(encoding="utf-8")

    assert str(vault) not in text
    assert "/mnt" not in text
    assert "path" not in text


async def test_initialize_refuses_to_overwrite_an_existing_manifest(
    store: VaultManifestFile, vault: Path
) -> None:
    await store.initialize(vault, vault_id="archive-main", label="Personal Archive")

    with pytest.raises(VaultAlreadyInitialized):
        await store.initialize(vault, vault_id="archive-other", label="Other")

    assert (await store.read(vault)).vault_id == "archive-main"


async def test_initialize_never_creates_the_mount_root(
    store: VaultManifestFile, tmp_path: Path
) -> None:
    missing = tmp_path / "not-mounted"

    with pytest.raises(VaultNotInitialized, match="never created"):
        await store.initialize(missing, vault_id="archive-main", label="Personal Archive")

    assert not missing.exists()


async def test_initialize_rejects_an_invalid_id_without_writing_anything(
    store: VaultManifestFile, vault: Path
) -> None:
    with pytest.raises(InvalidVaultManifest):
        await store.initialize(vault, vault_id="Bad Id", label="Personal Archive")

    assert not manifest_path_for(vault).exists()


async def test_reading_an_uninitialised_directory_is_explicit(
    store: VaultManifestFile, tmp_path: Path
) -> None:
    with pytest.raises(VaultNotInitialized):
        await store.read(tmp_path / "absent")


async def test_malformed_toml_is_reported_as_a_manifest_problem(
    store: VaultManifestFile, vault: Path
) -> None:
    manifest_path = manifest_path_for(vault)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("this is not = = toml", encoding="utf-8")

    with pytest.raises(InvalidVaultManifest, match="not valid TOML"):
        await store.read(vault)


@pytest.mark.parametrize(
    "body",
    [
        'format_version = 2\nvault_id = "archive-main"\nlabel = "x"\n'
        'created_at = "2026-09-20T08:00:00+00:00"\n',
        'format_version = 1\nvault_id = "../usb"\nlabel = "x"\n'
        'created_at = "2026-09-20T08:00:00+00:00"\n',
        'format_version = 1\nvault_id = "archive-main"\nlabel = "  "\n'
        'created_at = "2026-09-20T08:00:00+00:00"\n',
        'format_version = 1\nvault_id = "archive-main"\nlabel = "x"\n'
        'created_at = "2026-09-20T08:00:00"\n',
        'format_version = 1\nvault_id = "archive-main"\nlabel = "x"\n',
    ],
)
async def test_invalid_manifests_are_rejected(
    store: VaultManifestFile, vault: Path, body: str
) -> None:
    manifest_path = manifest_path_for(vault)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(body, encoding="utf-8")

    with pytest.raises(InvalidVaultManifest):
        await store.read(vault)

