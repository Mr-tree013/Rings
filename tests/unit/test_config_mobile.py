"""`[mobile]` configuration: three keys, and nothing that could widen the control plane (ADR-0026).

The section is deliberately poor in expressive power. There is no way to name a public host, no
switch that trusts a proxy header, no CORS list and no certificate bypass, because the deployment
this project supports is one trusted LAN and the default is that nothing is served at all.
"""

from __future__ import annotations

import pytest

from assistant.domain.config import (
    DEFAULT_MOBILE_BIND,
    DEFAULT_MOBILE_ENABLED,
    DEFAULT_MOBILE_PORT,
    AssistantConfig,
    MobileConfig,
)
from assistant.domain.errors import InvalidAssistantConfig
from assistant.domain.mobile import MobileBindMode


def test_the_default_is_off_and_loopback_only() -> None:
    config = AssistantConfig.empty()

    assert config.mobile.enabled is DEFAULT_MOBILE_ENABLED is False
    assert config.mobile.bind == DEFAULT_MOBILE_BIND == "loopback"
    assert config.mobile.port == DEFAULT_MOBILE_PORT == 8765


def test_the_section_is_optional() -> None:
    config = AssistantConfig.from_mapping({"format_version": 1})

    assert config.mobile == MobileConfig()


@pytest.mark.parametrize("bind", ["loopback", "lan"])
def test_both_bind_modes_are_accepted(bind: str) -> None:
    config = AssistantConfig.from_mapping(
        {"format_version": 1, "mobile": {"enabled": True, "bind": bind, "port": 9000}}
    )

    assert config.mobile.bind == bind
    assert config.mobile.port == 9000
    assert config.mobile.bind_mode is MobileBindMode(bind)


def test_the_bind_mode_is_what_maps_to_an_address() -> None:
    """The bind address is not the boundary; the private-client check is."""
    assert MobileBindMode.LOOPBACK.host == "127.0.0.1"
    assert MobileBindMode.LAN.host == "0.0.0.0"


@pytest.mark.parametrize(
    "bind",
    ["public", "0.0.0.0", "all", "any", "127.0.0.1", "LAN", "LAN "],
)
def test_anything_other_than_the_two_modes_is_refused(bind: str) -> None:
    """`lan` already means every interface; a mode claiming more would be a silent second host."""
    with pytest.raises(InvalidAssistantConfig):
        MobileConfig(enabled=True, bind=bind)
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mobile": {"enabled": True, "bind": bind}}
        )


@pytest.mark.parametrize("port", [1024, 8765, 65535])
def test_the_port_bounds_are_inclusive(port: int) -> None:
    config = AssistantConfig.from_mapping(
        {"format_version": 1, "mobile": {"enabled": True, "port": port}}
    )

    assert config.mobile.port == port


@pytest.mark.parametrize("port", [1023, 65536, 0, -1, 80])
def test_an_out_of_range_port_is_refused(port: int) -> None:
    with pytest.raises(InvalidAssistantConfig):
        MobileConfig(enabled=True, port=port)
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mobile": {"enabled": True, "port": port}}
        )


@pytest.mark.parametrize("port", ["8765", 8765.0, True])
def test_a_port_that_is_not_an_integer_is_refused(port: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mobile": {"enabled": True, "port": port}}
        )


@pytest.mark.parametrize("enabled", ["yes", 1, 0])
def test_a_non_boolean_enabled_is_refused(enabled: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mobile": {"enabled": enabled}}
        )
    with pytest.raises(InvalidAssistantConfig):
        MobileConfig(enabled=enabled)  # type: ignore[arg-type]


def test_the_section_must_be_a_table() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "mobile": "yes"})


@pytest.mark.parametrize(
    "key",
    [
        "hostname",
        "public_url",
        "proxy",
        "trust_proxy",
        "trusted_proxies",
        "cors_origins",
        "allowed_origins",
        "tls",
        "verify_tls",
        "certificate",
        "relay",
        "auth",
        "password",
        "token",
    ],
)
def test_a_key_that_would_widen_the_control_plane_is_rejected(key: str) -> None:
    """Unknown keys are refused rather than ignored: a typo must not silently change the bounds."""
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "mobile": {"enabled": True, key: "x"}}
        )
