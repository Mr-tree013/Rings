"""Architecture tests: layer boundaries that must not drift.

Kept deliberately dependency-free — `ast` plus a text check, no architecture-test library.

Covered rules:

- the domain layer is pure (no storage, no adapters, no clock reads);
- the application layer never reaches into the store;
- the greedy planner stays pure: no ports, no store, no clock, no CLI;
- only the store (and other infrastructure modules) may import `sqlite3`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from assistant.store.events import SqliteEventRepository

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"
DOMAIN_DIR = SOURCE_ROOT / "domain"
FORBIDDEN_DOMAIN_IMPORT_PREFIXES = (
    "sqlite3",
    "pypdf",
    "assistant.store",
    "assistant.adapters",
    "assistant.application",
)
LAYERS_WITHOUT_SQLITE = ("application", "domain", "ports")
MODULES_WITHOUT_SQLITE = (
    "cli.py",
    "bootstrap.py",
    "daemon/app.py",
    "daemon/supervisor.py",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _imported_names(path: Path) -> set[str]:
    """Fully-qualified names of `from X import Y` statements."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _domain_modules() -> list[Path]:
    modules = sorted(DOMAIN_DIR.glob("*.py"))
    assert modules, f"no domain modules found under {DOMAIN_DIR}"
    return modules


def test_domain_does_not_import_sqlite_store_or_adapters() -> None:
    violations = [
        f"{path.name} imports {imported}"
        for path in _domain_modules()
        for imported in _imported_modules(path)
        if imported.startswith(FORBIDDEN_DOMAIN_IMPORT_PREFIXES)
    ]

    assert not violations, violations


def test_domain_never_reads_the_clock_directly() -> None:
    offenders = [
        path.name
        for path in _domain_modules()
        if "datetime.now(" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"time must come from a Clock port, not datetime.now(): {offenders}"


def test_application_never_reads_the_clock_directly() -> None:
    offenders = [
        path.name
        for path in sorted((SOURCE_ROOT / "application").glob("*.py"))
        if "datetime.now(" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        "application services must take a Clock, not read wall-clock time: "
        f"{offenders}"
    )


def test_application_does_not_import_the_store() -> None:
    application_modules = sorted((SOURCE_ROOT / "application").glob("*.py"))
    assert application_modules, "no application modules found"

    violations = [
        f"{path.name} imports {imported}"
        for path in application_modules
        for imported in _imported_modules(path)
        if imported.startswith("assistant.store")
    ]

    assert not violations, violations


def test_application_does_not_import_adapters_or_sqlite() -> None:
    application_modules = sorted((SOURCE_ROOT / "application").glob("*.py"))

    violations = [
        f"{path.name} imports {imported}"
        for path in application_modules
        for imported in _imported_modules(path)
        if imported.startswith(("assistant.adapters", "assistant.store", "pypdf"))
        or imported == "sqlite3"
    ]

    assert not violations, violations


def test_ports_do_not_import_concrete_adapters() -> None:
    port_modules = sorted((SOURCE_ROOT / "ports").glob("*.py"))
    assert port_modules, "no port modules found"

    violations = [
        f"{path.name} imports {imported}"
        for path in port_modules
        for imported in _imported_modules(path)
        if imported.startswith(("assistant.adapters", "assistant.store"))
    ]

    assert not violations, violations


def test_only_the_pdf_adapter_imports_pypdf() -> None:
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name == "pypdf" or name.startswith("pypdf.")
            for name in _imported_modules(path)
        )
        and not (path.parent.name == "content" and path.name == "pdf.py")
    ]

    assert not offenders, offenders
    assert (SOURCE_ROOT / "adapters" / "content" / "pdf.py").is_file()


def test_domain_does_not_touch_the_filesystem() -> None:
    offenders: list[str] = []
    for path in _domain_modules():
        if any(name == "os" or name.startswith("os.") for name in _imported_modules(path)):
            offenders.append(f"{path.name} imports os")
        if "pathlib.Path" in _imported_names(path):
            offenders.append(f"{path.name} imports pathlib.Path")
    assert not offenders, offenders


def test_metadata_scanner_never_reads_file_contents() -> None:
    scanner = SOURCE_ROOT / "adapters" / "filesystem" / "scanner.py"
    text = scanner.read_text(encoding="utf-8")

    for forbidden in ("open(", "read_bytes(", "read_text(", "hashlib", "sha256"):
        assert forbidden not in text, f"metadata scanning must not read contents: {forbidden}"


def test_only_infrastructure_imports_sqlite3() -> None:
    modules = [
        path
        for layer in LAYERS_WITHOUT_SQLITE
        for path in sorted((SOURCE_ROOT / layer).glob("*.py"))
    ]
    modules.extend(SOURCE_ROOT / module for module in MODULES_WITHOUT_SQLITE)

    violations = [
        f"{path.relative_to(SOURCE_ROOT)} imports sqlite3"
        for path in modules
        if any(imported == "sqlite3" for imported in _imported_modules(path))
    ]

    assert not violations, violations


def test_store_never_disables_the_sqlite_thread_guard() -> None:
    offenders = [
        path.name
        for path in sorted((SOURCE_ROOT / "store").glob("*.py"))
        if "check_same_thread=" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        "a connection must never be shared across threads; "
        f"check_same_thread must not be used: {offenders}"
    )


def test_repository_public_api_is_async() -> None:
    for name in ("add", "get", "get_by_external_identity", "list_pending", "transition"):
        method = getattr(SqliteEventRepository, name)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_greedy_planner_stays_a_pure_scheduling_function() -> None:
    planner = SOURCE_ROOT / "application" / "greedy_planner.py"
    modules = _imported_modules(planner)
    names = _imported_names(planner)
    text = planner.read_text(encoding="utf-8")

    forbidden_prefixes = (
        "assistant.ports",
        "assistant.store",
        "assistant.adapters",
        "assistant.bootstrap",
        "assistant.cli",
        "sqlite3",
        "random",
        "uuid",
    )
    violations = sorted(
        imported
        for imported in modules
        if imported.startswith(forbidden_prefixes) or imported == "sqlite3"
    )

    assert not violations, f"the planner must not reach outside the domain: {violations}"
    assert not any(name.endswith("Clock") for name in names), names
    for forbidden in ("datetime.now(", "time.time(", "uuid4", "random."):
        assert forbidden not in text, f"the planner must stay deterministic: {forbidden}"
