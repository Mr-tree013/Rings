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
    "application/conversation_capabilities/registry.py",
    "application/conversation_capabilities/handlers.py",
)

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
    "assistant.application.mail_send_actions",
    "assistant.application.ehall_certificate",
)

FORBIDDEN_NAMES = (
    "ApprovalService",
    "ActionExecutionService",
    "ActionService",
    "ActionExecutor",
    "MailSendActionService",
    "EHallCertificateService",
    "ModelTool",
    "ToolExecutor",
    "AgentToolLoop",
    "FunctionCallingExecutor",
)


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
    for relative in CONVERSATION_APPLICATION:
        modules = _imported_modules(relative)
        for prefix in FORBIDDEN_IN_APPLICATION:
            offending = sorted(
                module
                for module in modules
                if module == prefix or module.startswith(f"{prefix}.")
            )
            assert not offending, f"{relative} imports {offending}"


def test_no_conversation_module_can_name_an_approval_or_executor() -> None:
    for relative in (*CONVERSATION_DOMAIN, *CONVERSATION_APPLICATION):
        text = _read(relative)
        for name in FORBIDDEN_NAMES:
            assert name not in text, f"{relative} names {name}"


def test_no_dynamic_execution_or_reflection_based_dispatch() -> None:
    for relative in CONVERSATION_APPLICATION:
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


def test_the_migration_set_is_pinned_through_the_conversation_migration() -> None:
    migrations = sorted((SOURCE_ROOT.parents[1] / "migrations").glob("*.sql"))
    names = [path.name for path in migrations]

    assert names[-1] == "0016_conversations.sql"
    assert len([name for name in names if name.startswith("0016")]) == 1
