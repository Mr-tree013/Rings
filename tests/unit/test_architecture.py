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
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
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


THREADING_MODULES = (
    "application/mail_threading.py",
    "domain/mail_analysis.py",
)

MAIL_INTELLIGENCE_MODULES = (
    "application/mail_event_handler.py",
    "application/mail_analysis.py",
    "application/mail_analysis_schema.py",
    "application/mail_analysis_prompt.py",
    "application/mail_context.py",
    "application/mail_threading.py",
    "ports/mail_intelligence_repository.py",
    "domain/mail_analysis.py",
)


def test_mail_threading_is_code_and_never_a_model() -> None:
    """Thread identity is decided by headers. The linker must not be able to call a model."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in THREADING_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "assistant.ports.model",
                "assistant.adapters",
                "assistant.store",
                "assistant.application.structured_model",
                "assistant.application.mail_analysis",
            )
        )
        or imported == "sqlite3"
    ]

    assert not offenders, offenders


def test_the_mail_analysis_path_cannot_reach_a_mutation_service() -> None:
    """An analysis is a record, not an action: no service that changes state is imported."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in MAIL_INTELLIGENCE_MODULES
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
                "assistant.application.grounded_answer",
                "assistant.application.mail_sync",
                "sqlite3",
                "httpx",
                "smtplib",
            )
        )
    ]

    assert not offenders, offenders


def test_the_mail_analysis_path_keeps_provider_and_protocol_details_out() -> None:
    offenders = [
        f"{relative} imports {imported}"
        for relative in MAIL_INTELLIGENCE_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(("imaplib", "ssl", "socket", "email.parser", "email.message"))
    ]

    assert not offenders, offenders


def test_the_mail_analysis_path_has_no_tool_or_execution_machinery() -> None:
    forbidden = (
        "tool_registry",
        "tool_choice",
        "function_call",
        "tool_call",
        "agent_loop",
        "subprocess",
        "os.system",
        "smtplib",
        "eval(",
        "exec(",
    )
    offenders: list[str] = []
    for relative in MAIL_INTELLIGENCE_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        offenders.extend(
            f"{relative} mentions {needle}" for needle in forbidden if needle in text
        )

    assert not offenders, offenders


def test_the_mail_analysis_path_does_not_read_credentials_or_the_environment() -> None:
    offenders = [
        relative
        for relative in MAIL_INTELLIGENCE_MODULES
        if "os.environ" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        or "DEEPSEEK" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        or "password" in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not offenders, offenders


def test_the_mail_model_context_carries_no_raw_message_or_path() -> None:
    """The context builder cannot name a raw object, a filesystem path or a credential."""
    context = (SOURCE_ROOT / "application" / "mail_context.py").read_text(encoding="utf-8")

    for forbidden in ("list_attachments", "to_addresses", "cc_addresses", "raw_sha256"):
        assert forbidden not in context, f"the mail context must not reach {forbidden}"
    for forbidden in ("Path", "physical_path", "mount_path", "os.environ"):
        assert forbidden not in context, f"mail context must not mention {forbidden}"


def test_the_mail_analysis_schema_has_nowhere_to_put_an_action() -> None:
    """A closed schema is the structural half of 'candidates only'.

    The property sets are pinned exactly: widening what a model may answer is a reviewed change,
    not something that can drift in behind a new field name.
    """
    from assistant.application.mail_analysis_schema import MAIL_ANALYSIS_SCHEMA_V1

    schema = MAIL_ANALYSIS_SCHEMA_V1.schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == [
        "action_candidates",
        "category",
        "requires_reply",
        "summary",
    ]
    assert sorted(schema["required"]) == sorted(schema["properties"])
    candidate = schema["properties"]["action_candidates"]["items"]
    assert candidate["additionalProperties"] is False
    assert sorted(candidate["properties"]) == [
        "interpreted_at",
        "temporal_kind",
        "text",
        "time_text",
    ]
    assert schema["properties"]["category"]["enum"] == [
        "ordinary_correspondence",
        "receipt_result",
        "actionable_notice",
        "unknown",
    ]
    assert candidate["properties"]["temporal_kind"]["enum"] == [
        "none",
        "deadline",
        "event_start",
        "other",
    ]


DRAFT_MODULES = (
    "application/mail_drafts.py",
    "application/mail_draft_context.py",
    "application/mail_draft_schema.py",
    "application/mail_draft_prompt.py",
    "domain/mail_draft.py",
    "ports/mail_draft_repository.py",
)

NOTHING_THAT_COULD_SEND = (
    "smtplib",
    "sendmail",
    "email.mime",
)


def test_the_draft_path_cannot_reach_a_transport_or_a_mutation_service() -> None:
    """Drafting writes drafts. It cannot send, schedule, or change a commitment."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in DRAFT_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                *NOTHING_THAT_COULD_SEND,
                "assistant.store",
                "assistant.adapters",
                "assistant.application.task_service",
                "assistant.application.calendar_service",
                "assistant.application.work_service",
                "assistant.application.planner_service",
                "assistant.application.scheduler_service",
                "assistant.application.interpreter",
                "assistant.application.mail_sync",
                "assistant.application.event_worker",
            )
        )
        or imported in {"sqlite3", "httpx", "smtplib", "socket"}
    ]

    assert not offenders, offenders


def test_the_draft_path_has_no_sending_or_tool_machinery() -> None:
    forbidden = (
        "smtplib",
        "send_mail",
        "sendmail",
        "send_message",
        "tool_registry",
        "tool_choice",
        "function_call",
        "tool_call",
        "agent_loop",
        "subprocess",
        "os.system",
        "eval(",
        "exec(",
    )
    offenders: list[str] = []
    for relative in DRAFT_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        offenders.extend(
            f"{relative} mentions {needle}" for needle in forbidden if needle in text
        )

    assert not offenders, offenders


def test_the_recipient_resolver_cannot_reach_a_model() -> None:
    """Who a reply goes to is derived from headers. No provider is involved at all."""
    module = SOURCE_ROOT / "domain" / "mail_draft.py"
    offenders = [
        imported
        for imported in _imported_modules(module)
        if imported.startswith(
            ("assistant.ports", "assistant.adapters", "assistant.store", "assistant.application")
        )
    ]

    assert not offenders, offenders
    text = module.read_text(encoding="utf-8")
    for forbidden in ("ModelPort", "ModelRequest", "chat", "complete("):
        assert forbidden not in text, forbidden


def test_the_draft_schema_has_nowhere_to_put_a_recipient_or_a_send() -> None:
    from assistant.application.mail_draft_schema import MAIL_REPLY_DRAFT_SCHEMA_V1

    schema = MAIL_REPLY_DRAFT_SCHEMA_V1.schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == ["body", "needs_user_input", "used_source_ids"]
    assert sorted(schema["required"]) == sorted(schema["properties"])


def test_personal_knowledge_is_reachable_only_from_an_explicit_query() -> None:
    """The one call into the index sits behind `if query is None: return (), ()`.

    Checked structurally, because "no email may trigger a search" is a property of the code's
    shape, not a promise in a comment.
    """
    module = SOURCE_ROOT / "application" / "mail_drafts.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_function(node: ast.AST) -> str | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None

    knowledge_calls = {
        enclosing_function(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_knowledge"
    }
    assert knowledge_calls == {"_knowledge_evidence"}

    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_knowledge_evidence"
    )
    body = [
        statement
        for statement in function.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    guard = body[0]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "query is None"
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Return)


def test_nothing_outside_the_cli_can_create_a_draft() -> None:
    """No event, no sync round, no schedule and no daemon startup may trigger drafting."""
    forbidden = ("MailDraftService", "mail_draft_service", "mail_draft_writer", "MailDraft")
    watched = (
        "daemon/app.py",
        "daemon/supervisor.py",
        "application/event_worker.py",
        "application/mail_sync.py",
        "application/mail_event_handler.py",
        "application/scheduler_service.py",
        "application/index_sync.py",
    )
    offenders = [
        f"{relative} mentions {needle}"
        for relative in watched
        for needle in forbidden
        if needle in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not offenders, offenders
    assert "mail_draft_writer" in (SOURCE_ROOT / "cli_mail.py").read_text(encoding="utf-8")


def test_the_draft_path_still_cannot_reach_the_action_layer() -> None:
    """Drafting produces drafts. It never prepares, approves or executes an action."""
    forbidden = ("assistant.application.approval_service", "assistant.application.action_execution",
                 "assistant.application.action_service", "assistant.ports.action_repository",
                 "assistant.ports.action_executor", "assistant.domain.approval",
                 "assistant.domain.execution")
    offenders = [
        f"{relative} imports {imported}"
        for relative in DRAFT_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden)
    ]
    assert not offenders, offenders
    # Checked on identifiers, not on prose: a docstring may *explain* that drafting approves
    # nothing without the module being able to name an approval in code.
    named = [
        f"{relative} names {name}"
        for relative in DRAFT_MODULES
        for name in _identifiers(SOURCE_ROOT / relative)
        & {"Approval", "ApprovalRecord", "ActionRequest", "ExecutionRun", "ActionExecutor"}
    ]
    assert not named, named


ACTION_MODULES = (
    "application/case_service.py",
    "application/action_service.py",
    "application/approval_service.py",
    "application/action_execution.py",
    "domain/case.py",
    "domain/action.py",
    "domain/approval.py",
    "domain/execution.py",
    "ports/case_repository.py",
    "ports/action_repository.py",
    "ports/action_executor.py",
)

MODULES_ALLOWED_TO_KNOW_THE_EXECUTOR_PROTOCOL = frozenset(
    {
        "ports/action_executor.py",
        "application/action_execution.py",
        "bootstrap.py",
        "cli_actions.py",
    }
)


def test_the_action_layer_has_no_transport_and_no_model() -> None:
    """No SMTP, no HTTP, no browser, no shell — and no model anywhere near approval."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in ACTION_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "smtplib",
                "imaplib",
                "socket",
                "ssl",
                "subprocess",
                "assistant.adapters",
                "assistant.store",
                "assistant.ports.model",
                "assistant.application.structured_model",
                "assistant.application.interpreter",
                "assistant.application.grounded_answer",
                "assistant.application.mail_drafts",
                "assistant.application.mail_event_handler",
            )
        )
        or imported in {"sqlite3", "httpx"}
    ]

    assert not offenders, offenders
    for relative in ACTION_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        for needle in (
            "smtplib",
            "ModelPort",
            "StructuredModel",
            "tool_call",
            "agent_loop",
            "os.system",
            "eval(",
            "exec(",
        ):
            assert needle not in text, f"{relative} mentions {needle}"


def test_only_the_composition_root_and_the_execution_path_know_an_executor() -> None:
    """A concrete executor cannot be imported anywhere it could be wired by accident."""
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name.endswith("ActionExecutor") or name.endswith("action_executor")
            for name in _imported_names(path)
        )
        and str(path.relative_to(SOURCE_ROOT))
        not in MODULES_ALLOWED_TO_KNOW_THE_EXECUTOR_PROTOCOL
    ]

    assert not offenders, offenders


def test_the_production_executor_set_is_empty() -> None:
    """Phase 6A ships no capability at all: every named action type is unperformable."""
    from assistant.bootstrap import registered_action_executors

    assert registered_action_executors() == {}


def test_no_smtp_shell_or_browser_capability_exists() -> None:
    """High-risk capabilities are missing from the code, not forbidden by a prompt."""
    forbidden_modules = ("smtplib", "selenium", "playwright", "pyppeteer", "requests")
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)} imports {imported}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for imported in _imported_modules(path)
        if imported.startswith(forbidden_modules)
    ]

    assert not offenders, offenders
    banned_identifiers = {
        "send_mail",
        "sendmail",
        "submit_form",
        "open_browser",
        "run_shell",
        "ActionRequestCreate",
    }
    named = [
        f"{path.relative_to(SOURCE_ROOT)} names {name}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for name in _identifiers(path) & banned_identifiers
    ]
    assert not named, named
