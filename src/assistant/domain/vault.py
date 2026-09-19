"""The archive vault manifest: how a vault names itself, wherever it is mounted (ADR-0011).

`.pa/vault.toml` lives at the root of an archive vault and contains only identity and
description:

```toml
format_version = 1
vault_id = "archive-main"
label = "Personal Archive"
created_at = "2026-09-20T00:00:00.000000+00:00"
```

It never contains a mount path, a credential or a secret: the vault is the stable thing,
the drive letter is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from assistant.domain.errors import InvalidStorageRoot, InvalidVaultManifest
from assistant.domain.storage import validate_root_id

VAULT_FORMAT_VERSION = 1
MANIFEST_DIRECTORY = ".pa"
MANIFEST_FILENAME = "vault.toml"
MANIFEST_RELATIVE_PATH = f"{MANIFEST_DIRECTORY}/{MANIFEST_FILENAME}"

_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"'}


@dataclass(frozen=True, slots=True)
class VaultManifest:
    """The identity block stored at the root of an archive vault."""

    vault_id: str
    label: str
    created_at: datetime
    format_version: int = VAULT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != VAULT_FORMAT_VERSION:
            raise InvalidVaultManifest(
                f"unsupported vault format_version {self.format_version}; "
                f"this build understands {VAULT_FORMAT_VERSION}"
            )
        try:
            validate_root_id(self.vault_id)
        except InvalidStorageRoot as exc:
            raise InvalidVaultManifest(f"invalid vault_id: {exc}") from exc
        if not self.label.strip():
            raise InvalidVaultManifest("vault label must not be blank")
        if any(character < " " for character in self.label):
            raise InvalidVaultManifest("vault label must not contain control characters")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidVaultManifest("created_at must be timezone-aware")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> VaultManifest:
        """Build a manifest from parsed TOML data, rejecting anything unexpected."""
        unknown = sorted(set(data) - {"format_version", "vault_id", "label", "created_at"})
        if unknown:
            raise InvalidVaultManifest(f"unexpected manifest keys: {', '.join(unknown)}")
        format_version = data.get("format_version")
        if not isinstance(format_version, int) or isinstance(format_version, bool):
            raise InvalidVaultManifest("format_version must be an integer")
        vault_id = data.get("vault_id")
        if not isinstance(vault_id, str):
            raise InvalidVaultManifest("vault_id must be a string")
        label = data.get("label")
        if not isinstance(label, str):
            raise InvalidVaultManifest("label must be a string")
        created_at = data.get("created_at")
        if not isinstance(created_at, str):
            raise InvalidVaultManifest("created_at must be an ISO 8601 string")
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise InvalidVaultManifest(f"created_at is not ISO 8601: {created_at!r}") from exc
        if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
            raise InvalidVaultManifest("created_at must be timezone-aware")
        return cls(
            vault_id=vault_id,
            label=label,
            created_at=parsed_created_at,
            format_version=format_version,
        )

    def to_toml(self) -> str:
        """Render the manifest as TOML.

        A tiny fixed-field writer on purpose: this is not a general TOML serialiser.
        """
        created_at = self.created_at.astimezone(UTC).isoformat(timespec="microseconds")
        return (
            "# growing-assistant archive vault manifest — see ADR-0011\n"
            f"format_version = {self.format_version}\n"
            f'vault_id = "{_toml_string(self.vault_id)}"\n'
            f'label = "{_toml_string(self.label)}"\n'
            f'created_at = "{created_at}"\n'
        )


def _toml_string(value: str) -> str:
    escaped = value
    for character, replacement in _TOML_ESCAPES.items():
        escaped = escaped.replace(character, replacement)
    return escaped


__all__ = [
    "MANIFEST_DIRECTORY",
    "MANIFEST_FILENAME",
    "MANIFEST_RELATIVE_PATH",
    "VAULT_FORMAT_VERSION",
    "VaultManifest",
]

