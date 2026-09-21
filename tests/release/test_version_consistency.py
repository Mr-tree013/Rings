"""One version, everywhere it is written down (ADR-0032 §26/§71/§72).

A release has several places that name its version: the package metadata, the module constant, the
changelog, the release notes and the three console entry points. Any two of them can drift silently
— a wheel that says 1.0.0 while `pw --version` says 0.9.0 is a bug report waiting to happen — so
this file pins them together at the version being released.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from assistant import __version__

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

"""The version this release publishes. A release candidate that is not 1.1.0 fails here."""

VERSION = "1.1.0"
"""The version this release publishes. A release candidate that is not 1.1.0 fails here."""


def test_every_version_source_agrees() -> None:
    """§72: pyproject, the package, the changelog and the release notes are one version."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = REPOSITORY_ROOT / "docs" / "releases" / f"{VERSION}.md"

    assert pyproject["project"]["version"] == VERSION
    assert __version__ == VERSION
    assert f"## [{VERSION}]" in changelog
    assert notes.is_file(), notes
    first_line = notes.read_text(encoding="utf-8").splitlines()[0]
    assert VERSION in first_line


def test_the_release_notes_state_what_v1_does_not_do() -> None:
    """§28: the notes name the limitations instead of implying capabilities that do not exist."""
    text = (REPOSITORY_ROOT / "docs" / "releases" / f"{VERSION}.md").read_text(encoding="utf-8")

    for limitation in ("Known limitations", "exactly-once", "trusted LAN", "non-executing"):
        assert limitation in text, limitation
    for overclaim in ("fully autonomous", "automatic form filling", "cloud sync"):
        assert overclaim not in text.lower(), overclaim
