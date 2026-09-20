"""`[mcp]` configuration: off by default, read-only by default (ADR-0030).

Three keys, and the interesting thing about each of them is what it cannot say. There is no
transport, no port, no trusted client, no CORS list and no capability list — the surface is fixed by
code, and configuration only decides how much of it exists.
"""

from __future__ import annotations

import pytest

from assistant.domain.config import (
    DEFAULT_MCP_ENABLED,
    DEFAULT_MCP_EXPOSE_KNOWLEDGE,
    DEFAULT_MCP_WRITE_SCOPE,
    AssistantConfig,
    McpConfig,
)
from assistant.domain.errors import InvalidAssistantConfig


def test_the_default_is_disabled_read_only_and_private() -> None:
    config = AssistantConfig.empty()

    assert config.mcp.enabled is DEFAULT_MCP_ENABLED is False
    assert config.mcp.write_scope == DEFAULT_MCP_WRITE_SCOPE == "none"
    assert config.mcp.expose_knowledge is DEFAULT_MCP_EXPOSE_KNOWLEDGE is False
    assert config.mcp.allows_task_writes is False


def test_the_section_is_optional() -> None:
    assert AssistantConfig.from_mapping({"format_version": 1}).mcp == McpConfig()


def test_enabling_the_server_needs_only_a_boolean() -> None:
    config = AssistantConfig.from_mapping(
        {"format_version": 1, "mcp": {"enabled": True}}
    )

    assert config.mcp.enabled is True
    assert config.mcp.write_scope == "none"
    assert config.mcp.expose_knowledge is False


@pytest.mark.parametrize("scope", ["none", "tasks"])
def test_both_write_scopes_are_accepted(scope: str) -> None:
    config = AssistantConfig.from_mapping(
        {"format_version": 1, "mcp": {"enabled": True, "write_scope": scope}}
    )

    assert config.mcp.write_scope == scope
    assert config.mcp.allows_task_writes is (scope == "tasks")


@pytest.mark.parametrize(
    "scope",
    [
        "all",
        "actions",
        "approvals",
        "mail",
        "ehall",
        "facts",
        "playbooks",
        "shell",
        "filesystem",
        "TASKS",
        "",
    ],
)
def test_a_widening_write_scope_is_refused(scope: str) -> None:
    """There is no scope that grants approval, execution or a transport capability."""
    with pytest.raises(InvalidAssistantConfig):
        McpConfig(enabled=True, write_scope=scope)
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mcp": {"enabled": True, "write_scope": scope}}
        )


@pytest.mark.parametrize(
    "key",
    [
        "transport",
        "port",
        "host",
        "bind",
        "token",
        "trusted_clients",
        "cors_origins",
        "allow_sampling",
        "roots",
        "tools",
    ],
)
def test_a_key_that_would_widen_the_surface_is_rejected(key: str) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mcp": {"enabled": True, key: "x"}}
        )


@pytest.mark.parametrize("enabled", ["yes", 1, 0])
def test_a_non_boolean_enabled_is_refused(enabled: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mcp": {"enabled": enabled}}
        )
    with pytest.raises(InvalidAssistantConfig):
        McpConfig(enabled=enabled)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["yes", 1, 0])
def test_a_non_boolean_knowledge_flag_is_refused(value: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mcp": {"expose_knowledge": value}}
        )


def test_the_section_must_be_a_table() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "mcp": "yes"})


def test_a_non_string_write_scope_is_refused() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mcp": {"write_scope": 1}}
        )
