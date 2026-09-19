"""Unit tests for the archive vault manifest value object."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.domain.errors import InvalidVaultManifest
from assistant.domain.vault import (
    MANIFEST_RELATIVE_PATH,
    VAULT_FORMAT_VERSION,
    VaultManifest,
)

CREATED_AT = datetime(2026, 9, 20, 8, 30, 15, 123456, tzinfo=UTC)


def _manifest(**overrides: object) -> VaultManifest:
    values: dict[str, object] = {
        "vault_id": "archive-main",
        "label": "Personal Archive",
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return VaultManifest(**values)  # type: ignore[arg-type]


def test_manifest_path_is_the_documented_location() -> None:
    assert MANIFEST_RELATIVE_PATH == ".pa/vault.toml"


def test_manifest_defaults_to_format_version_one() -> None:
    assert _manifest().format_version == VAULT_FORMAT_VERSION == 1


def test_toml_round_trip() -> None:
    manifest = _manifest(label='Archive "main" \\ backup')

    restored = VaultManifest.from_mapping(_parse(manifest.to_toml()))

    assert restored == manifest


def test_toml_round_trip_preserves_unicode_labels() -> None:
    manifest = _manifest(label="个人归档 📦")

    assert VaultManifest.from_mapping(_parse(manifest.to_toml())) == manifest


def test_manifest_never_contains_a_physical_path_or_secret() -> None:
    rendered = _manifest().to_toml()

    assert "/mnt/" not in rendered
    assert "path" not in rendered
    assert "password" not in rendered
    assert "token" not in rendered


def test_rejects_an_unsupported_format_version() -> None:
    with pytest.raises(InvalidVaultManifest, match="format_version"):
        _manifest(format_version=2)


@pytest.mark.parametrize("vault_id", ["../usb", "A B", "/mnt/e", "Archive", ""])
def test_rejects_an_invalid_vault_id(vault_id: str) -> None:
    with pytest.raises(InvalidVaultManifest, match="vault_id"):
        _manifest(vault_id=vault_id)


def test_rejects_a_blank_label() -> None:
    with pytest.raises(InvalidVaultManifest, match="label"):
        _manifest(label="  ")


def test_rejects_a_label_with_control_characters() -> None:
    with pytest.raises(InvalidVaultManifest, match="control"):
        _manifest(label="Archive\u0007")


def test_rejects_a_naive_created_at() -> None:
    with pytest.raises(InvalidVaultManifest, match="timezone-aware"):
        _manifest(created_at=datetime(2026, 9, 20, 8, 30))


def test_from_mapping_rejects_missing_and_unknown_keys() -> None:
    with pytest.raises(InvalidVaultManifest, match="format_version"):
        VaultManifest.from_mapping({"vault_id": "archive-main", "label": "x", "created_at": "x"})
    with pytest.raises(InvalidVaultManifest, match="unexpected"):
        VaultManifest.from_mapping(
            {
                "format_version": 1,
                "vault_id": "archive-main",
                "label": "x",
                "created_at": CREATED_AT.isoformat(),
                "mount_path": "/mnt/e",
            }
        )


@pytest.mark.parametrize(
    "created_at",
    ["not-a-date", "2026-09-20T08:30:00", "2026-09-20"],
)
def test_from_mapping_rejects_bad_timestamps(created_at: str) -> None:
    with pytest.raises(InvalidVaultManifest):
        VaultManifest.from_mapping(
            {
                "format_version": 1,
                "vault_id": "archive-main",
                "label": "x",
                "created_at": created_at,
            }
        )


def test_from_mapping_rejects_wrong_types() -> None:
    with pytest.raises(InvalidVaultManifest, match="label"):
        VaultManifest.from_mapping(
            {
                "format_version": 1,
                "vault_id": "archive-main",
                "label": 7,
                "created_at": CREATED_AT.isoformat(),
            }
        )
    with pytest.raises(InvalidVaultManifest, match="format_version"):
        VaultManifest.from_mapping(
            {
                "format_version": "1",
                "vault_id": "archive-main",
                "label": "x",
                "created_at": CREATED_AT.isoformat(),
            }
        )


def _parse(text: str) -> dict[str, object]:
    import tomllib

    return tomllib.loads(text)

