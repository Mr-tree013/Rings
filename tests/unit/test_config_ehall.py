"""`[ehall]` configuration: two keys, and nothing that could hold a credential (ADR-0025)."""

from __future__ import annotations

import pytest

from assistant.domain.config import (
    DEFAULT_EHALL_ENABLED,
    DEFAULT_EHALL_TIMEOUT_SECONDS,
    AssistantConfig,
    EHallConfig,
)
from assistant.domain.errors import InvalidAssistantConfig


def test_the_default_is_disabled() -> None:
    config = AssistantConfig.empty()

    assert config.ehall.enabled is DEFAULT_EHALL_ENABLED is False
    assert config.ehall.timeout_seconds == DEFAULT_EHALL_TIMEOUT_SECONDS == 30


def test_enabling_the_pipeline_needs_only_a_boolean() -> None:
    config = AssistantConfig.from_mapping({"format_version": 1, "ehall": {"enabled": True}})

    assert config.ehall.enabled is True
    assert config.ehall.timeout_seconds == 30


@pytest.mark.parametrize("timeout", [10, 30, 120])
def test_the_timeout_bounds_are_inclusive(timeout: int) -> None:
    config = AssistantConfig.from_mapping(
        {"format_version": 1, "ehall": {"enabled": True, "timeout_seconds": timeout}}
    )

    assert config.ehall.timeout_seconds == timeout


@pytest.mark.parametrize("timeout", [9, 121, 0, -1])
def test_an_out_of_range_timeout_is_refused(timeout: int) -> None:
    with pytest.raises(InvalidAssistantConfig):
        EHallConfig(enabled=True, timeout_seconds=timeout)
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "ehall": {"timeout_seconds": timeout}}
        )


@pytest.mark.parametrize(
    "key",
    [
        "username",
        "password",
        "service_url",
        "submit_selector",
        "verify_tls",
        "headless",
        "allowed_origins",
        "browser",
    ],
)
def test_a_credential_url_or_selector_key_is_rejected(key: str) -> None:
    """None of these belong in configuration: the pipeline is the thing that knows them."""
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "ehall": {"enabled": True, key: "x"}}
        )


def test_a_non_boolean_enabled_is_refused() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "ehall": {"enabled": "yes"}})
    with pytest.raises(InvalidAssistantConfig):
        EHallConfig(enabled=1)  # type: ignore[arg-type]
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "ehall": "yes"})


def test_the_section_is_optional() -> None:
    config = AssistantConfig.from_mapping({"format_version": 1})

    assert config.ehall.enabled is False
