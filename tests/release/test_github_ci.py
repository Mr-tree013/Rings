"""The GitHub Actions quality gate stays conservative (no YAML parser, on purpose).

The workflow is a released artifact with repository permissions and third-party code in it, so it is
read as text and checked the way a reviewer would: triggers are limited to pull requests, `main` and
manual dispatch; permissions are read-only; every action is pinned to a full commit SHA; the steps
are the same gates a contributor runs locally in the same order; and nothing reaches for a secret,
a shell bootstrap or sudo.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
README = REPOSITORY_ROOT / "README.md"

"""The gates, in the order the workflow must run them."""
GATE_ORDER = (
    "uv python install 3.13",
    "uv sync --frozen",
    "uv lock --check",
    "uv run ruff check .",
    "uv run mypy src",
    "uv run pytest",
    "git diff --check",
    "uv build",
    'test -z "$(git status --porcelain --untracked-files=no)"',
)

FORBIDDEN_IN_WORKFLOW = (
    "pull_request_target",
    "workflow_run",
    "schedule:",
    "secrets.",
    "curl ",
    "wget ",
    "sudo ",
    "write-all",
    "id-token",
)

_USES = re.compile(r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)(?:\s+(?P<comment>#.*))?$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_COMMENT = re.compile(r"^#\s*v\d+\.\d+\.\d+$")
_CONCURRENCY_GROUP = re.compile(
    r"^concurrency:\s*\n\s*group:\s*ci-\$\{\{ github\.workflow \}\}", re.MULTILINE
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _without_comments() -> str:
    return "\n".join(
        line for line in _workflow_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_the_workflow_exists_and_is_named_ci() -> None:
    assert WORKFLOW.is_file(), WORKFLOW
    assert _workflow_text().splitlines()[0] == "name: CI"


def test_the_triggers_are_pull_request_main_and_manual_only() -> None:
    text = _without_comments()

    assert "pull_request:" in text
    assert "workflow_dispatch:" in text
    assert re.search(r"push:\s*\n\s*branches:\s*\[main\]", text), text
    for trigger in FORBIDDEN_IN_WORKFLOW[:3]:
        assert trigger not in text, trigger


def test_permissions_are_read_only() -> None:
    text = _without_comments()

    assert re.search(r"^permissions:\s*\n\s*contents:\s*read\s*$", text, re.MULTILINE), text
    assert not re.search(r":\s*write", text), text
    assert "permissions: write" not in text


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    pinned: dict[str, str] = {}
    for line in _workflow_text().splitlines():
        match = _USES.match(line)
        if match is None:
            continue
        action, ref, comment = match.group("action"), match.group("ref"), match.group("comment")
        assert _FULL_SHA.match(ref), f"{action} is not pinned to a full SHA: {ref}"
        assert comment is not None, f"{action} has no version comment"
        assert _SEMVER_COMMENT.match(comment.strip()), f"{action} comment is not a tag: {comment}"
        pinned[action] = ref

    assert set(pinned) == {"actions/checkout", "astral-sh/setup-uv"}, pinned
    for action, ref in pinned.items():
        assert ref not in {"main", "master"}, action
    # Floating major tags must not appear anywhere in the file.
    for floating in ("@v7\n", "@v10\n", "@main", "@master"):
        assert floating not in _workflow_text(), floating


def test_the_checkout_step_keeps_no_credentials_and_full_history() -> None:
    text = _without_comments()

    assert "fetch-depth: 0" in text
    assert "persist-credentials: false" in text


def test_uv_is_configured_for_python_3_13_without_caching() -> None:
    text = _without_comments()

    assert 'python-version: "3.13"' in text
    assert "enable-cache: false" in text


def test_the_single_job_is_bounded_and_cancels_superseded_runs() -> None:
    text = _without_comments()

    assert "runs-on: ubuntu-latest" in text
    assert "timeout-minutes: 45" in text
    assert text.count("runs-on:") == 1, "the workflow should keep exactly one job"
    assert _CONCURRENCY_GROUP.search(text), text
    assert "cancel-in-progress: true" in text


def test_the_gates_run_in_order_and_are_the_full_gates() -> None:
    text = _without_comments()

    positions = [text.index(gate) for gate in GATE_ORDER]
    assert positions == sorted(positions), "the quality gates are out of order"
    # The full suite is run: no deselection, no marker narrowing, no early exit.
    for narrowing in ("-k ", "-m ", "--ignore=", "-x", "pytest tests/unit"):
        assert narrowing not in text, narrowing


def test_the_workflow_reaches_for_no_secret_and_no_bootstrap() -> None:
    text = _without_comments()

    for forbidden in FORBIDDEN_IN_WORKFLOW:
        assert forbidden not in text, forbidden


def test_the_workflow_pins_no_timezone() -> None:
    """CI must stay timezone-neutral: the runner's UTC default is what finds host-timezone bugs."""
    text = _without_comments()

    for pinned in ("TZ:", "TZ="):
        assert pinned not in text, pinned
    assert "Asia/Shanghai" not in text


def test_the_readme_shows_the_ci_badge_for_this_workflow() -> None:
    readme = README.read_text(encoding="utf-8")

    badge = "https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml/badge.svg"
    assert badge in readme
    sentence = "Every pull request and push to main runs the repository quality gates"
    assert sentence in readme
    for unwanted in ("codecov", "coveralls", "shields.io/pypi", "CodeQL"):
        assert unwanted not in readme, unwanted
