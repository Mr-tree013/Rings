"""VaultManifestStore port: read and create `.pa/vault.toml` (ADR-0011).

Initialisation is explicit and never overwrites: seeing a directory that looks like a
drive must not silently turn it into a vault.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from assistant.domain.vault import VaultManifest


class VaultManifestStore(Protocol):
    """Reads and creates archive vault manifests."""

    async def read(self, root: Path) -> VaultManifest:
        """Return the manifest of the vault rooted at `root`.

        Raises:
            VaultNotInitialized: there is no `.pa/vault.toml`.
            InvalidVaultManifest: the manifest exists but cannot be trusted.
        """
        ...

    async def initialize(self, root: Path, *, vault_id: str, label: str) -> VaultManifest:
        """Create `.pa/vault.toml` under `root` and return it.

        Raises:
            VaultNotInitialized: `root` does not exist or is not a directory (the mount
                root itself is never created).
            VaultAlreadyInitialized: a manifest already exists.
            InvalidVaultManifest: `vault_id` or `label` is not acceptable.
        """
        ...


__all__ = ["VaultManifestStore"]

