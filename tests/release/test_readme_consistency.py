"""The public README stays true to the release it describes (26, 30, 36-38).

A README is a promise. This file keeps three kinds of promise checkable without a Markdown parser:
every `pw …` command it shows must exist with the options it shows, it must not describe a v1
capability as missing, and neither it nor the other public documents may carry a personal path or a
credential-shaped value.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import typer

from assistant.cli import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
PUBLIC_DOCUMENTS = (
    README,
    REPOSITORY_ROOT / "CONTRIBUTING.md",
    REPOSITORY_ROOT / "SECURITY.md",
)

_INVOCATION = re.compile(r"^(?:uv run )?pw\b(?P<rest>.*)$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _joined_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line number, logical line), joining shell continuations so options are not lost."""
    buffer = ""
    start = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        buffer = f"{buffer} {stripped}" if buffer else stripped
        if not buffer:
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


def _documented_invocations() -> Iterator[tuple[int, list[str]]]:
    for number, line in _joined_lines(_read(README)):
        match = _INVOCATION.match(line)
        if match is None:
            continue
        try:
            tokens = shlex.split(match.group("rest"))
        except ValueError:  # an unbalanced quote in prose is not a command example
            continue
        yield number, tokens


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


def test_the_readme_names_the_release_it_documents() -> None:
    text = _read(README)

    assert "1.0.0" in text
    assert "Current release: v1.0.0" in text
    for section in ("Product model", "Core capabilities", "Safety model", "Known limitations"):
        assert section in text, section


def test_every_documented_command_exists_with_the_options_it_shows() -> None:
    """A README example that no longer runs is worse than no example."""
    checked = 0
    for number, tokens in _documented_invocations():
        if not tokens:
            continue
        command, remaining = _resolve(tokens)
        documented = [token.split("=", 1)[0] for token in remaining if token.startswith("--")]
        unknown = sorted(set(documented) - _long_options(command))
        assert not unknown, f"README.md:{number} documents unknown option(s) {unknown}"
        checked += 1
    assert checked >= 50, f"expected the README to show real commands, found {checked}"


def test_the_readme_does_not_claim_v1_capabilities_are_missing() -> None:
    """The v0.x README said these; a v1 README that repeats them would mislead a reader."""
    text = _read(README).lower()
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
    for phrase in stale:
        assert phrase not in text, phrase

    # And the capabilities a reader is promised are actually described.
    for capability in ("mail.send", "ehall.submit-certificate", "non-executing", "trusted-lan"):
        assert capability in text, capability


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
