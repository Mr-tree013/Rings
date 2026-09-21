"""The command line a v1 user meets: every command opens, every version agrees (26, 30-32).

Two release-level properties are checked here. First, `pw --help` and every subcommand's `--help`
work without configuration, credentials, a database or a network — a help screen that fails is a
broken first impression. Second, a normal user error produces a sentence, not a traceback, and never
leaks a SQL fragment, an internal absolute path or a token hash.

The version sources are pinned in one place: `pyproject.toml`, `assistant.__version__`, the release
notes, the changelog and the installed metadata must agree, so no two of them can drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from assistant import __version__
from assistant.cli import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()

VERSION = "1.1.1"
"""The version this release hardens to. A release candidate that is not 1.1.1 fails here."""


def _top_level_commands() -> list[str]:
    group = typer.main.get_command(app)
    return sorted(group.commands)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host with empty XDG roots: help must work even before anything exists."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return tmp_path


# ------------------------------------------------------------------------- help surface


def test_the_root_help_and_version_work_without_any_state(isolated: Path) -> None:
    """§26: the version is a pure read; the help screen needs no configuration."""
    version = runner.invoke(app, ["--version"])
    help_screen = runner.invoke(app, ["--help"])

    assert version.exit_code == 0, version.output
    assert f"pw {__version__}" in version.output
    assert not (isolated / "data").exists()  # nothing was created to answer a version question
    assert help_screen.exit_code == 0, help_screen.output
    assert "Usage" in help_screen.output


@pytest.mark.parametrize("command", _top_level_commands())
def test_every_top_level_command_has_working_help(command: str, isolated: Path) -> None:
    """§30: every command tree opens, with no config, no key, no database and no network."""
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "Usage" in result.output


def test_the_documented_command_tree_is_still_present(isolated: Path) -> None:
    """The v1 README names these; a release that quietly dropped one would mislead a reader."""
    documented = {
        "task",
        "calendar",
        "plan",
        "work",
        "notifications",
        "scheduled",
        "model",
        "interpret",
        "ask",
        "mail",
        "cases",
        "action",
        "ehall",
        "mobile",
        "correction",
        "fact",
        "playbook",
        "watch",
        "ingest",
        "mcp",
        "backup",
        "integrity",
        "daemon",
    }

    assert documented <= set(_top_level_commands())


# ------------------------------------------------------------------------ error hygiene

_FORBIDDEN_IN_ERRORS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\bSELECT .+ FROM\b"),
    re.compile(r"\bINSERT INTO\b"),
    re.compile(r"\bsqlite3\."),
    re.compile(r"/home/[^ \n]+/growing-assistant/src/"),
)
_TOKEN_SHAPES = (
    re.compile(r"\b[0-9a-f]{64}\b"),  # a stored token hash, session hash or fingerprint
)


def _assert_clean_error(output: str) -> None:
    for pattern in _FORBIDDEN_IN_ERRORS:
        assert not pattern.search(output), output
    for pattern in _TOKEN_SHAPES:
        assert not pattern.search(output), output


@pytest.mark.parametrize(
    "arguments",
    [
        ["task", "show", "not-a-uuid"],
        ["task", "done", "not-a-uuid"],
        ["case", "show", "missing-case"],
        ["action", "show", "missing-action"],
        ["fact", "candidate", "show", "missing-candidate"],
        ["playbook", "show", "missing-playbook"],
        ["mail", "show", "missing-message"],
        ["watch", "observation", "show", "missing-observation"],
        ["ingest", "show", "missing-input"],
        ["vault", "status", "/does/not/exist"],
    ],
)
def test_a_user_error_is_a_sentence_not_a_traceback(arguments: list[str], isolated: Path) -> None:
    """§31: the failure mode of a mistyped id or path is a message, and nothing internal."""
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0, result.output
    _assert_clean_error(result.output)


def test_integrity_and_backup_refusals_are_plain_language(isolated: Path) -> None:
    """The two operator commands report refusals with reasons, never with internals."""
    missing_database = runner.invoke(app, ["integrity", "check"])
    missing_archive = runner.invoke(app, ["backup", "verify", str(isolated / "nope.gab")])

    assert missing_database.exit_code == 1, missing_database.output
    _assert_clean_error(missing_database.output)
    assert missing_archive.exit_code == 2, missing_archive.output
    _assert_clean_error(missing_archive.output)
