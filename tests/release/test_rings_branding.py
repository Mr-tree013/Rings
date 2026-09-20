"""The public product language stays Rings — and stays language, not a new domain model.

Rings, Tree, Roots, Seeds, Branches, Leaves and Rings are how the product is described; they are
not entities. This file keeps the naming honest: the README is titled Rings, the vocabulary is
documented once with an explicit mapping onto the frozen domain terms, the compatibility identifiers
are untouched, and the safety terminology is not renamed to fit the metaphor.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
LANGUAGE = REPOSITORY_ROOT / "docs" / "concepts" / "rings-language.md"
SPEC = REPOSITORY_ROOT / "docs" / "specs" / "0001-system-design.md"

VOCABULARY = ("Roots", "Seeds", "Branches", "Leaves", "Rings", "Tree")

"""The frozen domain names the metaphor must not displace."""
FROZEN_DOMAIN_NAMES = (
    "Task",
    "Deadline",
    "CalendarEvent",
    "PlanBlock",
    "WorkSession",
    "Case",
    "ScheduledJob",
    "InboundEvent",
    "ActionRequest",
    "Approval",
    "ExecutionRun",
    "FactCandidate",
    "ConfirmedFact",
    "PlaybookCandidate",
    "Playbook",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_readme_is_titled_rings() -> None:
    lines = _read(README).splitlines()
    headings = [line for line in lines if line.startswith("# ")]

    assert headings, "the README has no top-level heading"
    assert headings[0] == "# Rings", headings[0]


def test_the_readme_carries_the_product_language() -> None:
    text = _read(README)

    for word in VOCABULARY:
        assert word in text, word
    assert "## The Tree Model" in text
    assert "docs/concepts/rings-language.md" in text
    # The tagline, and the one line of Chinese the README is allowed to carry.
    assert "grows with you" in text
    assert "让每天的叶子，长成年轮。" in text  # noqa: RUF001 - the motto is intentional


def test_the_readme_clones_the_public_repository() -> None:
    text = _read(README)

    assert "git clone https://github.com/Mr-tree013/Rings.git" in text
    assert "cd Rings" in text
    # The old repository name must not survive as a clone target.
    assert "github.com/Mr-tree013/growing-assistant" not in text


def test_branches_are_capability_modules_not_autonomous_agents() -> None:
    readme = _read(README)
    language = _read(LANGUAGE)

    assert "not autonomous sub-agents" in readme
    assert "Branches are capability modules, not autonomous sub-agents." in language
    for text in (readme, language):
        for overclaim in ("autonomous branch", "branches act on their own", "self-directed branch"):
            assert overclaim not in text.lower(), overclaim


def test_the_tree_is_documented_as_bounded() -> None:
    language = _read(LANGUAGE)

    assert "Tree is not an unrestricted autonomous agent." in language
    for boundary in ("ActionRequest", "human Approval", "ExecutionRun"):
        assert boundary in language, boundary


def test_the_language_document_exists_and_maps_onto_the_frozen_terminology() -> None:
    assert LANGUAGE.is_file(), LANGUAGE
    language = _read(LANGUAGE)

    for word in VOCABULARY:
        assert f"### {word}" in language, word
    for name in FROZEN_DOMAIN_NAMES:
        assert f"`{name}`" in language, name
    # The document says out loud that the metaphor changes no entity.
    assert "There is no Seed domain entity in v1." in language
    assert "Do not add a `Seed` class" in language
    assert "Do not add a `Ring` entity" in language
    assert "Branches are capability modules, not autonomous sub-agents." in language


def test_the_spec_calls_the_vocabulary_product_language() -> None:
    spec = _read(SPEC)

    assert "Product Language" in spec
    assert "public conceptual vocabulary" in spec
    assert "replacement domain model" in spec
    assert "docs/concepts/rings-language.md" in spec
    for word in VOCABULARY:
        assert f"`{word}`" in spec, word


def test_the_safety_terminology_is_not_renamed() -> None:
    readme = _read(README)
    for name in ("ActionRequest", "Approval", "ExecutionRun", "FactCandidate", "ConfirmedFact"):
        assert f"`{name}`" in readme, name


def test_the_compatibility_identifiers_are_kept() -> None:
    readme = _read(README)

    # The public name is Rings; the v1 compatibility surface keeps its historical identifiers.
    for identifier in (
        "assistant",
        "growing-assistant",
        "pw",
        "assistantd",
        "growing-assistant-mcp",
        "GROWING_ASSISTANT_*",
    ):
        assert identifier in readme, identifier
