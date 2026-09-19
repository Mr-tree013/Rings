"""Architecture tests: layer boundaries that must not drift.

Kept deliberately dependency-free — `ast` plus a text check, no architecture-test library.

Covered rules:

- the domain layer is pure (no storage, no adapters, no clock reads);
- the application layer never reaches into the store;
- only the store (and other infrastructure modules) may import `sqlite3`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from assistant.store.events import SqliteEventRepository

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"
DOMAIN_DIR = SOURCE_ROOT / "domain"
FORBIDDEN_IMPORT_PREFIXES = ("sqlite3", "assistant.store", "assistant.adapters")
LAYERS_WITHOUT_SQLITE = ("application", "domain", "ports")
MODULES_WITHOUT_SQLITE = ("daemon.py", "cli.py")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _domain_modules() -> list[Path]:
    modules = sorted(DOMAIN_DIR.glob("*.py"))
    assert modules, f"no domain modules found under {DOMAIN_DIR}"
    return modules


def test_domain_does_not_import_sqlite_store_or_adapters() -> None:
    violations = [
        f"{path.name} imports {imported}"
        for path in _domain_modules()
        for imported in _imported_modules(path)
        if imported.startswith(FORBIDDEN_IMPORT_PREFIXES)
    ]

    assert not violations, violations


def test_domain_never_reads_the_clock_directly() -> None:
    offenders = [
        path.name
        for path in _domain_modules()
        if "datetime.now(" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"time must come from a Clock port, not datetime.now(): {offenders}"


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
