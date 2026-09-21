"""The public documentation stays true to the release it describes (26, 30, 35-38).

A document is a promise. This file keeps three kinds of promise checkable without a Markdown parser:
every `pw …` command the README and the user guides show must exist with the options they show, the
documents must not describe a v1 capability as missing, and none of the public documents may carry a
personal path or a credential-shaped value.

The README is a landing page, so most of the commands now live in `docs/guides/` — that is why this
checks the guides as well instead of counting commands in the README alone.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer

from assistant.cli import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
README_EN = REPOSITORY_ROOT / "README_en.md"
GUIDES = tuple(sorted((REPOSITORY_ROOT / "docs" / "guides").glob("*.md")))
PUBLIC_DOCUMENTS = (
    README,
    README_EN,
    REPOSITORY_ROOT / "CONTRIBUTING.md",
    REPOSITORY_ROOT / "SECURITY.md",
    *GUIDES,
)
COMMAND_DOCUMENTS = (README, README_EN, *GUIDES)

_INVOCATION = re.compile(r"^(?:uv run )?pw\b(?P<rest>.*)$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _joined_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line number, logical line), joining shell continuations so options are not lost."""
    buffer = ""
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if buffer:
            buffer = f"{buffer} {stripped}"
        else:
            buffer = stripped
            start = number
        if buffer.endswith("\\"):
            buffer = buffer[:-1]
            continue
        if buffer:
            yield start, buffer
        buffer = ""
    if buffer:
        yield start, buffer


def _long_options(command: object) -> set[str]:
    known = {"--help"}
    for parameter in getattr(command, "params", []):
        for option in getattr(parameter, "opts", []) or []:
            if option.startswith("--"):
                known.add(option)
    return known


def _documented_invocations() -> Iterator[tuple[Path, int, list[str]]]:
    for path in COMMAND_DOCUMENTS:
        for number, line in _joined_lines(_read(path)):
            match = _INVOCATION.match(line)
            if match is None:
                continue
            try:
                tokens = shlex.split(match.group("rest"))
            except ValueError:  # an unbalanced quote in prose is not a command example
                continue
            yield path, number, tokens


def _resolve(tokens: list[str]) -> tuple[object, list[str]]:
    """Walk as far down the command tree as the tokens allow, then return the leftover tokens."""
    command: object = typer.main.get_command(app)
    remaining = list(tokens)
    while remaining:
        child = getattr(command, "commands", {}).get(remaining[0])
        if child is None:
            break
        command = child
        remaining.pop(0)
    return command, remaining


def test_the_public_documents_name_the_release_they_describe() -> None:
    for page in (README, README_EN):
        text = _read(page)

        assert "1.2.0" in text, page.name
        assert "docs/releases/1.2.0.md" in text, page.name


def test_every_documented_command_exists_with_the_options_it_shows() -> None:
    """A documentation example that no longer runs is worse than no example."""
    checked = 0
    for path, number, tokens in _documented_invocations():
        if not tokens:
            continue
        command, remaining = _resolve(tokens)
        documented = [token.split("=", 1)[0] for token in remaining if token.startswith("--")]
        unknown = sorted(set(documented) - _long_options(command))
        where = path.relative_to(REPOSITORY_ROOT)
        assert not unknown, f"{where}:{number} documents unknown option(s) {unknown}"
        checked += 1

    assert len(GUIDES) >= 9, f"expected the user guides to exist, found {len(GUIDES)}"
    assert checked >= 100, f"expected real commands across the docs, found {checked}"


def test_the_documents_do_not_claim_v1_capabilities_are_missing() -> None:
    """The v0.x README said these; repeating them would mislead a reader."""
    stale = (
        "no smtp support",
        "no smtp capability",
        "no ehall support",
        "no ehall integration",
        "no web ui",
        "no mcp support",
        "no mobile web",
        "smtp is not implemented",
        "ehall is not implemented",
        "production capability set is empty",
    )
    for path in COMMAND_DOCUMENTS:
        text = _read(path).lower()
        for phrase in stale:
            assert phrase not in text, f"{path.name}: {phrase}"

    # And the capabilities a reader is promised are actually described somewhere public.
    everything = "\n".join(_read(path).lower() for path in PUBLIC_DOCUMENTS)
    for capability in ("mail.send", "ehall.submit-certificate", "non-executing", "trusted-lan"):
        assert capability in everything, capability


_CREDENTIAL_SHAPED = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\b(password|passwd|secret|api_key|access_token)\b\s*=\s*[\"'][^\"']{6,}[\"']"),
)
_PERSONAL_PATH = re.compile(r"/(?:home|Users)/(?!user\b|someone\b|a\b|x\b)[A-Za-z0-9._-]+")


def test_public_documents_carry_no_personal_path_or_credential() -> None:
    """27-28, 38: examples use placeholders, never somebody's real path or a real secret."""
    for path in PUBLIC_DOCUMENTS:
        text = _read(path)
        for pattern in _CREDENTIAL_SHAPED:
            match = pattern.search(text)
            assert match is None, f"{path.name} looks like it carries a secret: {match.group(0)!r}"
        match = _PERSONAL_PATH.search(text)
        assert match is None, f"{path.name} carries a personal path: {match.group(0)!r}"


@pytest.mark.parametrize("page", (README, README_EN))
def test_the_guides_are_linked_from_the_documentation_hub(page: Path) -> None:
    """A guide nobody can find is a guide nobody reads."""
    text = _read(page)

    for guide in GUIDES:
        relative = guide.relative_to(REPOSITORY_ROOT).as_posix()
        assert relative in text, f"{page.name}: {relative}"
