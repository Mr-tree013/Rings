"""Architecture tests: layer boundaries that must not drift.

Kept deliberately dependency-free — `ast` plus a text check, no architecture-test library.

Covered rules:

- the domain layer is pure (no storage, no adapters, no clock reads);
- the application layer never reaches into the store;
- the greedy planner stays pure: no ports, no store, no clock, no CLI;
- scheduler job payloads are data, never code;
- the model boundary keeps providers, credentials and reasoning out of the core;
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
    "httpx",
    "jsonschema",
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


def _identifiers(path: Path) -> set[str]:
    """Every identifier the module mentions: names, attributes, arguments and identifiers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg is not None):
            identifiers.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
    return identifiers


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
        or imported == "httpx"
        or imported.startswith("httpx.")
    ]

    assert not violations, violations


def test_only_the_structured_output_service_validates_schemas() -> None:
    """`jsonschema` belongs to the one application module that owns output validation."""
    offenders = [
        path.name
        for path in sorted((SOURCE_ROOT / "application").glob("*.py"))
        if any(
            name == "jsonschema" or name.startswith("jsonschema.")
            for name in _imported_modules(path)
        )
        and path.name != "structured_model.py"
    ]

    assert not offenders, offenders
    assert (SOURCE_ROOT / "application" / "structured_model.py").is_file()


def test_ports_do_not_import_concrete_adapters() -> None:
    port_modules = sorted((SOURCE_ROOT / "ports").glob("*.py"))
    assert port_modules, "no port modules found"

    violations = [
        f"{path.name} imports {imported}"
        for path in port_modules
        for imported in _imported_modules(path)
        if imported.startswith(("assistant.adapters", "assistant.store"))
        or imported in {"httpx", "jsonschema"}
        or imported.startswith(("httpx.", "jsonschema."))
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


SCHEDULER_MODULES = (
    "domain/scheduled_job.py",
    "domain/scheduler_payloads.py",
    "domain/notification.py",
    "application/scheduler_service.py",
    "application/rolling_replan.py",
    "store/scheduled_jobs.py",
    "store/scheduler.py",
    "ports/scheduler_repository.py",
)


def test_scheduler_job_payloads_are_data_never_code() -> None:
    """A job payload is typed JSON. Nothing in the scheduler may execute stored content."""
    forbidden = (
        "eval(",
        "exec(",
        "import pickle",
        "pickle.",
        "subprocess",
        "os.system",
        "import importlib",
        "compile(",
    )
    offenders: list[str] = []
    for relative in SCHEDULER_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{relative} contains {needle}")

    assert not offenders, offenders


def test_scheduler_service_reaches_the_database_only_through_ports() -> None:
    module = SOURCE_ROOT / "application" / "scheduler_service.py"
    imported = _imported_modules(module)

    assert not [
        name
        for name in imported
        if name.startswith(("assistant.store", "assistant.adapters")) or name == "sqlite3"
    ]
    assert "sqlite3" not in module.read_text(encoding="utf-8")


def test_daemon_supervises_services_through_the_async_service_protocol() -> None:
    """Supervision depends on the service protocol, never on what a service is made of."""
    module = SOURCE_ROOT / "daemon" / "supervisor.py"
    imported = _imported_modules(module)

    assert not [
        name
        for name in imported
        if name.startswith("assistant.store") or name == "sqlite3"
    ]
    text = module.read_text(encoding="utf-8")
    assert "AsyncService" in text
    assert "SchedulerService" not in text
    assert "IndexSyncService" not in text


PROVIDER_STRINGS = ("api.deepseek.com", "deepseek", "Authorization", "DEEPSEEK_API_KEY")

PROVIDER_NEUTRAL_LAYERS = ("domain", "application", "ports")

PROVIDER_STRING_EXCEPTIONS = frozenset(
    {
        # Configuration validation names the supported providers, which is where a provider
        # *choice* belongs; the adapter is the only place that names an endpoint.
        "domain/config.py",
    }
)


def test_only_the_composition_root_and_the_adapter_know_the_provider() -> None:
    """A provider name, URL or credential variable must not reach the core layers."""
    offenders: list[str] = []
    for layer in PROVIDER_NEUTRAL_LAYERS:
        for path in sorted((SOURCE_ROOT / layer).glob("*.py")):
            relative = f"{layer}/{path.name}"
            if relative in PROVIDER_STRING_EXCEPTIONS:
                continue
            text = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{relative} mentions {needle}"
                for needle in PROVIDER_STRINGS
                if needle.lower() in text.lower()
            )

    assert not offenders, offenders


def test_the_deepseek_endpoint_lives_only_in_the_adapter() -> None:
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "api.deepseek.com" in path.read_text(encoding="utf-8")
        and path.name != "deepseek.py"
    ]

    assert not offenders, offenders


def test_credentials_are_only_read_by_the_composition_root_and_the_adapter() -> None:
    allowed = {"bootstrap.py", "deepseek.py"}
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "DEEPSEEK_API_KEY" in path.read_text(encoding="utf-8")
        and path.name not in allowed
    ]

    assert not offenders, offenders


def test_no_module_persists_or_logs_provider_reasoning() -> None:
    """Reasoning is dropped inside the adapter; nothing else may even name it."""
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)} mentions {needle}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for needle in ("reasoning_content", "chain_of_thought", "chain-of-thought")
        if needle in path.read_text(encoding="utf-8")
    ]

    assert not offenders, offenders


def test_reasoning_fields_exist_only_as_an_accounting_counter() -> None:
    """`reasoning_tokens` is a number; there is no field anywhere for reasoning *text*."""
    import dataclasses

    from assistant.domain import model as model_module

    forbidden = {"reasoning", "reasoning_content", "chain_of_thought", "thinking"}
    offenders = [
        f"{name}.{field.name}"
        for name, value in vars(model_module).items()
        if dataclasses.is_dataclass(value) and isinstance(value, type)
        for field in dataclasses.fields(value)
        if field.name in forbidden
    ]

    assert not offenders, offenders


INTERPRETER_MODULES = (
    "application/interpreter.py",
    "application/interpreter_context.py",
    "application/interpreter_schema.py",
    "application/interpreter_prompt.py",
)

FORBIDDEN_INTERPRETER_IMPORTS = (
    "assistant.store",
    "assistant.adapters",
    "assistant.application.task_service",
    "assistant.application.calendar_service",
    "assistant.application.planner_service",
    "assistant.application.scheduler_service",
    "assistant.application.work_service",
    "assistant.application.knowledge_search",
    "sqlite3",
    "httpx",
)


def test_the_interpreter_cannot_reach_a_mutation_service() -> None:
    """'The interpreter does not execute' is a property of its imports, not a promise."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in INTERPRETER_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(FORBIDDEN_INTERPRETER_IMPORTS)
    ]

    assert not offenders, offenders


def test_the_interpreter_has_no_tool_loop_machinery() -> None:
    forbidden = (
        "tool_registry",
        "tool_choice",
        "function_call",
        "tool_call",
        "agent_loop",
        "ToolRegistry",
    )
    offenders: list[str] = []
    for relative in INTERPRETER_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        offenders.extend(
            f"{relative} mentions {needle}" for needle in forbidden if needle in text
        )
    # The interpreter must not even offer tool-shaped vocabulary to a provider.
    assert not offenders, offenders


def test_the_interpreter_does_not_read_credentials_or_the_environment() -> None:
    offenders = [
        relative
        for relative in INTERPRETER_MODULES
        if "os.environ" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        or "DEEPSEEK" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not offenders, offenders


def test_the_schema_stops_at_the_reviewed_migration_set() -> None:
    """Every migration is a deliberate, reviewed step: nothing appears here by accident."""
    migrations = sorted(path.name for path in (SOURCE_ROOT.parents[1] / "migrations").glob("*.sql"))

    assert migrations == [
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
    ]


MAIL_MODULES = (
    "application/mail_sync.py",
    "ports/mail_source.py",
    "ports/mail_repository.py",
    "ports/mail_parser.py",
    "domain/mail.py",
)


def test_mail_protocol_details_stay_in_the_adapter() -> None:
    """`imaplib`, `ssl`, sockets and the RFC822 parser belong to `adapters/mail`."""
    forbidden = ("imaplib", "ssl", "socket", "smtplib", "email.parser", "email.message")
    offenders = [
        f"{relative} imports {imported}"
        for relative in MAIL_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden)
    ]

    assert not offenders, offenders


def test_only_the_mail_adapter_imports_imaplib() -> None:
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name == "imaplib" or name.startswith("imaplib.")
            for name in _imported_modules(path)
        )
        and path.parent.name != "mail"
    ]

    assert not offenders, offenders


def test_the_mail_path_cannot_reach_a_mutation_service_or_the_model() -> None:
    """Mail ingress stores mail and bridges an event; it classifies and sends nothing."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in MAIL_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "assistant.store",
                "assistant.adapters",
                "assistant.application.task_service",
                "assistant.application.calendar_service",
                "assistant.application.planner_service",
                "assistant.application.scheduler_service",
                "assistant.application.interpreter",
                "assistant.application.grounded_answer",
                "assistant.ports.model",
                "sqlite3",
                "httpx",
            )
        )
    ]

    assert not offenders, offenders


def test_credentials_and_passwords_stay_out_of_the_core_layers() -> None:
    """Only the composition root and the mail adapter may handle a mail credential.

    Checked on identifiers rather than raw text, so documentation may still explain *why* a
    password never lives in configuration.
    """
    forbidden_names = {"password", "passwd", "secret", "api_key", "credential"}
    offenders: list[str] = []
    for layer in ("domain", "application", "ports"):
        for path in sorted((SOURCE_ROOT / layer).glob("*.py")):
            for name in _identifiers(path) & forbidden_names:
                offenders.append(f"{layer}/{path.name} names {name}")

    assert not offenders, offenders


def test_no_smtp_or_sending_capability_exists() -> None:
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name == "smtplib" or name.startswith("smtplib.")
            for name in _imported_modules(path)
        )
    ]

    assert not offenders, offenders


GROUNDED_MODULES = (
    "application/grounded_answer.py",
    "application/grounded_context.py",
    "application/grounded_answer_schema.py",
    "application/grounded_answer_prompt.py",
)


def test_the_grounded_answer_path_cannot_reach_a_mutation_service() -> None:
    """Knowledge answers are read-only: no service that could change anything is imported."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in GROUNDED_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "assistant.store",
                "assistant.adapters",
                "assistant.application.task_service",
                "assistant.application.calendar_service",
                "assistant.application.work_service",
                "assistant.application.planner_service",
                "assistant.application.scheduler_service",
                "assistant.application.interpreter",
                "sqlite3",
                "httpx",
            )
        )
    ]

    assert not offenders, offenders


def test_the_grounded_answer_path_has_no_tool_or_execution_machinery() -> None:
    forbidden = (
        "tool_registry",
        "tool_choice",
        "function_call",
        "tool_call",
        "agent_loop",
        "subprocess",
        "os.system",
    )
    offenders: list[str] = []
    for relative in GROUNDED_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        offenders.extend(
            f"{relative} mentions {needle}" for needle in forbidden if needle in text
        )

    assert not offenders, offenders


def test_source_metadata_is_never_taken_from_model_output() -> None:
    """Citations carry ids only; paths, pages and lines are resolved from local evidence."""
    parser = (SOURCE_ROOT / "application" / "grounded_answer.py").read_text(encoding="utf-8")

    assert "logical_uri" not in parser
    assert "page_number" not in parser
    assert "line_start" not in parser
    schema = (
        SOURCE_ROOT / "application" / "grounded_answer_schema.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("logical_uri", "page_number", "line_start", "root_id", "filename"):
        assert forbidden not in schema


def test_the_grounded_answer_path_does_not_read_credentials_or_the_environment() -> None:
    offenders = [
        relative
        for relative in GROUNDED_MODULES
        if "os.environ" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        or "DEEPSEEK" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not offenders, offenders
