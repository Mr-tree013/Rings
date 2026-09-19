"""Reading and initialising `.pa/vault.toml` (ADR-0011).

Reading uses `tomllib`; writing uses a small fixed-field writer plus
`temp file -> flush -> fsync -> os.replace`, so a crash cannot leave a half-written
manifest behind.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import tomllib
from pathlib import Path

from assistant.domain.errors import (
    InvalidVaultManifest,
    VaultAlreadyInitialized,
    VaultNotInitialized,
)
from assistant.domain.vault import MANIFEST_DIRECTORY, MANIFEST_FILENAME, VaultManifest
from assistant.ports.clock import Clock


class VaultManifestFile:
    """`VaultManifestStore` backed by the filesystem."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def read(self, root: Path) -> VaultManifest:
        return await asyncio.to_thread(self._read_sync, Path(root))

    async def initialize(self, root: Path, *, vault_id: str, label: str) -> VaultManifest:
        return await asyncio.to_thread(self._initialize_sync, Path(root), vault_id, label)

    def _read_sync(self, root: Path) -> VaultManifest:
        manifest_path = manifest_path_for(root)
        if not manifest_path.is_file():
            raise VaultNotInitialized(f"{root} has no {MANIFEST_FILENAME} manifest")
        try:
            with manifest_path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise InvalidVaultManifest(f"{manifest_path} is not valid TOML: {exc}") from exc
        except OSError as exc:
            raise InvalidVaultManifest(f"{manifest_path} could not be read: {exc}") from exc
        return VaultManifest.from_mapping(data)

    def _initialize_sync(self, root: Path, vault_id: str, label: str) -> VaultManifest:
        if not root.is_dir():
            raise VaultNotInitialized(
                f"{root} does not exist or is not a directory; the mount root is never created"
            )
        manifest_path = manifest_path_for(root)
        if manifest_path.exists():
            raise VaultAlreadyInitialized(f"{manifest_path} already exists")
        manifest = VaultManifest(vault_id=vault_id, label=label, created_at=self._clock.now())
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(manifest_path, manifest.to_toml())
        return manifest


def manifest_path_for(root: Path) -> Path:
    """Return the manifest path for a vault rooted at `root`."""
    return Path(root) / MANIFEST_DIRECTORY / MANIFEST_FILENAME


def _write_atomically(target: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".vault-", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


__all__ = ["VaultManifestFile", "manifest_path_for"]
