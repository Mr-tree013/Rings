"""Architecture checks for the conversation runtime (ADR-0033 §30).

Kept dependency-free, like `test_architecture.py`: `ast` plus text, no architecture-test library.
The properties here are the ones a reviewer would otherwise have to keep in their head:

* the conversation domain is pure;
* the application layer reaches no store, no adapter, no SQLite and no provider client;
* no approval, action, execution or mail service is reachable from a conversation;
* the capability registry is a closed mapping, not reflection;
* the daemon never starts a conversation operation on its own;
* the migration set is pinned through 0016.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "assistant"

CONVERSATION_DOMAIN = (
    "domain/conversation.py",
    "domain/conversation_plan.py",
    "domain/conversation_context.py",
)
CONVERSATION_APPLICATION = (
    "application/conversation_service.py",
    "application/conversation_context.py",
    "application/conversation_interpreter.py",
    "application/conversation_schema.py",
    "application/conversation_prompt.py",
    "application/conversation_render.py",
    "application/conversation_recurring_intent.py",
    "application/conversation_capabilities/registry.py",
    "application/conversation_capabilities/handlers.py",
    "application/conversation_external_review.py",
)

MODEL_FACING_APPLICATION = tuple(
    module for module in CONVERSATION_APPLICATION
    if module != "application/conversation_external_review.py"
)
"""Everything the model's plan flows through — and not the controller that settles a review."""

FORBIDDEN_EVERYWHERE = (
    "sqlite3",
    "smtplib",
    "subprocess",
    "socket",
    "playwright",
    "typer",
    "rich",
    "httpx",
)
FORBIDDEN_IN_DOMAIN = (
    *FORBIDDEN_EVERYWHERE,
    "assistant.store",
    "assistant.adapters",
    "assistant.application",
    "assistant.ports",
    "jsonschema",
)
FORBIDDEN_IN_APPLICATION = (
    *FORBIDDEN_EVERYWHERE,
    "assistant.store",
    "assistant.adapters",
    "assistant.application.approval_service",
    "assistant.application.action_service",
    "assistant.application.action_execution",
    "assistant.application.ehall_certificate",
)
"""What the model-facing conversation path may not reach.

The mail-send *preparation* service is allowed from Phase 10B on: it creates an immutable action
and stops. The approval and execution services are not, from anywhere in the model-facing path.
"""

FORBIDDEN_NAMES = (
    "ApprovalService",
    "ActionExecutionService",
    "ActionExecutor",
    "EHallCertificateService",
    "ModelTool",
    "ToolExecutor",
    "AgentToolLoop",
    "FunctionCallingExecutor",
)
"""The authorities the model-facing path may not name.

`MailSendActionService` is deliberately absent from this list: preparing an immutable action is
what Phase 10B adds to the model's reach, and it stops there (ADR-0034 §5-6).
"""


def _read(relative: str) -> str:
    return (SOURCE_ROOT / relative).read_text(encoding="utf-8")


def _imported_modules(relative: str) -> set[str]:
    tree = ast.parse(_read(relative), filename=relative)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_the_conversation_domain_is_pure() -> None:
    for relative in CONVERSATION_DOMAIN:
        modules = _imported_modules(relative)
        for prefix in FORBIDDEN_IN_DOMAIN:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_the_conversation_application_reaches_no_concrete_technology() -> None:
    for relative in MODEL_FACING_APPLICATION:
        modules = _imported_modules(relative)
        for prefix in FORBIDDEN_IN_APPLICATION:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_no_conversation_module_can_name_an_approval_or_executor() -> None:
    """The model-facing path may not name the approval or execution machinery at all.

    The one exception is the deterministic controller, whose entire job is to settle a review; the
    next test pins what *it* is forbidden to depend on instead.
    """
    for relative in (*CONVERSATION_DOMAIN, *MODEL_FACING_APPLICATION):
        text = _read(relative)
        for name in FORBIDDEN_NAMES:
            assert name not in text, f"{relative} names {name}"


def test_the_external_controller_has_no_model_in_its_graph() -> None:
    """ADR-0034 §20: the thing that can send mail cannot talk to a model."""
    relative = "application/conversation_external_review.py"
    modules = _imported_modules(relative)

    for forbidden in (
        "assistant.ports.model",
        "assistant.adapters.model",
        "assistant.application.conversation_interpreter",
        "assistant.application.conversation_prompt",
        "assistant.application.structured_model",
        "assistant.application.structured_model",
        "assistant.store",
        "assistant.adapters",
    ):
        assert forbidden not in modules, f"{relative} imports {forbidden}"

    tree = ast.parse(_read(relative), filename=relative)
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for name in ("ModelPort", "ModelRequest", "ModelMessage", "ModelResponse", "instructions"):
        assert name not in identifiers, f"{relative} uses {name}"


def test_the_external_controller_may_only_reach_approval_and_execution() -> None:
    """The controller's whole purpose is settlement, so its imports are pinned to that."""
    modules = _imported_modules("application/conversation_external_review.py")
    application_imports = sorted(
        module for module in modules if module.startswith("assistant.application")
    )

    assert application_imports == [
        "assistant.application.action_execution",
        "assistant.application.approval_service",
        "assistant.application.mail_send_status",
    ]


def test_the_model_facing_path_cannot_reach_the_controller() -> None:
    """The interpreter and the capability handlers never import the approval controller."""
    for relative in MODEL_FACING_APPLICATION:
        if relative == "application/conversation_service.py":
            continue  # the service routes a human sentence to it, and nothing else does
        assert (
            "conversation_external_review" not in _imported_modules(relative)
        ), relative


def test_no_dynamic_execution_or_reflection_based_dispatch() -> None:
    for relative in MODEL_FACING_APPLICATION:
        tree = ast.parse(_read(relative), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = node.func
                name = getattr(called, "id", None) or getattr(called, "attr", None)
                assert name not in {"eval", "exec", "__import__"}, f"{relative} calls {name}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"getattr", "setattr"}, f"{relative} uses reflection"
            if isinstance(node, ast.Name):
                assert node.id not in {"getattr", "setattr"}, f"{relative} uses reflection"
        assert "importlib" not in _imported_modules(relative)


def test_the_capability_registry_is_a_closed_mapping() -> None:
    from assistant.application.conversation_capabilities.registry import ConfirmationPolicy

    text = _read("application/conversation_capabilities/registry.py")

    assert {policy.value for policy in ConfirmationPolicy} == {
        "read",
        "local_write",
        "confirm_local",
    }
    assert "EXTERNAL_WRITE = " not in text
    assert "getattr" not in text
    assert "self._by_type" in text


def test_the_daemon_never_runs_conversation_operations() -> None:
    daemon_sources = "\n".join(
        _read(f"daemon/{name}") for name in ("app.py", "supervisor.py")
    )

    assert "conversation" not in daemon_sources.lower()


def test_the_model_is_asked_exactly_once_per_turn() -> None:
    """No `while model_requests_tool` shape exists anywhere in the runtime."""
    for relative in CONVERSATION_APPLICATION:
        tree = ast.parse(_read(relative), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.While):
                raise AssertionError(f"{relative} contains a while loop")
    service = _read("application/conversation_service.py")
    assert service.count("await self._interpreter.plan(") == 1


def test_the_migration_set_is_pinned_through_the_contacts_migration() -> None:
    migrations = sorted((SOURCE_ROOT.parents[1] / "migrations").glob("*.sql"))
    names = [path.name for path in migrations]

    assert names[-1] == "0019_contacts_and_outbound_mail.sql"
    assert len([name for name in names if name.startswith("0019")]) == 1
    assert "0020" not in "".join(names)


# ------------------------------------------------------- weekly commitments (ADR-0036 §9-§13)

RECURRING_DOMAIN = ("domain/recurring_calendar.py",)
RECURRING_APPLICATION = (
    "application/recurring_calendar_service.py",
    "application/planner_service.py",
    "application/planning_availability.py",
)
"""The weekly-commitment path: one pure domain, one service, and the planner that reads it."""


def test_the_recurring_calendar_domain_is_pure() -> None:
    for relative in RECURRING_DOMAIN:
        modules = _imported_modules(relative)
        for prefix in FORBIDDEN_IN_DOMAIN:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_recurrence_expansion_reaches_no_model_and_no_store() -> None:
    """Deriving a term of classes is arithmetic over `zoneinfo`, not a model call or a query."""
    forbidden = ("assistant.ports.model", "assistant.adapters", "assistant.store")
    for relative in (*RECURRING_DOMAIN, *RECURRING_APPLICATION):
        modules = _imported_modules(relative)
        for prefix in forbidden:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"
        text = _read(relative)
        assert "ModelPort" not in text, f"{relative} names ModelPort"


def test_a_weekly_rule_is_not_a_scheduled_job() -> None:
    """A class is a fact about the week; a job is something the system does (ADR-0036 §2)."""
    for relative in (*RECURRING_DOMAIN, *RECURRING_APPLICATION, "store/recurring_calendar.py"):
        text = _read(relative)
        assert "ScheduledJob" not in text, f"{relative} names ScheduledJob"
        assert "scheduler_repository" not in text, f"{relative} reaches the scheduler"
    assert "scheduled_jobs" not in _read("store/recurring_calendar.py")


def test_there_is_no_recurrence_grammar_dependency() -> None:
    """v1.1 has four closed operations and no RRULE surface (ADR-0036 §12)."""
    pyproject = (SOURCE_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "rrule" not in pyproject.lower()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "RRULE" not in text, f"{path.name} mentions RRULE"
        assert "recurrence_rule" not in text, f"{path.name} mentions recurrence_rule"


def test_the_conversation_reaches_weekly_rules_through_the_service() -> None:
    """No SQLite, no table name and no second rule model inside the conversation path."""
    handlers = _read("application/conversation_capabilities/handlers.py")
    assert "RecurringCalendarService" in handlers
    for relative in CONVERSATION_APPLICATION:
        text = _read(relative)
        assert "recurring_calendar_rules" not in text, f"{relative} names a table"
        assert "INSERT INTO" not in text, f"{relative} writes SQL"


def test_weekly_rules_add_no_external_capability() -> None:
    """A recurring write is local: no action type, no approval, no execution (ADR-0036 §15)."""
    from assistant.application.conversation_capabilities import (
        ConfirmationPolicy,
        build_phase_10a_registry,
    )
    from assistant.domain.conversation_plan import ConversationOperationType

    recurring = (
        ConversationOperationType.CALENDAR_RECURRING_LIST,
        ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY,
        ConversationOperationType.CALENDAR_RECURRING_EDIT,
        ConversationOperationType.CALENDAR_RECURRING_RETIRE,
    )
    # A conversation operation is not an action type, and nothing here creates one: the four
    # recurring operations never appear in `action_requests`, so no approval can name them.
    handlers = _read("application/conversation_capabilities/handlers.py")
    for operation_type in recurring:
        assert operation_type.value.startswith("calendar.recurring.")
        assert f"ConversationOperationType.{operation_type.name}" in handlers
    assert "prepare_action" not in handlers
    assert "ApprovalService" not in handlers
    assert "EXTERNAL_WRITE" not in {member.value for member in ConfirmationPolicy}
    _ = build_phase_10a_registry  # the registry is built from the handlers, never by reflection


# ------------------------------------------- contacts and new outbound mail (ADR-0037 §37)

CONTACT_DOMAIN = ("domain/contact.py", "domain/new_mail_draft.py")
RECIPIENT_APPLICATION = (
    "application/recipient_resolution.py",
    "application/contacts.py",
    "application/new_mail_drafts.py",
)


def test_the_contact_domain_is_pure() -> None:
    for relative in CONTACT_DOMAIN:
        modules = _imported_modules(relative)
        for prefix in FORBIDDEN_IN_DOMAIN:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_the_recipient_resolver_reaches_no_model_and_no_network() -> None:
    """Resolution is deterministic: the human's words, the contacts and the configured accounts."""
    forbidden = (
        "assistant.ports.model",
        "assistant.adapters",
        "assistant.store",
        "socket",
        "httpx",
        "urllib",
    )
    for relative in RECIPIENT_APPLICATION:
        modules = _imported_modules(relative)
        for prefix in forbidden:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_the_interpreter_cannot_reach_an_approval_or_an_executor() -> None:
    """The model-facing path prepares; only the deterministic controller settles (ADR-0034)."""
    for relative in MODEL_FACING_APPLICATION:
        text = _read(relative)
        for name in ("ApprovalService", "ActionExecutionService", "ActionExecutor"):
            assert name not in text, f"{relative} names {name}"
        modules = _imported_modules(relative)
        assert "smtplib" not in modules, f"{relative} imports smtplib"
        assert "assistant.adapters.mail.smtp" not in modules, relative


def test_the_model_facing_vocabulary_cannot_express_mail_send() -> None:
    from assistant.domain.action import ActionType
    from assistant.domain.conversation_plan import ConversationOperationType

    offered = {member.value for member in ConversationOperationType}
    assert "mail.send" not in offered
    assert not [value for value in offered if value.startswith("approval.")]
    assert not [value for value in offered if value.startswith("action.")]
    assert not [value for value in offered if value.startswith("execution.")]
    assert ActionType("mail.send") == ActionType("mail.send")


def test_new_mail_and_replies_converge_on_one_send_path() -> None:
    """One action type, one link store, one executor — never a second SMTP pipeline."""
    actions = _read("application/mail_send_actions.py")
    assert actions.count('ActionType("mail.send")') == 1
    assert "prepare_send" in actions and "prepare_new_send" in actions
    execution = _read("application/action_execution.py")
    executor_modules = [
        path.name
        for path in (SOURCE_ROOT / "adapters" / "mail").glob("*.py")
        if "smtp" in path.name
    ]
    assert executor_modules == ["smtp.py"]
    assert "smtplib" not in execution  # the application layer never talks to a server
    adapters = sorted(
        path.name for path in SOURCE_ROOT.rglob("*.py") if "smtp" in path.name
    )
    assert adapters == ["smtp.py"], adapters


def test_no_generic_external_write_or_tool_capability_exists() -> None:
    from assistant.application.conversation_capabilities import ConfirmationPolicy
    from assistant.domain.conversation_plan import ConversationOperationType

    assert "EXTERNAL_WRITE" not in {member.value for member in ConfirmationPolicy}
    forbidden = ("shell.", "filesystem.", "browser.", "http.", "tool.", "ehall.", "memory.")
    for member in ConversationOperationType:
        assert not member.value.startswith(forbidden), member.value
    # Phase 10F (ADR-0038) adds exactly three fact operations, and no way to confirm one.
    fact_operations = {
        member.value for member in ConversationOperationType if member.value.startswith("fact.")
    }
    assert fact_operations == {"fact.list", "fact.show", "fact.propose"}


# --------------------------------------- facts and the today brief (ADR-0038, ADR-0039)

FACT_CONVERSATION_MODULES = (
    "application/conversational_facts.py",
    "application/conversation_context.py",
    "application/conversation_service.py",
    "application/conversation_capabilities/handlers.py",
)


def test_the_fact_confirmation_path_has_no_model_dependency() -> None:
    """The words that confirm a fact are parsed by code, and nothing in that path holds a model."""
    intent = _read("application/conversational_facts.py")
    assert "fact_confirmation_intent" in intent
    assert "ModelPort" in intent  # named only in prose, as the thing it deliberately does not have
    modules = _imported_modules("application/conversational_facts.py")
    assert not [module for module in modules if module.startswith("assistant.ports.model")]
    assert not [
        module
        for module in modules
        if module.startswith("assistant.application.structured_model")
    ]


def test_the_conversation_reaches_facts_only_through_the_learning_service() -> None:
    """No SQL, no fact table and no second memory model inside the conversation path."""
    for relative in FACT_CONVERSATION_MODULES:
        modules = _imported_modules(relative)
        assert not [
            module for module in modules if module.startswith("assistant.store")
        ], relative
        text = _read(relative)
        assert "confirmed_facts" not in text, relative
        assert "fact_candidates" not in text, relative
    orchestration = _read("application/conversational_facts.py")
    assert "LearningService" in orchestration
    assert "vector" not in orchestration.lower()


def test_the_today_brief_has_no_model_dependency_and_cannot_mutate() -> None:
    """One deterministic read: no provider, no store, no write anywhere in the module."""
    relative = "application/today_brief.py"
    text = _read(relative)
    modules = _imported_modules(relative)
    assert not [module for module in modules if module.startswith("assistant.store")]
    assert not [module for module in modules if module.startswith("assistant.ports.model")]
    for mutation in (
        "add_",
        "create_",
        "update_",
        "cancel_",
        "apply_",
        "confirm_",
        "reject_",
        "execute",
    ):
        assert f"await self._{mutation}" not in text, mutation
