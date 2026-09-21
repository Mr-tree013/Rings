"""The README is a landing page, not a manual (post-v1 documentation reorganization).

Detail belongs in `docs/`: the README answers what Rings is, what it can do, why it is safe to let
it near anything, how to run it, and where the rest is written down. The page ships in two language
variants — Chinese in `README.md`, the original English in `README_en.md` — and both are held to
the same shape: bounded length, a fixed set of sections, working links, and none of the
implementation vocabulary that belongs in the guides, the specification or the ADRs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
README_EN = REPOSITORY_ROOT / "README_en.md"
GUIDES = tuple(sorted((REPOSITORY_ROOT / "docs" / "guides").glob("*.md")))

LANDING_PAGES = (README, README_EN)
"""The Chinese page is the landing page; the English file is the same page, kept as an option."""

MAX_LINES = 350
"""Hard ceiling for the landing page. The preferred range is roughly 200-300 lines."""

REQUIRED_SECTIONS = {
    README: (
        "## Tree Model",
        "## Rings 能做什么",
        "## 安全设计",
        "## 快速开始",
        "## 常用示例",
        "## 文档",
        "## 架构概览",
        "## 已知限制",
        "## 开发",
        "## License",
    ),
    README_EN: (
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
    ),
}
"""The sections each language variant promises its reader."""

INVITATION = {
    README: ("# Rings", "与你一同成长", "## Tree Model", "## Rings 能做什么", "## 安全设计",
             "## 快速开始"),
    README_EN: ("# Rings", "grows with you", "## The Tree Model", "## What Rings Can Do",
                "## Safety by Design", "## Quick Start"),
}
"""What a first-time visitor must see, per language variant."""

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


def _read(page: Path) -> str:
    return page.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_the_readme_stays_a_landing_page(page: Path) -> None:
    lines = _read(page).splitlines()

    assert len(lines) <= MAX_LINES, f"{page.name} is {len(lines)} lines; the ceiling is {MAX_LINES}"
    # Not a wall of text either: the page keeps structure its reader can scan.
    assert sum(1 for line in lines if line.startswith("## ")) >= 8


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_the_readme_keeps_its_sections(page: Path) -> None:
    text = _read(page)

    for section in REQUIRED_SECTIONS[page]:
        assert section in text, f"{page.name}: {section}"


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_the_readme_links_the_documentation_hub(page: Path) -> None:
    text = _read(page)

    for target in (
        "docs/guides/getting-started.md",
        "docs/concepts/rings-language.md",
        "docs/specs/0001-system-design.md",
        "docs/adr/",
        "docs/releases/1.2.0.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert target in text, f"{page.name}: {target}"


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_every_local_link_in_the_readme_resolves(page: Path) -> None:
    broken: list[str] = []
    for match in _LINK.finditer(_read(page)):
        target = match.group("target")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (REPOSITORY_ROOT / path).exists():
            broken.append(target)

    assert not broken, f"{page.name} links to something that does not exist: {broken}"


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


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_the_landing_page_carries_no_implementation_vocabulary(page: Path) -> None:
    text = _read(page).lower()

    for pattern in IMPLEMENTATION_DETAIL:
        match = pattern.search(text)
        assert match is None, f"{page.name} mentions {match.group(0)!r}: move it to a guide or ADR"


@pytest.mark.parametrize("page", LANDING_PAGES)
def test_the_first_hundred_lines_are_an_invitation(page: Path) -> None:
    head = "\n".join(_read(page).splitlines()[:100])

    for expected in INVITATION[page]:
        assert expected in head, f"{page.name}: {expected}"
    for pattern in IMPLEMENTATION_DETAIL:
        match = pattern.search(head.lower())
        assert match is None, match.group(0)


def test_the_readme_is_chinese_primary() -> None:
    """The landing page reads as Chinese; only proper nouns and identifiers stay English."""
    text = _read(README)
    han = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")

    assert han >= 300, f"expected a Chinese-primary README, found {han} Han characters"
    # The English section titles this page used before must not creep back in.
    for stale in ("## Quick Start", "## Known Limitations", "## What Rings Can Do"):
        assert stale not in text, stale


def test_the_english_variant_is_english_primary() -> None:
    """`README_en.md` is the same page in its original language, kept as the other option."""
    text = _read(README_EN)
    han = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")

    # The motto is the one Chinese line both pages carry.
    assert han <= 40, f"expected an English-primary README, found {han} Han characters"


def test_the_language_variants_point_at_each_other() -> None:
    assert "(README_en.md)" in _read(README)
    assert "(README.md)" in _read(README_EN)
