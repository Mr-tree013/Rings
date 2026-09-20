"""The README is a landing page, not a manual (post-v1 documentation reorganization).

Detail belongs in `docs/`: the README answers what Rings is, what it can do, why it is safe to let
it near anything, how to run it, and where the rest is written down. This file keeps that shape:
bounded length, a fixed set of sections, working links, and none of the implementation vocabulary
that belongs in the guides, the specification or the ADRs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
GUIDES = tuple(sorted((REPOSITORY_ROOT / "docs" / "guides").glob("*.md")))

MAX_LINES = 350
"""Hard ceiling for the landing page. The preferred range is roughly 200-300 lines."""

REQUIRED_SECTIONS = (
    "## The Tree Model",
    "## What Rings Can Do",
    "## Safety by Design",
    "## Quick Start",
    "## Everyday Examples",
    "## Documentation",
    "## Architecture at a Glance",
    "## Known Limitations",
    "## Development",
    "## License",
)

"""Vocabulary that belongs in a guide, the specification or an ADR - never on the landing page."""
IMPLEMENTATION_DETAIL = tuple(
    re.compile(pattern)
    for pattern in (
        r"\buidvalidity\b",
        r"\bleases?\b",
        r"\bfencing\b",
        r"\beocd\b",
        r"\bwal\b",
        r"\bmigrations?\b",
        r"\basyncio\b",
        r"\bbody\.peek\b",
        r"\bpage[- ]contracts?\b",
        r"\bsqlite schema\b",
        r"\bstate machines?\b.*\binternals?\b",
    )
)

_LINK = re.compile(r"\[[^\]]+\]\((?P<target>[^)\s]+)\)")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_stays_a_landing_page() -> None:
    lines = _readme().splitlines()

    assert len(lines) <= MAX_LINES, f"README is {len(lines)} lines; the ceiling is {MAX_LINES}"
    # Not a wall of text either: the page keeps structure its reader can scan.
    assert sum(1 for line in lines if line.startswith("## ")) >= 8


def test_the_readme_keeps_its_sections() -> None:
    text = _readme()

    for section in REQUIRED_SECTIONS:
        assert section in text, section


def test_the_readme_links_the_documentation_hub() -> None:
    text = _readme()

    for target in (
        "docs/guides/getting-started.md",
        "docs/concepts/rings-language.md",
        "docs/specs/0001-system-design.md",
        "docs/adr/",
        "docs/releases/1.0.0.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert target in text, target


def test_every_local_link_in_the_readme_resolves() -> None:
    broken: list[str] = []
    for match in _LINK.finditer(_readme()):
        target = match.group("target")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (REPOSITORY_ROOT / path).exists():
            broken.append(target)

    assert not broken, f"README links to something that does not exist: {broken}"


def test_every_local_link_in_the_guides_resolves() -> None:
    """A guide links to ADRs, the spec and sibling guides by relative path; those must exist."""
    broken: list[str] = []
    for guide in GUIDES:
        for match in _LINK.finditer(guide.read_text(encoding="utf-8")):
            target = match.group("target")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if path and not (guide.parent / path).exists():
                broken.append(f"{guide.name} -> {target}")

    assert not broken, f"guides link to something that does not exist: {broken}"


def test_the_landing_page_carries_no_implementation_vocabulary() -> None:
    text = _readme().lower()

    for pattern in IMPLEMENTATION_DETAIL:
        match = pattern.search(text)
        assert match is None, f"README mentions {match.group(0)!r}: move it to a guide or ADR"


def test_the_first_hundred_lines_are_an_invitation() -> None:
    """A first-time visitor sees the name, the model, the capabilities and how to start."""
    head = "\n".join(_readme().splitlines()[:100])

    for expected in ("# Rings", "grows with you", "## The Tree Model", "## What Rings Can Do",
                     "## Safety by Design", "## Quick Start"):
        assert expected in head, expected
    for pattern in IMPLEMENTATION_DETAIL:
        match = pattern.search(head.lower())
        assert match is None, match.group(0)
