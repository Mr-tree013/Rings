"""Unit tests for stable storage identity: root ids, logical URIs and their safety rules."""

from __future__ import annotations

from dataclasses import fields

import pytest

from assistant.domain.errors import InvalidStorageRoot, InvalidStorageUri
from assistant.domain.storage import (
    StorageKind,
    StorageRoot,
    StorageUri,
    validate_relative_path,
    validate_root_id,
)


@pytest.mark.parametrize(
    "root_id",
    ["archive-main", "university", "projects", "photos-2024", "a", "a" * 63],
)
def test_accepts_valid_root_ids(root_id: str) -> None:
    assert validate_root_id(root_id) == root_id


@pytest.mark.parametrize(
    "root_id",
    ["../usb", "A B", "/mnt/e", "", "Archive", "-leading", "9lives", "a" * 64, "a_b", "a.b"],
)
def test_rejects_invalid_root_ids(root_id: str) -> None:
    with pytest.raises(InvalidStorageRoot):
        validate_root_id(root_id)


def test_storage_root_keeps_identity_and_rejects_a_blank_label() -> None:
    root = StorageRoot(root_id="archive-main", kind=StorageKind.VAULT, label="Personal Archive")

    assert root.scheme == "vault"
    with pytest.raises(InvalidStorageRoot):
        StorageRoot(root_id="archive-main", kind=StorageKind.VAULT, label="   ")


def test_storage_root_has_no_physical_path_field() -> None:
    root = StorageRoot(root_id="university", kind=StorageKind.LOCAL, label="University")

    field_names = {field.name for field in fields(root)}

    assert field_names == {"root_id", "kind", "label"}
    assert not hasattr(root, "mount_path")


@pytest.mark.parametrize(
    "uri",
    [
        StorageUri(
            kind=StorageKind.VAULT,
            root_id="archive-main",
            relative_path="Courses/OS/report.pdf",
        ),
        StorageUri(
            kind=StorageKind.LOCAL, root_id="university", relative_path="SE/Lab 1/report.pdf"
        ),
        StorageUri(
            kind=StorageKind.LOCAL,
            root_id="university",
            relative_path="课程/操作系统/实验二/报告.md",
        ),
        StorageUri(
            kind=StorageKind.LOCAL, root_id="university", relative_path="notes/100% #1.txt"
        ),
        StorageUri(
            kind=StorageKind.LOCAL, root_id="university", relative_path="emoji/📄-note.txt"
        ),
    ],
)
def test_uri_round_trips_through_its_string_form(uri: StorageUri) -> None:
    rendered = str(uri)

    assert StorageUri.parse(rendered) == uri
    assert rendered.startswith(f"{uri.kind.value}://{uri.root_id}/")


def test_uri_escapes_reserved_characters() -> None:
    uri = StorageUri(
        kind=StorageKind.LOCAL, root_id="university", relative_path="a b/100% #1.txt"
    )

    assert str(uri) == "local://university/a%20b/100%25%20%231.txt"
    assert StorageUri.parse(str(uri)) == uri


def test_uri_exposes_name_and_suffix() -> None:
    uri = StorageUri(
        kind=StorageKind.VAULT, root_id="archive-main", relative_path="Courses/OS/report.pdf"
    )

    assert uri.name == "report.pdf"
    assert uri.suffix == ".pdf"


@pytest.mark.parametrize(
    "value",
    [
        "vault://archive-main/../secret",
        "local://university/a/../../secret",
        "local://university/%2E%2E/secret",
        "local://university/a/%2e%2e/b",
        "local://university/./a.txt",
        "local://university//etc/passwd",
        "local://university/a//b.txt",
        "local://university/..\\secret",
        "local://university/C:/secret",
        "local://university/",
        "local://university",
        "local:///etc/passwd",
        "https://example.com/report.pdf",
        "file:///mnt/e/report.pdf",
        "local://university/a.txt?x=1",
        "local://university/a.txt#fragment",
    ],
)
def test_uri_parse_rejects_traversal_and_absolute_paths(value: str) -> None:
    with pytest.raises(InvalidStorageUri):
        StorageUri.parse(value)


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "   ",
        "/absolute",
        "../escape",
        "a/../b",
        "a/./b",
        "a//b",
        "C:/windows",
        "back\\slash",
    ],
)
def test_relative_paths_are_validated_the_same_way_everywhere(relative_path: str) -> None:
    with pytest.raises(InvalidStorageUri):
        validate_relative_path(relative_path)


def test_uri_construction_validates_its_parts() -> None:
    with pytest.raises(InvalidStorageRoot):
        StorageUri(kind=StorageKind.LOCAL, root_id="Not Valid", relative_path="a.txt")
    with pytest.raises(InvalidStorageUri):
        StorageUri(kind=StorageKind.LOCAL, root_id="university", relative_path="../a.txt")


def test_encoded_separator_is_decoded_once_and_then_validated() -> None:
    # A single encoding of a separator is a legitimate path segment boundary.
    assert StorageUri.parse("local://university/notes%2F2026.txt").relative_path == (
        "notes/2026.txt"
    )
    # Double encoding stays a literal, so it cannot smuggle `..` through validation.
    assert StorageUri.parse("local://university/%252E%252E.txt").relative_path == "%2E%2E.txt"
