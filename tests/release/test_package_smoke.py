"""The built artifact is installable and carries what the runtime needs (16, 17, 27-29).

`uv run` proves the *checkout* works. A release has to prove the *wheel* works: it must contain the
migrations and static assets (or an installed `assistantd` cannot migrate a runtime), it must not
contain tests, fixtures, secrets or runtime state, and the three console entry points must run with
nothing but the installed package on `sys.path`.

Dependency installation is deliberately avoided here — the wheels for `playwright` and friends are
not something a release gate should fetch — so the artifact is installed with `--no-deps` into a
temporary directory and run with the suite's own interpreter, which already has the dependencies.
What is under test is the artifact's contents and import graph, not PyPI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from assistant import __version__

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WHEEL_SENTINELS = (
    "SMTP-SECRET-PASSWORD",
    "MAIL-BODY-SECRET-SENTINEL",
    "SECRET-PAGE-CONTENT",
    "TASK-TITLE-THAT-MUST-NOT-BE-LOGGED",
    "FACT-SENTINEL-ROOM-302",
    "super-secret-key-123",
)
"""Strings that live in this repository's fixtures. None belongs in a release artifact."""

RUNTIME_STATE_NAMES = (
    "runtime.sqlite3",
    "assistant.db",
    "mail/raw/",
    "web/snapshots/",
    "nju-profile/",
)


@pytest.fixture(scope="session")
def distribution(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build the wheel and sdist once for the whole session."""
    if shutil.which("uv") is None:  # pragma: no cover - the project is developed with uv
        pytest.skip("uv is not installed, so the distribution cannot be built here")
    out = tmp_path_factory.mktemp("dist")
    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr
    yield out


@pytest.fixture(scope="session")
def wheel(distribution: Path) -> Path:
    wheels = sorted(distribution.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


@pytest.fixture(scope="session")
def sdist(distribution: Path) -> Path:
    archives = sorted(distribution.glob("*.tar.gz"))
    assert len(archives) == 1, archives
    return archives[0]


@pytest.fixture(scope="session")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel, installed into a temporary directory with no dependency resolution."""
    target = tmp_path_factory.mktemp("installed")
    completed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            "--python",
            sys.executable,
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr
    return target


def _run_with_argv(
    installed: Path,
    arguments: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one console entry point against the installed package, outside the checkout.

    `argv[0]` is the script name and the rest is what the wrapper would have passed, so this is the
    entry point's own call path rather than an import of the module by another name.
    """
    module, attribute = _entry_point_for(arguments[0])
    code = (
        "import sys; "
        f"sys.argv = {arguments!r}; "
        f"from {module} import {attribute}; "
        f"{attribute}()"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(installed), **environment},
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _entry_point_for(name: str) -> tuple[str, str]:
    return {
        "pw": ("assistant.cli", "main"),
        "rings": ("assistant.cli_chat", "main"),
        "assistantd": ("assistant.daemon", "main"),
        "growing-assistant-mcp": ("assistant.adapters.mcp.server", "main"),
    }[name]


def _environment(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "config" / "growing-assistant").mkdir(parents=True, exist_ok=True)
    return {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "COLUMNS": "200",
    }


# ------------------------------------------------------------------------- contents


def test_the_wheel_carries_the_migrations_and_the_static_assets(wheel: Path) -> None:
    """§16: a wheel without its SQL or its web assets cannot run a first-run daemon."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    migrations = sorted(name for name in names if name.startswith("assistant/migrations/"))
    static = sorted(name for name in names if name.startswith("assistant/adapters/web/static/"))

    assert len(migrations) == 19, migrations
    assert migrations[0].endswith("0001_initial.sql")
    assert migrations[-1].endswith("0019_contacts_and_outbound_mail.sql")
    assert {Path(name).name for name in static} == {
        "app.js",
        "approve.html",
        "index.html",
        "pair.html",
        "styles.css",
    }
    assert "assistant/cli.py" in names
    assert "assistant/daemon/app.py" in names


def test_the_sdist_carries_the_migrations(wheel: Path, sdist: Path) -> None:
    """§28: the source distribution is a build input, so it needs them too."""
    import tarfile

    with tarfile.open(sdist) as archive:
        names = archive.getnames()

    migrations = sorted(name for name in names if "/migrations/0" in name and name.endswith(".sql"))

    assert len(migrations) == 19, migrations
    assert any(
        name.endswith("migrations/0019_contacts_and_outbound_mail.sql")
        for name in migrations
    )


def test_no_release_artifact_carries_secrets_or_runtime_state(wheel: Path, sdist: Path) -> None:
    """§20/§29: no credential sentinel, no `.env`, no database, no personal object."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not [name for name in names if name.startswith("tests/")]
        assert not [name for name in names if name.endswith((".env", "assistant.db"))]
        for name in RUNTIME_STATE_NAMES:
            assert not [member for member in names if name in member], name
        payloads = [
            archive.read(name) for name in names if name.endswith((".py", ".html", ".js", ".css"))
        ]
    blob = b"\n".join(payloads)
    for sentinel in WHEEL_SENTINELS:
        assert sentinel.encode() not in blob, sentinel

    import tarfile

    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    for name in RUNTIME_STATE_NAMES:
        assert not [member for member in sdist_names if name in member], name
    assert not [name for name in sdist_names if name.endswith(".env")]


# ------------------------------------------------------------------- installed artifact


def test_the_installed_package_reports_the_version_everywhere(
    installed: Path, tmp_path: Path
) -> None:
    """§26/§55/§72: one version, from the module, the metadata and all three entry points."""
    environment = _environment(tmp_path)
    module = _run_with_argv(
        installed,
        ["pw", "--version"],
        environment=environment,
        cwd=tmp_path,
    )
    daemon = _run_with_argv(
        installed,
        ["assistantd", "--version"],
        environment=environment,
        cwd=tmp_path,
    )
    mcp = _run_with_argv(
        installed,
        ["growing-assistant-mcp", "--version"],
        environment=environment,
        cwd=tmp_path,
    )

    assert module.returncode == 0, module.stderr
    assert module.stdout.strip() == f"pw {__version__}"
    assert daemon.returncode == 0, daemon.stderr
    assert daemon.stdout.strip() == f"assistantd {__version__}"
    # The MCP server's version contract is stderr, because stdout belongs to the protocol.
    assert mcp.returncode == 0, mcp.stderr
    assert mcp.stdout == ""
    assert mcp.stderr.strip() == f"growing-assistant-mcp {__version__}"


def test_the_installed_package_migrates_and_audits_a_fresh_runtime(
    installed: Path, tmp_path: Path
) -> None:
    """§17/§55: migrations resolve inside the package, so a first run works outside a checkout."""
    environment = _environment(tmp_path)

    created = _run_with_argv(
        installed,
        ["pw", "task", "add", "A task from the installed package"],
        environment=environment,
        cwd=tmp_path,
    )
    integrity = _run_with_argv(
        installed, ["pw", "integrity", "check"], environment=environment, cwd=tmp_path
    )
    doctor = _run_with_argv(installed, ["pw", "doctor"], environment=environment, cwd=tmp_path)
    status = _run_with_argv(installed, ["pw", "status"], environment=environment, cwd=tmp_path)

    assert created.returncode == 0, created.stderr
    assert (tmp_path / "data" / "growing-assistant" / "assistant.db").is_file()
    assert integrity.returncode == 0, integrity.stdout + integrity.stderr
    assert "PASS" in integrity.stdout
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert status.returncode == 0, status.stdout + status.stderr


def test_the_installed_package_declares_exactly_four_entry_points(installed: Path) -> None:
    """The console scripts are the artifact's public interface, and four is the whole set.

    `rings` is the primary conversational entry point (ADR-0033 §17); `pw` stays the
    advanced/admin surface, and the daemon and the MCP server keep their v1 identifiers.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import distribution;"
            "d = distribution('growing-assistant');"
            "print(d.version);"
            "print(sorted((e.name, e.value) for e in d.entry_points))",
        ],
        env={**os.environ, "PYTHONPATH": str(installed)},
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    version, entry_points = completed.stdout.strip().splitlines()
    assert version == __version__
    assert entry_points == (
        "[('assistantd', 'assistant.daemon:main'), "
        "('growing-assistant-mcp', 'assistant.adapters.mcp.server:main'), "
        "('pw', 'assistant.cli:main'), "
        "('rings', 'assistant.cli_chat:main')]"
    )


def test_the_installed_package_finds_its_migrations_and_assets(installed: Path) -> None:
    """The two non-Python payloads resolve from the package, not from the checkout."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from assistant.store.migrations import default_migrations_dir;"
            "from assistant.adapters.web.app import STATIC_DIR;"
            "import assistant, pathlib;"
            "root = pathlib.Path(assistant.__file__).parent;"
            "print(default_migrations_dir());"
            "print(len(sorted(default_migrations_dir().glob('*.sql'))));"
            "print(STATIC_DIR);",
        ],
        env={**os.environ, "PYTHONPATH": str(installed)},
        cwd=str(installed),
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    migrations, count, static = completed.stdout.strip().splitlines()
    assert Path(migrations) == Path(installed) / "assistant" / "migrations"
    assert count == "19"
    assert Path(static) == Path(installed) / "assistant" / "adapters" / "web" / "static"
    assert Path(static).is_dir()
