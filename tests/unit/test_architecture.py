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
import re
from pathlib import Path

from assistant.application.integrity_service import REVIEWED_MIGRATION_COUNT
from assistant.domain.config import AssistantConfig
from assistant.store.events import SqliteEventRepository

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"
EHALL_ADAPTER_DIR = "adapters/ehall/"
"""The only package allowed to import Playwright (ADR-0025)."""
SMTP_MODULE = "adapters/mail/smtp.py"
"""The one module allowed to speak SMTP (ADR-0024)."""
RANDOMNESS_MODULES = frozenset(
    {"adapters/security/tokens.py", "adapters/mail/message_ids.py"}
)
"""Where system randomness may be read: approval tokens and RFC Message-IDs (ADR-0023/0024)."""
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
        "0011_approved_mail_send.sql",
        "0012_mobile_web.sql",
        "0013_learning_facts.sql",
        "0014_playbooks.sql",
        "0015_inbound_observations.sql",
        "0016_conversations.sql",
        "0017_conversation_external_reviews.sql",
        "0018_recurring_calendar_rules.sql",
        "0019_contacts_and_outbound_mail.sql",
        "0020_conversation_requests.sql",
        "0021_attention_items.sql",
        "0022_planning_preferences.sql",
        "0023_conversation_review_expansion.sql",
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

    The plural `credentials` is not in the ban, and it is not a loophole: ADR-0043 gives the
    settings surface a `MailCredentialStatus` — `{configured, source_kind, reference}` — which is a
    *fact about* a credential rather than a credential. The singular name stays reserved for the
    thing that must never appear here.
    """
    forbidden_names = {"password", "passwd", "secret", "api_key", "credential"}
    offenders: list[str] = []
    for layer in ("domain", "application", "ports"):
        for path in sorted((SOURCE_ROOT / layer).glob("*.py")):
            for name in _identifiers(path) & forbidden_names:
                offenders.append(f"{layer}/{path.name} names {name}")

    assert not offenders, offenders


def test_smtp_lives_only_in_the_mail_adapter() -> None:
    """`smtplib` is one file, and that file is the only place a message can leave the machine."""
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name == "smtplib" or name.startswith("smtplib.")
            for name in _imported_modules(path)
        )
        and str(path.relative_to(SOURCE_ROOT)) != SMTP_MODULE
    ]

    assert not offenders, offenders
    assert (SOURCE_ROOT / SMTP_MODULE).is_file()


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
    """Without an outbound configuration there is no capability at all."""
    from assistant.bootstrap import registered_action_executors

    assert registered_action_executors() == {}
    assert registered_action_executors(AssistantConfig()) == {}


def test_the_only_registered_capability_is_mail_send_when_smtp_is_configured() -> None:
    """Naming a type still grants nothing: a capability appears only where one was written."""
    from assistant.bootstrap import registered_action_executors
    from assistant.domain.config import MailAccountConfig, MailConfig

    def account(**overrides: object) -> MailAccountConfig:
        values: dict[str, object] = {
            "id": "smail",
            "host": "imap.example.edu",
            "username": "student@example.edu",
            "mailbox": "INBOX",
        }
        values.update(overrides)
        return MailAccountConfig(**values)  # type: ignore[arg-type]

    receive_only = AssistantConfig(mail=MailConfig(accounts=(account(),)))
    sending = AssistantConfig(
        mail=MailConfig(
            accounts=(
                account(
                    smtp_host="smtp.example.edu",
                    smtp_username="student@example.edu",
                    from_address="student@example.edu",
                ),
            )
        )
    )

    assert registered_action_executors(receive_only) == {}
    assert sorted(
        item.value for item in registered_action_executors(sending)
    ) == ["mail.send"]


SEND_MODULES = (
    "application/mail_send_actions.py",
    "application/mail_send_status.py",
    "application/mail_send_reconciliation.py",
    "domain/mail_send.py",
    "ports/mail_send_repository.py",
    "ports/sent_mail_lookup.py",
    "adapters/mail/smtp.py",
    "adapters/mail/sent_lookup.py",
)

BACKGROUND_MODULES = (
    "daemon/app.py",
    "daemon/supervisor.py",
    "application/event_worker.py",
    "application/mail_sync.py",
    "application/mail_event_handler.py",
    "application/mail_drafts.py",
    "application/mail_threading.py",
    "application/scheduler_service.py",
    "application/index_sync.py",
    "application/interpreter.py",
    "application/grounded_answer.py",
)


def test_the_send_path_never_reaches_a_model() -> None:
    """What leaves the machine was approved as bytes; nothing rewrites it on the way out."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in SEND_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "assistant.ports.model",
                "assistant.application.structured_model",
                "assistant.application.interpreter",
                "assistant.application.grounded_answer",
                "assistant.adapters.model",
            )
        )
    ]

    assert not offenders, offenders
    named = [
        f"{relative} names {name}"
        for relative in SEND_MODULES
        for name in _identifiers(SOURCE_ROOT / relative)
        & {"ModelPort", "StructuredModel", "ModelRequest"}
    ]
    assert not named, named


def test_only_the_execution_service_can_reach_a_send_capability() -> None:
    """No daemon, worker, scheduler or model path imports the SMTP executor or its services."""
    forbidden = (
        "assistant.adapters.mail.smtp",
        "assistant.application.mail_send_actions",
        "assistant.application.mail_send_reconciliation",
        "assistant.application.action_execution",
    )
    offenders = [
        f"{relative} imports {imported}"
        for relative in BACKGROUND_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden)
    ]

    assert not offenders, offenders
    text_offenders = [
        f"{relative} mentions {needle}"
        for relative in BACKGROUND_MODULES
        for needle in ("SmtpMailExecutor", "mail.send")
        if needle in (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    ]
    assert not text_offenders, text_offenders


EHALL_MODULES = (
    "domain/ehall.py",
    "ports/ehall_certificate.py",
    "application/ehall_certificate.py",
    "adapters/ehall/session.py",
    "adapters/ehall/page.py",
    "adapters/ehall/nju_certificate.py",
    "adapters/ehall/executor.py",
    "cli_ehall.py",
)

DESTRUCTIVE_ACTION_TYPES = (
    "drop-course",
    "drop_course",
    "withdraw",
    "cancel-application",
    "delete-application",
    "checkout-dorm",
    "resign",
    "submit-any-form",
    "arbitrary-submit",
)


def test_only_the_ehall_package_imports_playwright() -> None:
    """Playwright is one package deep, and nothing above it can name a page or a selector."""
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(
            name.startswith("playwright") for name in _imported_modules(path)
        )
        and not str(path.relative_to(SOURCE_ROOT)).startswith(EHALL_ADAPTER_DIR)
    ]

    assert not offenders, offenders
    assert (SOURCE_ROOT / EHALL_ADAPTER_DIR / "session.py").is_file()


def test_the_application_layer_cannot_name_a_browser() -> None:
    """No port or application module has an identifier for a page, a selector or a click.

    Checked on the syntax tree rather than on prose: the port's docstring *explains* that a caller
    cannot name a selector, and that explanation is the design, not a violation of it.
    """
    forbidden_names = {
        "playwright",
        "selector",
        "locator",
        "goto",
        "click",
        "fill",
        "evaluate",
        "BrowserPort",
        "Page",
    }
    offenders: list[str] = []
    for relative in ("ports/ehall_certificate.py", "application/ehall_certificate.py"):
        path = SOURCE_ROOT / relative
        if names := _identifiers(path) & forbidden_names:
            offenders.append(f"{relative} names {sorted(names)}")
    assert not offenders, offenders


def test_ehall_has_exactly_two_typed_operations() -> None:
    """The port is the certificate errand, not a browser automation surface."""
    from assistant.ports.ehall_certificate import EHallCertificateGateway

    methods = {
        name
        for name in dir(EHallCertificateGateway)
        if not name.startswith("_")
    }
    assert methods == {"inspect_form", "submit_certificate"}


def test_only_the_whitelisted_action_types_exist() -> None:
    """High-risk operations are absent from the code, not forbidden by a prompt.

    Checked on the action-type vocabulary: every `ActionType("…")` the project constructs must be
    one of the two deliberate capabilities. A withdrawal or deletion path cannot appear without
    first appearing as a literal here.
    """
    from assistant.domain.action import ActionType

    allowed = {"mail.send", "ehall.submit-certificate"}
    constructed: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ActionType"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                constructed.add(node.args[0].value)

    assert constructed <= allowed, sorted(constructed - allowed)
    assert ActionType("ehall.submit-certificate").value == "ehall.submit-certificate"
    for destructive in DESTRUCTIVE_ACTION_TYPES:
        assert not destructive.startswith("mail.")  # the vocabulary below is never registered
    from assistant.adapters.ehall.executor import E_HALL_CERTIFICATE_ACTION_TYPE

    assert E_HALL_CERTIFICATE_ACTION_TYPE.value == "ehall.submit-certificate"


def test_no_model_or_background_path_can_reach_the_ehall_pipeline() -> None:
    """A browser opens because a person asked, never because a message or a schedule arrived."""
    forbidden = (
        "assistant.adapters.ehall",
        "assistant.application.ehall_certificate",
        "assistant.ports.ehall_certificate",
        "assistant.domain.ehall",
    )
    watched = (
        *BACKGROUND_MODULES,
        "application/mail_send_actions.py",
        "application/mail_send_reconciliation.py",
    )
    offenders = [
        f"{relative} imports {imported}"
        for relative in watched
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden)
    ]

    assert not offenders, offenders


def test_the_daemon_does_not_supervise_a_browser_service() -> None:
    """There is no `ehall-worker`: the browser runs only while a user is running a command."""
    text = (SOURCE_ROOT / "daemon" / "app.py").read_text(encoding="utf-8")
    for forbidden in ("ehall", "browser", "playwright", "EHall"):
        assert forbidden not in text, forbidden


def test_the_ehall_path_cannot_reach_a_model() -> None:
    """No model decides a form value, and no model can drive the pipeline."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in EHALL_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(
            (
                "assistant.ports.model",
                "assistant.application.structured_model",
                "assistant.adapters.model",
                "assistant.application.interpreter",
                "assistant.application.grounded_answer",
                "assistant.application.knowledge_search",
            )
        )
    ]

    assert not offenders, offenders


def test_the_ehall_path_has_no_retry_loop() -> None:
    """A submit click is never repeated by code: an unclear result goes to a person."""
    offenders: list[str] = []
    for relative in EHALL_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        for needle in ("retry(", "resubmit", "submit_again", "for attempt in", "while attempt"):
            if needle in text:
                offenders.append(f"{relative} mentions {needle}")
    assert not offenders, offenders


MOBILE_MODULES = (
    "domain/mobile.py",
    "ports/mobile_session_repository.py",
    "application/mobile_auth.py",
    "adapters/web/app.py",
    "adapters/web/server.py",
    "cli_mobile.py",
)

WEB_LAYER_DIR = "adapters/web/"


def test_only_the_web_adapter_imports_the_web_framework() -> None:
    """FastAPI, Starlette and Uvicorn are one package deep; the core stays framework-free."""
    forbidden = ("fastapi", "starlette", "uvicorn")
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if any(name.startswith(forbidden) for name in _imported_modules(path))
        and not str(path.relative_to(SOURCE_ROOT)).startswith(WEB_LAYER_DIR)
    ]

    assert not offenders, offenders
    assert (SOURCE_ROOT / WEB_LAYER_DIR / "app.py").is_file()


def test_the_mobile_path_reaches_no_store_no_model_and_no_executor() -> None:
    """The control plane speaks to application services and nothing else.

    The CLI is allowed exactly one store import — `assistant.store.errors`, the shared vocabulary
    every command module uses to turn a storage failure into a readable message. Nothing else in
    the mobile path may reach the store at all.
    """
    offenders = [
        f"{relative} imports {imported}"
        for relative in MOBILE_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if (
            imported.startswith("assistant.store")
            and not (relative == "cli_mobile.py" and imported == "assistant.store.errors")
        )
        or imported.startswith(
            (
                "assistant.adapters.mail",
                "assistant.adapters.ehall",
                "assistant.adapters.model",
                "assistant.ports.model",
                "assistant.application.action_execution",
                "assistant.application.structured_model",
                "assistant.application.interpreter",
                "assistant.application.grounded_answer",
                "assistant.application.mail_send_actions",
                "assistant.application.ehall_certificate",
                "sqlite3",
            )
        )
    ]

    assert not offenders, offenders


def test_the_web_adapter_cannot_execute_anything() -> None:
    """A phone may approve; running the action stays on the host, with no route for it."""
    text = (SOURCE_ROOT / WEB_LAYER_DIR / "app.py").read_text(encoding="utf-8")
    assert "ActionExecutionService" not in text
    for forbidden in (
        "execute(",
        "smtplib",
        "SmtpMailExecutor",
        "EHallCertificateExecutor",
        "playwright",
        "subprocess",
    ):
        assert forbidden not in text, forbidden


def test_the_mobile_path_has_no_model_and_no_environment_reads() -> None:
    for relative in MOBILE_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        for needle in ("ModelPort", "StructuredModel", "os.environ", "DEEPSEEK"):
            assert needle not in text, f"{relative} mentions {needle}"
        named = _identifiers(SOURCE_ROOT / relative) & {"secrets", "urandom"}
        assert not named, (relative, named)


def test_the_static_ui_never_writes_user_data_as_html() -> None:
    """A task title or an action payload is data in the browser, never markup."""
    static = SOURCE_ROOT / "adapters" / "web" / "static"
    for path in sorted(static.iterdir()):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
            assert forbidden not in text, f"{path.name} mentions {forbidden}"
        assert "http://" not in text, path.name
        assert "https://" not in text, path.name


def test_the_daemon_supervises_the_web_service_without_owning_it() -> None:
    """`mobile-web` is one more supervised service, started only when the control plane is on."""
    app_module = (SOURCE_ROOT / "daemon" / "app.py").read_text(encoding="utf-8")
    assert "mobile_web_service" in app_module
    assert "config.mobile.enabled" in app_module
    supervisor = (SOURCE_ROOT / "daemon" / "supervisor.py").read_text(encoding="utf-8")
    for forbidden in ("fastapi", "uvicorn", "MobileWebService"):
        assert forbidden not in supervisor, forbidden


def test_the_web_service_stops_cleanly() -> None:
    """The server runs as a task so the stop event can end it; nothing is left dangling."""
    text = (SOURCE_ROOT / WEB_LAYER_DIR / "server.py").read_text(encoding="utf-8")
    assert "should_exit" in text
    assert "stop_event.wait()" in text
    assert "asyncio.wait" in text


def test_the_send_path_has_no_automatic_retry_or_resend_command() -> None:
    """A resend is never a code path: it is a decision a person makes in front of the content."""
    from assistant.cli_mail_send import mail_send_app

    offenders: list[str] = []
    for relative in SEND_MODULES:
        text = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
        for needle in ("retry(", "resend(", "send_again", "auto_retry", "for attempt in"):
            if needle in text:
                offenders.append(f"{relative} mentions {needle}")
    assert not offenders, offenders

    # The command surface says the same thing: there is nothing to retry with.
    commands = {command.name for command in mail_send_app.registered_commands}
    assert commands == {"prepare", "show", "reconcile", "list"}
    suspicious = {
        name
        for name in _identifiers(SOURCE_ROOT / "cli_mail_send.py")
        if any(part in name.lower() for part in ("resend", "retry", "sendagain"))
    }
    assert not suspicious, suspicious


def test_no_generic_browser_or_http_capability_exists() -> None:
    """High-risk capabilities are missing from the code, not forbidden by a prompt.

    SMTP is the one transport the project implements, and only in `SMTP_MODULE`; there is still no
    generic browser, HTTP or shell executor to widen the capability set by accident.
    """
    # `httpx` is excluded (the model adapter's client) and `playwright` is allowed only inside the
    # one whitelisted eHall package; everything else stays banned outright.
    forbidden_modules = ("selenium", "pyppeteer", "requests")
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)} imports {imported}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for imported in _imported_modules(path)
        if imported.startswith(forbidden_modules)
        or (
            imported.startswith("playwright")
            and not str(path.relative_to(SOURCE_ROOT)).startswith(EHALL_ADAPTER_DIR)
        )
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


LEARNING_MODULES = (
    "domain/correction.py",
    "domain/fact.py",
    "ports/learning_repository.py",
    "application/learning_service.py",
    "store/learning.py",
    "cli_facts.py",
)

LEARNING_ALLOWED_MODULES = frozenset(
    {
        *LEARNING_MODULES,
        # The composition root wires the service; nothing there promotes or proposes anything.
        "bootstrap.py",
        # Phase 10F (ADR-0038): the conversation may propose facts and read them back, through the
        # thin orchestration below and the bounded context/render/handler glue around it. None of
        # these modules promotes a fact: `confirm_fact` is called in exactly one place, from the
        # human's own explicit turn, and every one of them is checked separately below.
        "application/conversational_facts.py",
        "application/conversation_capabilities/handlers.py",
        "application/conversation_context.py",
        "application/conversation_render.py",
        "application/conversation_service.py",
        # Phase 11B (ADR-0042 §13): the attention projector may *read* pending candidates, because
        # "a candidate is waiting for you" is exactly the kind of live state an inbox has to show.
        # It reads the key only — never the value — and it cannot confirm, reject or promote
        # anything: `confirm_candidate` is still called in exactly one place, from the human's own
        # turn, and that is asserted separately below.
        "application/attention.py",
    }
)

FACT_IDENTIFIERS = frozenset(
    {
        "ConfirmedFact",
        "FactCandidate",
        "FactCandidateStatus",
        "LearningRepository",
        "LearningService",
        "SqliteLearningRepository",
        "add_correction",
        "confirm_candidate",
        "confirm_fact",
        "create_candidate_with_correction",
        "reject_fact",
    }
)
"""Names a module has to use to reach personal facts or to promote one.

`reject_candidate` is absent on purpose: Phase 7B names a *playbook* candidate the same way, and
the fact-side ban is already carried by `LearningService`, `LearningRepository`, `FactCandidate`,
`ConfirmedFact`, `confirm_fact`, `confirm_candidate` and `propose_fact`.
"""

FACT_STORAGE_IMPORTS = (
    "assistant.application.learning_service",
    "assistant.domain.correction",
    "assistant.domain.fact",
    "assistant.ports.learning_repository",
    "assistant.store.learning",
)
"""Imports that would let a module read or write the fact tables."""


def _source_modules(relative_glob: str = "*.py") -> list[Path]:
    """Every module under `src/assistant`, as paths, for whole-tree sweeps."""
    modules = sorted(SOURCE_ROOT.rglob(relative_glob))
    assert modules, "no source modules found"
    return modules


def _relative(path: Path) -> str:
    return str(path.relative_to(SOURCE_ROOT))


def test_only_the_learning_path_can_reach_a_fact_or_a_confirmation() -> None:
    """§16/§17: promoting, proposing and even reading a fact are one module's business.

    The allow-list is the learning modules, the composition root that wires them, and — since
    Phase 10F — the conversation modules that propose and read facts on the user's behalf. It stays
    closed: no worker, no daemon service, no web route, no action path and no adapter may name
    `ConfirmedFact`, `FactCandidate`, `LearningService` or a confirmation call.
    """
    named = [
        f"{_relative(path)} names {name}"
        for path in _source_modules()
        if _relative(path) not in LEARNING_ALLOWED_MODULES
        for name in _identifiers(path) & FACT_IDENTIFIERS
    ]
    imported = [
        f"{_relative(path)} imports {module}"
        for path in _source_modules()
        if _relative(path) not in LEARNING_ALLOWED_MODULES
        for module in _imported_modules(path)
        if module.startswith(FACT_STORAGE_IMPORTS)
    ]

    assert not named, named
    assert not imported, imported


def test_only_one_conversation_module_can_promote_a_fact() -> None:
    """Phase 10F: the conversation reaches facts through one orchestration, and confirms there.

    `confirm_fact` may be named by the learning service (which defines it), the CLI fact commands,
    and `ConversationalFactService` — the single place the conversation's deterministic
    confirmation path calls. No handler, no interpreter and no renderer may promote a fact, so
    "the model cannot confirm" is a property of the import graph rather than of a prompt.
    """
    allowed = {
        "application/learning_service.py",
        "application/conversational_facts.py",
        "cli_facts.py",
    }
    offenders = [
        _relative(path)
        for path in _source_modules()
        if _relative(path) not in allowed and "confirm_fact" in _identifiers(path)
    ]

    assert offenders == [], offenders


def test_the_learning_application_path_reaches_no_store_no_model_and_nothing_executable() -> None:
    """The service speaks to its repository port and nothing else.

    It composes no store, opens no file, calls no model and knows no executor: a fact cannot become
    a task, a message or a side effect by accident here.
    """
    forbidden = (
        "assistant.store",
        "assistant.adapters",
        "assistant.ports.model",
        "assistant.application.structured_model",
        "assistant.application.action_execution",
        "assistant.application.interpreter",
        "assistant.application.grounded_answer",
        "assistant.application.mail",
        "assistant.application.ehall",
        "sqlite3",
        "httpx",
        "jsonschema",
    )
    offenders = [
        f"application/learning_service.py imports {imported}"
        for imported in _imported_modules(SOURCE_ROOT / "application" / "learning_service.py")
        if imported.startswith(forbidden) or imported in {"httpx", "sqlite3", "jsonschema"}
    ]

    assert not offenders, offenders


def test_the_learning_domain_stays_pure() -> None:
    """Corrections and facts are values: no clock reads, no storage, no framework."""
    for relative in ("domain/correction.py", "domain/fact.py"):
        path = SOURCE_ROOT / relative
        text = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in text, relative
        for imported in _imported_modules(path):
            assert not imported.startswith(
                ("assistant.store", "assistant.adapters", "assistant.application", "sqlite3")
            ), f"{relative} imports {imported}"


def test_no_background_or_model_path_can_propose_or_confirm_a_fact() -> None:
    """A candidate is a person's sentence and a confirmation is a person's decision."""
    watched = [
        *BACKGROUND_MODULES,
        "application/mail_analysis.py",
        "application/grounded_context.py",
        "application/action_execution.py",
        "application/mail_send_actions.py",
        "application/mail_send_reconciliation.py",
        "application/ehall_certificate.py",
        "application/interpreter_context.py",
        "application/structured_model.py",
        "adapters/model/deepseek.py",
        "adapters/web/app.py",
        "adapters/ehall/executor.py",
        "adapters/mail/smtp.py",
        "ports/model.py",
    ]
    offenders = [
        f"{relative} reaches {name}"
        for relative in watched
        for name in (
            _identifiers(SOURCE_ROOT / relative)
            | {
                imported.split(".")[-1]
                for imported in _imported_modules(SOURCE_ROOT / relative)
            }
        )
        if name in FACT_IDENTIFIERS or name in {"learning_repository", "learning_service"}
    ]

    assert not offenders, offenders


def test_the_web_surface_has_no_fact_route() -> None:
    """§35: the control plane's route list does not change in this phase."""
    text = (SOURCE_ROOT / "adapters" / "web" / "app.py").read_text(encoding="utf-8")
    for forbidden in ("/api/facts", "learning", "Learning", "fact_candidates"):
        assert forbidden not in text, forbidden


PLAYBOOK_MODULES = (
    "domain/playbook.py",
    "ports/playbook_replay.py",
    "ports/playbook_repository.py",
    "store/playbooks.py",
    "application/playbook_service.py",
    "application/playbook_replay/mail_send.py",
    "application/playbook_replay/ehall_certificate.py",
    "application/playbook_replay/registry.py",
    "cli_playbooks.py",
)

PLAYBOOK_ALLOWED_MODULES = frozenset(
    {
        *PLAYBOOK_MODULES,
        # The package's own `__init__` re-exports the validators; the composition root wires them.
        "application/playbook_replay/__init__.py",
        "bootstrap.py",
    }
)

PLAYBOOK_IDENTIFIERS = frozenset(
    {
        "Playbook",
        "PlaybookCandidate",
        "PlaybookCandidateStatus",
        "PlaybookId",
        "PlaybookReplayRegistry",
        "PlaybookReplayTest",
        "PlaybookRepository",
        "PlaybookService",
        "PlaybookStatus",
        "SqlitePlaybookRepository",
        "add_replay_test",
        "create_candidate",
        "promote_candidate",
        "retire_playbook",
        "replay_input_fingerprint",
    }
)
"""Names a module has to use to create, test, promote or retire a playbook."""

PLAYBOOK_STORAGE_IMPORTS = (
    "assistant.application.playbook_replay",
    "assistant.application.playbook_service",
    "assistant.domain.playbook",
    "assistant.ports.playbook_replay",
    "assistant.ports.playbook_repository",
    "assistant.store.playbooks",
)
"""Imports that would let a module read or write the playbook tables."""


def test_only_the_playbook_path_can_create_test_or_promote_a_playbook() -> None:
    """§32: one module chain and one command group, and nothing else in the project."""
    named = [
        f"{_relative(path)} names {name}"
        for path in _source_modules()
        if _relative(path) not in PLAYBOOK_ALLOWED_MODULES
        for name in _identifiers(path) & PLAYBOOK_IDENTIFIERS
    ]
    imported = [
        f"{_relative(path)} imports {module}"
        for path in _source_modules()
        if _relative(path) not in PLAYBOOK_ALLOWED_MODULES
        for module in _imported_modules(path)
        if module.startswith(PLAYBOOK_STORAGE_IMPORTS)
    ]

    assert not named, named
    assert not imported, imported


def test_the_playbook_application_path_reaches_no_store_no_model_and_nothing_executable() -> None:
    """§34/§50: the service reads actions, writes playbooks and cannot run anything."""
    forbidden = (
        "assistant.store",
        "assistant.adapters",
        "assistant.ports.model",
        "assistant.ports.action_executor",
        "assistant.application.structured_model",
        "assistant.application.action_execution",
        "assistant.application.approval_service",
        "assistant.application.interpreter",
        "assistant.application.grounded_answer",
        "assistant.application.mail",
        "assistant.application.ehall",
    )
    offenders = [
        f"application/playbook_service.py imports {imported}"
        for imported in _imported_modules(SOURCE_ROOT / "application" / "playbook_service.py")
        if imported.startswith(forbidden)
        or imported in {"sqlite3", "httpx", "jsonschema", "smtplib", "playwright"}
    ]

    assert not offenders, offenders
    # The prose in that module *explains* these boundaries; the real check is that its code never
    # names them, so the sweep looks at identifiers rather than at the docstring text.
    banned_names = {
        "ActionExecutionService",
        "ActionExecutor",
        "ApprovalService",
        "ExecutionOutcome",
        "smtplib",
        "playwright",
        "execute",
    }
    named = _identifiers(SOURCE_ROOT / "application" / "playbook_service.py") & banned_names
    assert not named, named


def test_the_playbook_domain_stays_pure() -> None:
    """A candidate, a replay test and a playbook are values: no clock, no storage, no framework."""
    path = SOURCE_ROOT / "domain" / "playbook.py"
    text = path.read_text(encoding="utf-8")

    assert "datetime.now(" not in text
    for imported in _imported_modules(path):
        assert not imported.startswith(
            ("assistant.store", "assistant.adapters", "assistant.application", "sqlite3")
        ), imported


def test_a_replay_validator_is_not_an_executor() -> None:
    """§9/§50: the dry run may re-parse a payload and nothing else — no client, no browser."""
    forbidden_imports = (
        "assistant.ports.action_executor",
        "assistant.adapters",
        "sqlite3",
        "httpx",
        "smtplib",
        "imaplib",
        "playwright",
        "socket",
        "ssl",
    )
    offenders = [
        f"{relative} imports {imported}"
        for relative in PLAYBOOK_MODULES
        if relative.startswith("application/playbook_replay/")
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden_imports) or imported in set(forbidden_imports)
    ]

    assert not offenders, offenders
    banned_names = {"execute", "ActionExecutor", "submit", "send", "connect"}
    named = [
        f"{relative} names {name}"
        for relative in PLAYBOOK_MODULES
        if relative.startswith("application/playbook_replay/")
        for name in _identifiers(SOURCE_ROOT / relative) & banned_names
    ]
    assert not named, named
    # The port names exactly three things: the type, the contract version and the pure validate.
    port = SOURCE_ROOT / "ports" / "playbook_replay.py"
    assert "class PlaybookReplayValidator(Protocol)" in port.read_text(encoding="utf-8")
    assert not _identifiers(port) & {"ActionExecutor", "execute", "supports"}


def test_there_is_no_playbook_execution_or_parameterisation_api() -> None:
    """§21/§22: not a command, not a class, not a template placeholder anywhere in the source."""
    banned_identifiers = {
        "PlaybookExecutor",
        "PlaybookRunner",
        "apply_playbook",
        "instantiate_playbook",
        "render_playbook",
        "run_playbook",
        "to_action_request",
        "to_action_payload",
    }
    offenders = [
        f"{_relative(path)} names {name}"
        for path in _source_modules()
        for name in _identifiers(path) & banned_identifiers
    ]
    assert not offenders, offenders

    # `${...}` and `{{...}}` would be parameter slots; `}}` alone is just JSON-schema dict syntax.
    template_markers = ("${", "{{")
    offenders = [
        f"{_relative(path)} contains {marker}"
        for path in _source_modules()
        for marker in template_markers
        if marker in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_no_background_or_model_path_can_reach_the_playbook_pipeline() -> None:
    """§32: a successful run is history; only a person turns history into a playbook."""
    watched = [
        *BACKGROUND_MODULES,
        "application/mail_analysis.py",
        "application/action_execution.py",
        "application/mail_send_actions.py",
        "application/mail_send_reconciliation.py",
        "application/ehall_certificate.py",
        "application/learning_service.py",
        "application/structured_model.py",
        "adapters/model/deepseek.py",
        "adapters/web/app.py",
        "adapters/ehall/executor.py",
        "adapters/mail/smtp.py",
        "ports/model.py",
    ]
    offenders = [
        f"{relative} reaches {name}"
        for relative in watched
        for name in (
            _identifiers(SOURCE_ROOT / relative)
            | {
                imported.split(".")[-1]
                for imported in _imported_modules(SOURCE_ROOT / relative)
            }
        )
        if name in PLAYBOOK_IDENTIFIERS
        or name in {"playbook_service", "playbook_repository", "playbooks"}
    ]

    assert not offenders, offenders


def test_the_web_surface_has_no_playbook_route() -> None:
    """§47: the phone review surface does not grow a playbook endpoint."""
    text = (SOURCE_ROOT / "adapters" / "web" / "app.py").read_text(encoding="utf-8")
    for forbidden in ("/api/playbooks", "playbook", "Playbook"):
        assert forbidden not in text, forbidden


WATCH_MODULES = (
    "domain/web_watch.py",
    "domain/manual_input.py",
    "domain/observation_analysis.py",
    "ports/web_source.py",
    "ports/web_watch_repository.py",
    "ports/manual_input_repository.py",
    "ports/observation_analysis_repository.py",
    "ports/content_snapshot_store.py",
    "store/web_watch.py",
    "store/manual_inputs.py",
    "store/observation_analyses.py",
    "application/web_watch.py",
    "application/manual_input_service.py",
    "application/observation_context.py",
    "application/observation_analysis.py",
    "application/observation_analysis_prompt.py",
    "application/observation_analysis_schema.py",
    "application/observation_event_handler.py",
    "adapters/web_watch/http_source.py",
    "adapters/web_watch/snapshot_store.py",
    "adapters/web_watch/extractor.py",
    "cli_watch.py",
    "cli_ingest.py",
)

HTTP_BOUNDARY_MODULES = (
    "adapters/web_watch/http_source.py",
    "adapters/web_watch/snapshot_store.py",
    "adapters/web_watch/extractor.py",
)
"""The only place in the watcher path that may open a connection or read a file."""

WATCH_APPLICATION_MODULES = (
    "application/web_watch.py",
    "application/manual_input_service.py",
    "application/observation_context.py",
    "application/observation_analysis.py",
    "application/observation_analysis_prompt.py",
    "application/observation_analysis_schema.py",
    "application/observation_event_handler.py",
)

MUTATION_MODULES = (
    "assistant.application.task_service",
    "assistant.application.calendar_service",
    "assistant.application.planner_service",
    "assistant.application.scheduler_service",
    "assistant.application.work_service",
    "assistant.application.case_service",
    "assistant.application.action_service",
    "assistant.application.action_execution",
    "assistant.application.approval_service",
    "assistant.application.learning_service",
    "assistant.application.playbook_service",
    "assistant.application.mail_drafts",
    "assistant.application.mail_send_actions",
    "assistant.application.ehall_certificate",
)
"""Services that can change durable state or perform an external side effect."""


def test_http_and_sockets_live_only_in_the_watcher_adapter() -> None:
    """§46: `httpx` and `socket` are the adapter's business, not the core's."""
    forbidden = ("httpx", "socket", "ssl", "requests", "selenium", "playwright")
    offenders = [
        f"{relative} imports {imported}"
        for relative in WATCH_MODULES
        if relative not in HTTP_BOUNDARY_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden) or imported in set(forbidden)
    ]

    assert not offenders, offenders
    adapters = [
        relative
        for relative in WATCH_MODULES
        if relative.startswith("adapters/web_watch/")
        and any(
            imported.startswith(("httpx", "socket"))
            for imported in _imported_modules(SOURCE_ROOT / relative)
        )
    ]
    assert "adapters/web_watch/http_source.py" in adapters


def test_the_watch_application_path_reaches_no_store_no_model_adapter_and_no_mutation() -> None:
    """§46: the application layer speaks to ports, and to nothing that can change the world."""
    forbidden = (
        "assistant.store",
        "assistant.adapters",
        "sqlite3",
        "httpx",
        *MUTATION_MODULES,
    )
    offenders = [
        f"{relative} imports {imported}"
        for relative in WATCH_APPLICATION_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(forbidden) or imported in set(forbidden)
    ]

    assert not offenders, offenders


def test_the_watch_domain_stays_pure() -> None:
    """The values are pure: no clock reads, no storage, no framework, no network."""
    domain_modules = (
        "domain/web_watch.py",
        "domain/manual_input.py",
        "domain/observation_analysis.py",
    )
    for relative in domain_modules:
        path = SOURCE_ROOT / relative
        text = path.read_text(encoding="utf-8")
        assert "datetime.now(" not in text, relative
        for imported in _imported_modules(path):
            assert not imported.startswith(
                ("assistant.store", "assistant.adapters", "assistant.application", "sqlite3")
            ), f"{relative} imports {imported}"
            assert imported not in {"socket", "httpx", "ssl"}, f"{relative} imports {imported}"


def test_a_watcher_url_never_comes_from_a_model() -> None:
    """§2: the URL is configuration. Nothing in the model path can name a watcher target."""
    watched = (
        "application/interpreter.py",
        "application/interpreter_context.py",
        "application/grounded_answer.py",
        "application/mail_event_handler.py",
        "application/observation_event_handler.py",
        "application/structured_model.py",
        "adapters/model/deepseek.py",
        "ports/model.py",
    )
    offenders = [
        f"{relative} imports {imported}"
        for relative in watched
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(("assistant.adapters.web_watch", "assistant.ports.web_source"))
    ]

    assert not offenders, offenders
    # The handler sends a document built from stored content; it has no fetch capability at all.
    handler = SOURCE_ROOT / "application" / "observation_event_handler.py"
    assert not _identifiers(handler) & {
        "fetch",
        "WebSource",
        "HttpWebSource",
        "httpx",
        "get",
        "post",
    }


def test_no_generic_http_tool_exists_in_the_watcher_path() -> None:
    """§9: the interface takes a configured target, never an arbitrary URL from a caller."""
    banned_identifiers = {
        "HttpTool",
        "fetch_url",
        "fetch_arbitrary_url",
        "BrowserTool",
        "open_url",
        "request_url",
    }
    offenders = [
        f"{_relative(path)} names {name}"
        for path in _source_modules()
        for name in _identifiers(path) & banned_identifiers
    ]
    assert not offenders, offenders
    port = (SOURCE_ROOT / "ports" / "web_source.py").read_text(encoding="utf-8")
    assert "class WebSource(Protocol)" in port
    assert "def fetch(self, request: WebFetchRequest)" in port


def test_observation_analysis_cannot_create_work_or_facts_or_actions() -> None:
    """§32: one handler, one durable output — and no import that could write another."""
    handler = _imported_modules(SOURCE_ROOT / "application" / "observation_event_handler.py")

    for imported in handler:
        assert not imported.startswith(MUTATION_MODULES), imported
    identifiers = _identifiers(SOURCE_ROOT / "application" / "observation_event_handler.py")
    for forbidden in (
        "Task",
        "Case",
        "Deadline",
        "CalendarEvent",
        "FactCandidate",
        "ConfirmedFact",
        "Playbook",
        "PlaybookCandidate",
        "ActionRequest",
        "Approval",
        "ExecutionRun",
        "Notification",
        "ScheduledJob",
    ):
        assert forbidden not in identifiers, forbidden


def test_background_paths_still_cannot_promote_facts_or_playbooks() -> None:
    """The new event types add no authority to the workers that process them."""
    watched = [
        *BACKGROUND_MODULES,
        "application/observation_event_handler.py",
        "application/web_watch.py",
        "application/manual_input_service.py",
    ]
    offenders = [
        f"{relative} reaches {name}"
        for relative in watched
        for name in (
            _identifiers(SOURCE_ROOT / relative)
            | {
                imported.split(".")[-1]
                for imported in _imported_modules(SOURCE_ROOT / relative)
            }
        )
        if name in PLAYBOOK_IDENTIFIERS
        or name in FACT_IDENTIFIERS
        or name in {"learning_service", "playbook_service"}
    ]

    assert not offenders, offenders


def test_the_watcher_daemon_service_is_supervised_like_any_other() -> None:
    """§34: one more service in the list, started only when targets are configured."""
    app_module = (SOURCE_ROOT / "daemon" / "app.py").read_text(encoding="utf-8")
    assert "web_watch_service" in app_module
    assert "config.watchers.enabled_targets" in app_module
    service = (SOURCE_ROOT / "application" / "web_watch.py").read_text(encoding="utf-8")
    assert 'name = "web-watch"' in service
    assert "run_forever" in service


def test_the_event_worker_dispatches_exactly_three_event_types() -> None:
    """Mail, a watched page and pasted text — and an unknown type is still refused."""
    text = (SOURCE_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    start = text.index("def mail_event_worker")
    end = text.index("def mail_draft_service")
    worker = text[start:end]

    assert "MAIL_EVENT_TYPE" in worker
    assert "OBSERVATION_WEB_EVENT_TYPE" in worker
    assert "MANUAL_EVENT_TYPE" in worker
    dispatcher = (SOURCE_ROOT / "application" / "mail_event_handler.py").read_text(
        encoding="utf-8"
    )
    assert "no handler is registered for event type" in dispatcher


MCP_ADAPTER_MODULES = (
    "adapters/mcp/__init__.py",
    "adapters/mcp/server.py",
    "adapters/mcp/resources.py",
    "adapters/mcp/tools.py",
)

MCP_FACADE_MODULE = "application/mcp_facade.py"

MCP_FORBIDDEN_IN_FACADE = (
    "mcp",
    "assistant.store",
    "assistant.adapters",
    "sqlite3",
    "httpx",
    "socket",
    "subprocess",
    "assistant.ports.model",
    "assistant.application.structured_model",
    "assistant.application.action_execution",
    "assistant.application.approval_service",
    "assistant.application.learning_service",
    "assistant.application.playbook_service",
    "assistant.ports.action_executor",
)
"""Everything the MCP application surface must not be able to reach."""

MCP_FORBIDDEN_IN_ADAPTER = (
    "assistant.adapters.mail",
    "assistant.adapters.ehall",
    "assistant.adapters.web_watch",
    "assistant.application.action_execution",
    "assistant.application.approval_service",
    "assistant.application.learning_service",
    "assistant.application.playbook_service",
    "assistant.application.mail_drafts",
    "assistant.application.mail_send_actions",
    "assistant.application.ehall_certificate",
    "assistant.ports.action_executor",
    "assistant.ports.web_source",
    "assistant.ports.ehall_certificate",
    "smtplib",
    "playwright",
    "subprocess",
    "socket",
)
"""Everything the MCP adapter must not be able to reach, even with a task write scope."""


def test_only_the_mcp_adapter_imports_the_mcp_sdk() -> None:
    """§48: the SDK is one package deep; the application and domain never see it."""
    offenders = [
        _relative(path)
        for path in _source_modules()
        if any(
            imported == "mcp" or imported.startswith("mcp.")
            for imported in _imported_modules(path)
        )
        and _relative(path) not in MCP_ADAPTER_MODULES
    ]

    assert not offenders, offenders
    for relative in MCP_ADAPTER_MODULES:
        assert (SOURCE_ROOT / relative).is_file()


def test_the_mcp_facade_cannot_reach_a_store_a_model_or_a_side_effect() -> None:
    """§48: the facade speaks to application services and read-only port methods, nothing else."""
    path = SOURCE_ROOT / MCP_FACADE_MODULE
    offenders = [
        f"{MCP_FACADE_MODULE} imports {imported}"
        for imported in _imported_modules(path)
        if imported.startswith(MCP_FORBIDDEN_IN_FACADE)
        or imported in set(MCP_FORBIDDEN_IN_FACADE)
    ]

    assert not offenders, offenders
    named = _identifiers(path) & {
        "ActionExecutionService",
        "ApprovalService",
        "LearningService",
        "PlaybookService",
        "ModelPort",
        "StructuredModel",
        "ActionExecutor",
        "WebSource",
        "connect",
        "execute",
        "approve",
        "promote",
        "confirm_fact",
    }
    assert not named, named


def test_the_mcp_adapter_cannot_reach_a_side_effect_or_a_transport() -> None:
    """§27: even a task write scope grants nothing beyond local commitment state."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in MCP_ADAPTER_MODULES
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(MCP_FORBIDDEN_IN_ADAPTER)
        or imported in set(MCP_FORBIDDEN_IN_ADAPTER)
    ]

    assert not offenders, offenders
    banned_identifiers = {
        "ActionExecutionService",
        "ApprovalService",
        "LearningService",
        "PlaybookService",
        "ActionExecutor",
        "WebSource",
        "SmtpMailExecutor",
        "EHallCertificateExecutor",
        "Playwright",
        "run_shell",
        "read_file",
        "write_file",
        "open_url",
    }
    named = [
        f"{relative} names {name}"
        for relative in MCP_ADAPTER_MODULES
        for name in _identifiers(SOURCE_ROOT / relative) & banned_identifiers
    ]
    assert not named, named


def test_the_mcp_server_is_stdio_only() -> None:
    """§3: no streamable HTTP, no SSE, no listener of any kind."""
    banned = {
        "streamable_http_app",
        "sse_app",
        "run_sse_async",
        "run_streamable_http_async",
        "run_streamable_http",
        "uvicorn",
        "HTTPServer",
        "listen",
        "bind",
    }
    for relative in MCP_ADAPTER_MODULES:
        path = SOURCE_ROOT / relative
        assert not _identifiers(path) & banned, relative
        for imported in _imported_modules(path):
            assert not imported.startswith(("uvicorn", "starlette", "fastapi")), imported
    server = (SOURCE_ROOT / "adapters" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "run_stdio_async" in server


def test_the_mcp_surface_has_no_prompts_sampling_or_roots() -> None:
    """§29/§30: the server answers resources and tools, and asks the client nothing."""
    banned = {
        "sample",
        "elicit",
        "list_roots",
        "roots_callback",
        "add_prompt",
        "prompt",
        "complete",
        "sampling_callback",
        "ctx",
        "Context",
    }
    named = [
        f"{relative} names {name}"
        for relative in MCP_ADAPTER_MODULES
        for name in _identifiers(SOURCE_ROOT / relative) & banned
    ]
    assert not named, named
    # The facade's prose mentions offline *search* roots, so the check is about identifiers and
    # imports rather than about words: no protocol callback, no prompt, no client request.
    facade = SOURCE_ROOT / MCP_FACADE_MODULE
    assert not _identifiers(facade) & banned
    for imported in _imported_modules(facade):
        assert not imported.startswith("mcp"), imported


def test_the_mcp_resource_and_tool_names_are_pinned() -> None:
    """§41: the surface is reviewed, so adding anything to it is a deliberate change."""
    from assistant.adapters.mcp.resources import RESOURCE_URIS
    from assistant.adapters.mcp.tools import (
        COMPLETE_TASK_TOOL,
        CREATE_TASK_TOOL,
        GET_CASE_TOOL,
        GET_TASK_TOOL,
        READ_TOOL_NAMES,
        SEARCH_KNOWLEDGE_TOOL,
        WRITE_TOOL_NAMES,
    )

    assert RESOURCE_URIS == (
        "assistant://status",
        "assistant://tasks/open",
        "assistant://cases/open",
        "assistant://plan/current",
    )
    assert READ_TOOL_NAMES == (GET_TASK_TOOL, GET_CASE_TOOL)
    assert WRITE_TOOL_NAMES == (CREATE_TASK_TOOL, COMPLETE_TASK_TOOL)
    assert SEARCH_KNOWLEDGE_TOOL == "assistant_search_knowledge"
    assert {GET_TASK_TOOL, GET_CASE_TOOL, CREATE_TASK_TOOL, COMPLETE_TASK_TOOL} == {
        "assistant_get_task",
        "assistant_get_case",
        "assistant_create_task",
        "assistant_complete_task",
    }
    assert not [uri for uri in RESOURCE_URIS if "mail" in uri or "fact" in uri]


def test_the_mcp_surface_adds_no_schema() -> None:
    """§49: MCP keeps no durable server state, so the migration set stays the reviewed one."""
    migrations = sorted(path.name for path in (SOURCE_ROOT.parents[1] / "migrations").glob("*.sql"))

    assert migrations[-1] == "0023_conversation_review_expansion.sql"
    assert not [name for name in migrations if "mcp" in name]


def test_the_status_command_advertises_the_mcp_surface_honestly() -> None:
    """§51: the status view names the integration without claiming an agent runtime."""
    text = (SOURCE_ROOT / "cli.py").read_text(encoding="utf-8")

    assert "MCP / VS Code: local stdio, controlled capabilities" in text
    assert "read-only by default" in text
    # The row says what it is not, and it is not dressed up as a working agent.
    assert "no agent runtime, no execution, no approval" in text
    assert "developer integration" in text


OPS_APPLICATION_MODULES = (
    "application/backup_service.py",
    "application/integrity_service.py",
)

OPS_ADAPTER_MODULES = ("adapters/backup/archive.py", "adapters/ops/content_objects.py")

OPS_FORBIDDEN_IMPORTS = (
    # Reading a referenced raw object is local filesystem work; the *network* mail modules are the
    # boundary, so those are named individually rather than banning the whole package.
    "assistant.adapters.mail.smtp",
    "assistant.adapters.mail.imap",
    "assistant.adapters.mail.credentials",
    "assistant.adapters.mail.sent_lookup",
    "assistant.adapters.ehall",
    "assistant.adapters.model",
    "assistant.application.action_execution",
    "assistant.application.approval_service",
    "assistant.application.learning_service",
    "assistant.application.playbook_service",
    "assistant.application.ehall_certificate",
    "assistant.application.mail_send_actions",
    "assistant.ports.action_executor",
    "assistant.ports.model",
    "assistant.ports.web_source",
    "smtplib",
    "imaplib",
    "playwright",
    "httpx",
    "socket",
    "subprocess",
)
"""Everything an operational check must not be able to reach, because it never needs it."""


def test_the_operational_checks_cannot_reach_an_external_effect() -> None:
    """§64: backup and integrity are local, offline and read-only by construction."""
    offenders = [
        f"{relative} imports {imported}"
        for relative in (*OPS_APPLICATION_MODULES, *OPS_ADAPTER_MODULES)
        for imported in _imported_modules(SOURCE_ROOT / relative)
        if imported.startswith(OPS_FORBIDDEN_IMPORTS)
        or imported in set(OPS_FORBIDDEN_IMPORTS)
    ]

    assert not offenders, offenders
    banned_names = {
        "ActionExecutionService",
        "ActionExecutor",
        "ApprovalService",
        "LearningService",
        "PlaybookService",
        "ModelPort",
        "StructuredModel",
        "SmtpMailExecutor",
        "EHallCertificateExecutor",
        "WebSource",
        "connect",
        "execute",
        "approve",
        "promote",
    }
    named = [
        f"{relative} names {name}"
        for relative in OPS_APPLICATION_MODULES
        for name in _identifiers(SOURCE_ROOT / relative) & banned_names
    ]
    assert not named, named


def test_the_restore_path_cannot_write_outside_its_staging_directory() -> None:
    """§18/§39/§64: staging only, atomically renamed, and every member name checked first."""
    service = (SOURCE_ROOT / "application" / "backup_service.py").read_text(encoding="utf-8")

    assert "extractall" not in service
    assert "os.replace(staging, target)" in service
    assert "_require_usable_destination" in service
    # No force, no in-place and no destructive shortcut exists on the restore path.
    for forbidden in ("--force", "in_place", "in-place", "rmtree(target"):
        assert forbidden not in service
    archive = (SOURCE_ROOT / "adapters" / "backup" / "archive.py").read_text(encoding="utf-8")
    assert "extractall" not in archive
    assert "is_allowed_member" in archive


def test_the_archive_format_excludes_derived_and_secret_state() -> None:
    """§12: the format has four kinds, and an index, a profile or a config is not one of them."""
    from assistant.domain.backup import ALLOWED_TOP_LEVEL, is_allowed_member

    assert ALLOWED_TOP_LEVEL == ("manifest.json", "runtime.sqlite3", "mail", "web")
    for refused in (
        "cache/knowledge/index.sqlite3",
        "ehall/nju-profile/Cookies",
        "config.toml",
        ".env",
        "secrets/api_key.txt",
        "indexes/university.sqlite3",
    ):
        assert not is_allowed_member(refused), refused


def test_the_integrity_check_is_read_only() -> None:
    """§26/§64: findings are reported, never repaired, and no migration is applied."""
    repository = (SOURCE_ROOT / "store" / "integrity.py").read_text(encoding="utf-8")
    for forbidden in (
        "UPDATE ",
        "INSERT INTO",
        "DELETE FROM",
        "apply_migrations",
        "commit(",
    ):
        assert forbidden not in repository, forbidden
    cli = (SOURCE_ROOT / "cli_ops.py").read_text(encoding="utf-8")
    assert "Database.read_only" in cli
    assert "runtime_database" not in cli


def test_the_security_boundaries_still_hold_in_one_sweep() -> None:
    """§53: one consolidated check of every boundary, on top of the individual tests.

    This is deliberately redundant with the focused checks above: a refactor that quietly removed
    one of them would still fail here, and a reader can see the whole set in one place.
    """
    execution = {"ActionExecutionService", "ActionExecutor"}
    human_boundary = {"LearningService", "PlaybookService"}
    boundaries: tuple[tuple[str, tuple[str, ...], frozenset[str]], ...] = (
        (
            "model paths cannot create an approval",
            ("application/interpreter.py", "application/grounded_answer.py",
             "application/mail_analysis.py"),
            frozenset(execution | human_boundary | {"ApprovalService", "smtplib"}),
        ),
        (
            "background paths cannot execute an action",
            ("application/event_worker.py", "application/mail_sync.py",
             "application/scheduler_service.py", "daemon/app.py"),
            frozenset(execution | human_boundary | {"smtplib"}),
        ),
        # Mobile is a *human* surface: it may present and approve an ActionRequest, so only the
        # execution side is forbidden here.
        ("mobile cannot execute", ("adapters/web/app.py",), frozenset(execution | {"smtplib"})),
        (
            "mcp cannot approve or execute",
            ("adapters/mcp/tools.py", "application/mcp_facade.py"),
            frozenset(execution | human_boundary | {"ApprovalService", "smtplib"}),
        ),
        (
            "playbook replay cannot execute",
            ("application/playbook_replay/mail_send.py",
             "application/playbook_replay/ehall_certificate.py"),
            frozenset(execution | {"smtplib"}),
        ),
        (
            "ops cannot approve or execute",
            OPS_APPLICATION_MODULES,
            frozenset(execution | human_boundary | {"ApprovalService", "smtplib"}),
        ),
    )
    for label, modules, banned in boundaries:
        for relative in modules:
            path = SOURCE_ROOT / relative
            assert not _identifiers(path) & banned, f"{label}: {relative}"
            for imported in _imported_modules(path):
                assert not imported.startswith(
                    ("assistant.adapters.ehall", "assistant.adapters.mail.smtp",
                     "assistant.adapters.mail.imap", "playwright")
                ), f"{label}: {relative} imports {imported}"


_CREDENTIAL_PATTERNS = (
    # A DeepSeek-shaped key, a PEM block, or a credential-shaped value with no human-readable
    # separators: placeholders in this repository are all written as readable hyphenated phrases,
    # while a pasted real key is one unbroken token.
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"""(?i)\b(?:api_key|apikey|password|passwd|secret|token)"""
        r"""\s*=\s*["']([A-Za-z0-9+/=]{16,})["']"""
    ),
    re.compile(
        r"""(?i)\b(?:api_key|apikey|password|passwd|secret|token)"""
        r"""\s*=\s*["']([^"']{24,})["']"""
    ),
)
"""§54: shapes that only a real credential produces. Deliberately narrow: this scan guards against
a pasted secret, not against every use of the word "secret" in prose. Attribute-style keyword
arguments (`api_key="..."`) are not assignments and are not scanned, which is what keeps the
existing fake-adapter fixtures meaningful."""


def _credential_shape_findings(text: str) -> list[str]:
    """Return the credential-shaped substrings a source file must not contain."""
    findings: list[str] = []
    for pattern in _CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            captured = match.group(0)
            if "-----BEGIN" in captured:
                findings.append(captured)
            elif not re.search(r"[A-Za-z0-9]", captured):
                continue
            else:
                value = match.group(1) if match.groups() else captured
                # A readable placeholder always contains a separator or a repeated filler run.
                if re.search(r"[-_. ]", value) or len(set(value)) < 8:
                    continue
                findings.append(captured[:60])
    return findings


def test_no_credential_shaped_literal_is_committed() -> None:
    """§54: no pasted key, private key block or credential-shaped assignment in source or tests."""
    offenders: list[str] = []
    for path in sorted(
        list(SOURCE_ROOT.rglob("*.py"))
        + list((SOURCE_ROOT.parents[1] / "tests").rglob("*.py"))
    ):
        for captured in _credential_shape_findings(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {captured}")

    assert not offenders, offenders
    # The scan is only meaningful if it actually catches something, so it is checked against a
    # synthetic credential that is assembled here instead of being committed as a literal.
    opaque = "".join("0123456789abcdef"[index % 16] for index in range(32))
    synthetic = "api_" + "key" + ' = "' + opaque + '"'
    assert synthetic in _credential_shape_findings(synthetic)
    assert _credential_shape_findings("-----BEGIN RSA " + "PRIVATE KEY-----")


# ------------------------------------------- version 1 runtime and release contract (ADR-0032)

RUNTIME_ADAPTER_MODULES = (
    "adapters/runtime/__init__.py",
    "adapters/runtime/instance_lock.py",
    "adapters/runtime/permissions.py",
)
"""The only place the operating-system facts of ADR-0032 live."""

PRODUCTION_DEPENDENCIES = (
    "typer>=0.15",
    "rich>=13.9",
    "pypdf>=6.19.0",
    "httpx>=0.28.1",
    "jsonschema>=4.26.0",
    "playwright>=1.63.0",
    "fastapi>=0.141.1",
    "uvicorn>=0.53.0",
    "mcp>=2,<3",
    "prompt-toolkit>=3.0.51,<4",
)
"""Every runtime dependency v1 declares. Adding one is a phase, not a release chore.

`prompt-toolkit` is the one addition after v1: ADR-0040 §6 requires the interactive terminal to
edit lines with a mature editor instead of a home-grown raw-byte loop, and v1.1.1 is the hotfix
that replaces the latter. It is a line-editing frontend only — no TUI framework, no new
capability, and the non-interactive path does not import it.
"""

DEVELOPMENT_DEPENDENCIES = (
    "pytest>=8.3",
    "pytest-asyncio>=0.25",
    "pytest-cov>=6.0",
    "ruff>=0.9",
    "mypy>=1.14",
    "types-jsonschema>=4.26.0.20260518",
)
"""The tools that run the v1 gates, frozen for the same reason."""


def test_only_the_runtime_adapter_touches_the_operating_system() -> None:
    """The lock and the file modes are kernel facts, so they are adapters — and they are the only
    place that reaches for `fcntl` or `os.chmod`."""
    for relative in RUNTIME_ADAPTER_MODULES:
        assert (SOURCE_ROOT / relative).is_file(), relative

    fcntl_users = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "fcntl" in _imported_modules(path)
    ]
    chmod_users = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "os.chmod(" in path.read_text(encoding="utf-8")
    ]

    assert fcntl_users == ["adapters/runtime/instance_lock.py"]
    assert chmod_users == ["adapters/runtime/permissions.py"]


def test_the_store_may_import_exactly_one_adapter_module() -> None:
    """ADR-0032 freezes the one exception: applying an owner-only mode is not a persistence concern,
    and a private copy of `chmod` logic in the store is how two modules drift apart."""
    allowed = {"assistant.adapters.runtime.permissions"}
    violations: list[str] = []
    for path in sorted((SOURCE_ROOT / "store").glob("*.py")):
        for imported in _imported_modules(path):
            if not imported.startswith("assistant.adapters"):
                continue
            if imported not in allowed:
                violations.append(f"{path.name} imports {imported}")

    assert not violations, violations


def test_the_daemon_is_single_instance_and_version_aware() -> None:
    """§3/§5/§6/§26: the daemon takes an OS lock, releases it last, and answers `--version`."""
    module = SOURCE_ROOT / "daemon" / "app.py"
    names = _identifiers(module)
    text = module.read_text(encoding="utf-8")

    assert {"InstanceLock", "InstanceLockHeld", "daemon_lock_path"} <= names
    assert "lock.acquire()" in text
    # Released in the `finally` of the run, after `serve` returns: a successor must never meet a
    # predecessor that is still half-serving.
    assert "lock.release()" in text
    assert "--version" in text
    assert "os._exit" not in text


def test_the_daemon_cannot_execute_an_external_action() -> None:
    """§11/§21/§24: release hardening must not have moved execution into the daemon."""
    daemon_layers = ("daemon/app.py", "daemon/supervisor.py")
    banned_names = {
        "ActionExecutionService",
        "ActionExecutor",
        "SmtpMailExecutor",
        "EHallCertificateExecutor",
        "execute",
    }
    banned_imports = (
        "assistant.application.action_execution",
        "assistant.ports.action_executor",
        "assistant.adapters.mail.smtp",
        "assistant.adapters.ehall",
        "smtplib",
    )
    for relative in daemon_layers:
        path = SOURCE_ROOT / relative
        assert not _identifiers(path) & banned_names, relative
        for imported in _imported_modules(path):
            assert not imported.startswith(banned_imports), f"{relative} imports {imported}"


ATTENTION_MODULE = "application/attention.py"
"""The projector and the service. ADR-0042 §4-5, §21, §25."""


def test_the_attention_projector_cannot_reach_a_model_or_an_effect() -> None:
    """§75: reconciliation is deterministic application logic, and its imports prove it.

    `application/attention.py` reads bounded source collections and writes derived rows. It must
    never be able to ask a model what is important, approve anything, execute anything, or open a
    connection — so the imports are the boundary, not the docstring.
    """
    source = (SOURCE_ROOT / ATTENTION_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    for forbidden in (
        "assistant.ports.model",
        "assistant.application.action_execution",
        "assistant.application.approval_service",
        "assistant.application.learning_service",
        "sqlite3",
        "httpx",
        "playwright",
        "subprocess",
        "asyncio",
    ):
        assert forbidden not in imported, f"{ATTENTION_MODULE} imports {forbidden}"
    # And nothing that could act on a source: no executor, no approval, no model adapter.
    for name in ("ActionExecutor", "ApprovalService", "ModelPort", "ModelRequest"):
        assert name not in _identifiers(SOURCE_ROOT / ATTENTION_MODULE), name


def test_only_the_store_implements_attention_persistence() -> None:
    """§6: attention is durable, and the only module that speaks SQL about it is the store."""
    offenders = [
        _relative(path)
        for path in _source_modules()
        if _relative(path) not in {"store/attention.py", "store/integrity.py"}
        and _relative(path).endswith(".py")
        and "attention_items" in path.read_text(encoding="utf-8")
        # The migration runner and the read-only integrity audit are the two other legitimate
        # readers: neither writes a row, and the audit proves the rows agree with themselves.
        and _relative(path) != "store/migrations.py"
    ]

    assert not offenders, f"modules outside the store name the attention table: {offenders}"


def test_the_attention_vocabulary_has_no_execution_verb() -> None:
    """§12: the model may list, acknowledge and dismiss attention. There is nothing else."""
    from assistant.application.conversation_capabilities import build_phase_10a_registry
    from assistant.domain.conversation_plan import ConversationOperationType

    attention_operations = {
        operation.value
        for operation in ConversationOperationType
        if operation.value.startswith("attention.")
    }

    assert attention_operations == {
        "attention.list",
        "attention.acknowledge",
        "attention.dismiss",
    }
    schema = (SOURCE_ROOT / "application" / "conversation_schema.py").read_text(encoding="utf-8")
    assert "attention.execute" not in schema
    assert callable(build_phase_10a_registry)


def test_the_watcher_cannot_reach_an_attention_effect() -> None:
    """§11: a watcher may produce an observation, and that may be surfaced — nothing more.

    The projector reads `web_observations` and `observation_analyses`; the watcher itself gains no
    ability to settle an item, and no attention module gains an ability to fetch a URL.
    """
    watcher_modules = [
        path
        for path in _source_modules()
        if _relative(path) in {"application/web_watch.py", "adapters/web_watch/fetcher.py"}
        or _relative(path).startswith("adapters/web_watch/")
    ]
    assert watcher_modules, "the watcher modules were not found"

    for path in watcher_modules:
        names = _identifiers(path)
        assert not {name for name in names if name.startswith("Attention")}, path
        assert "attention_repository" not in names


MAIL_SETTINGS_MODULE = "application/mail_account_settings.py"
"""Typed mail metadata and safe views. ADR-0043 §1-8, §13-16."""

SMTP_PROBE_CLASS = "SmtpConnectionProbe"
"""The one connectivity probe allowed to speak SMTP, in the one module allowed to import smtplib."""


def test_the_mail_settings_service_cannot_reach_a_secret_or_an_effect() -> None:
    """§75: metadata and diagnostics, and nothing that could send, approve or disclose."""
    path = SOURCE_ROOT / MAIL_SETTINGS_MODULE
    source = path.read_text(encoding="utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }

    for forbidden in (
        "assistant.adapters.mail.smtp",
        "assistant.application.action_execution",
        "assistant.application.approval_service",
        "assistant.application.mail_send_actions",
        "smtplib",
        "imaplib",
        "httpx",
        "sqlite3",
    ):
        assert forbidden not in imported, f"{MAIL_SETTINGS_MODULE} imports {forbidden}"
    # The service holds no field a secret could be assigned to.
    assert "password" not in {
        name for name in _identifiers(path) if name.isidentifier()
    } - {"inbound_password", "outbound_password", "has_inbound", "has_outbound"}


def test_the_connection_probes_cannot_send_anything() -> None:
    """§26/§75: a connectivity test has no `DATA`, no `sendmail` and no executor.

    The SMTP probe shares a module with the delivery executor, because `smtplib` is allowed in
    exactly one file. The boundary therefore lives inside the class: its body is the thing under
    test, copied out by its own indentation.
    """
    smtp_source = (SOURCE_ROOT / SMTP_MODULE).read_text(encoding="utf-8")
    probe_body = _class_body(smtp_source, SMTP_PROBE_CLASS)

    assert probe_body, "the SMTP probe was not found"
    for forbidden in (".data(", ".mail(", ".rcpt(", "sendmail", "ActionExecutor", "approve("):
        assert forbidden not in probe_body, f"the SMTP probe reaches {forbidden}"
    # It stops at NOOP, which is what makes it a test rather than a send.
    assert ".noop()" in probe_body

    imap_path = SOURCE_ROOT / "adapters" / "mail" / "connection_test.py"
    imap_source = imap_path.read_text(encoding="utf-8")
    # The IMAP side is checked by the calls it actually makes, not by the words in its prose: the
    # module docstring says which commands it avoids, and a text search would read that as guilt.
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(imap_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(ast.parse(imap_source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"store", "expunge", "append", "copy", "delete", "uid"}
    # Read-only is the same call the sync service already makes.
    assert "readonly=True" in imap_source


def test_no_settings_route_can_return_a_secret_or_send_a_message() -> None:
    """§24: the frozen settings surface, checked by name rather than by trust."""
    source = (SOURCE_ROOT / "adapters" / "web" / "settings.py").read_text(encoding="utf-8")
    routes = sorted(re.findall(r'@app\.(get|post|patch|put|delete)\("([^"]+)"', source))

    assert routes == [
        ("get", "/api/settings/mail/accounts"),
        ("get", "/api/settings/planning"),
        ("get", "/settings"),
        ("get", "/settings.css"),
        ("get", "/settings.js"),
        ("patch", "/api/settings/mail/accounts/{account_id}"),
        ("post", "/api/settings/mail/accounts"),
        ("post", "/api/settings/mail/accounts/{account_id}/test-imap"),
        ("post", "/api/settings/mail/accounts/{account_id}/test-smtp"),
    ]
    joined = " ".join(path for _, path in routes)
    for forbidden in ("password", "secret", "send-test", "execute", "approve", "submit"):
        assert forbidden not in joined


def _class_body(source: str, class_name: str) -> str:
    """The text of one class body, up to the next top-level definition."""
    marker = f"class {class_name}"
    start = source.find(marker)
    if start == -1:
        return ""
    rest = source[start + len(marker) :]
    end = len(rest)
    for candidate in ("\nclass ", "\ndef ", "\n__all__"):
        position = rest.find(candidate)
        if position != -1:
            end = min(end, position)
    return rest[:end]


def test_no_release_added_a_production_dependency() -> None:
    """§4/§53: the dependency set is frozen with the capability set."""
    import tomllib

    pyproject = tomllib.loads(
        (SOURCE_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert tuple(pyproject["project"]["dependencies"]) == PRODUCTION_DEPENDENCIES
    # The development tools are pinned too: a release does not silently swap its own gates either.
    assert tuple(pyproject["dependency-groups"]["dev"]) == DEVELOPMENT_DEPENDENCIES


def test_the_release_keeps_the_migration_set_closed() -> None:
    """§4/§12: the migration set is exactly what shipped, and a shipped one is never edited."""
    migrations = sorted(
        (SOURCE_ROOT.parents[1] / "migrations").glob("*.sql")
    )
    names = [path.name for path in migrations]

    assert names[0] == "0001_initial.sql"
    assert names[-1] == "0023_conversation_review_expansion.sql"
    assert len(names) == 23
    assert "0024" not in "".join(names)
    assert len(names) == REVIEWED_MIGRATION_COUNT


def test_status_names_the_v1_capabilities_and_no_imaginary_ones() -> None:
    """§65: the status table is a claim about shipped behaviour, so it is pinned like one."""
    text = (SOURCE_ROOT / "cli.py").read_text(encoding="utf-8")
    reported = (
        "durable event pipeline",
        "per-root full-text index",
        "deterministic weekly proposals",
        "human-approved actions only",
        "IMAP inbound mail (receive-only)",
        "page watchers",
        "human-confirmed facts",
        "non-executing playbooks",
        "same-LAN control plane",
        "read-only integrity check",
        "MCP / VS Code",
        "not implemented",
    )
    for phrase in reported:
        assert phrase in text, phrase
    for overclaim in (
        "fully autonomous",
        "self-improving",
        "automatic action execution",
        "cloud sync",
        "automatic form filling",
    ):
        assert overclaim not in text.lower(), overclaim
