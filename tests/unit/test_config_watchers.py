"""`[watchers]` config: fixed public targets, validated before anything is fetched (ADR-0029).

The section is deliberately poor in expressive power. A target is an id and an HTTPS URL; there is
no header, no credential, no cookie jar, no method, no selector and no "follow redirects" switch,
because a watcher is a fixed public page and not a generic HTTP client.
"""

from __future__ import annotations

import pytest

from assistant.domain.config import (
    DEFAULT_WATCHER_FULL_FETCH_EVERY,
    DEFAULT_WATCHER_MAX_RESPONSE_BYTES,
    DEFAULT_WATCHER_POLL_SECONDS,
    DEFAULT_WATCHER_TIMEOUT_SECONDS,
    AssistantConfig,
    WatchersConfig,
    WebTargetConfig,
)
from assistant.domain.errors import InvalidAssistantConfig


def _target(**overrides: object) -> WebTargetConfig:
    values: dict[str, object] = {"id": "course-notices", "url": "https://example.edu/notices"}
    values.update(overrides)
    return WebTargetConfig(**values)  # type: ignore[arg-type]


def test_the_default_is_no_watchers() -> None:
    config = AssistantConfig.empty()

    assert config.watchers.web == ()
    assert config.watchers.enabled_targets == ()
    assert config.watchers.poll_interval_seconds == DEFAULT_WATCHER_POLL_SECONDS == 300
    assert config.watchers.timeout_seconds == DEFAULT_WATCHER_TIMEOUT_SECONDS == 20
    assert config.watchers.max_response_bytes == DEFAULT_WATCHER_MAX_RESPONSE_BYTES
    assert config.watchers.max_response_bytes == 2 * 1024 * 1024
    assert config.watchers.full_fetch_every == DEFAULT_WATCHER_FULL_FETCH_EVERY == 24


def test_the_section_is_optional() -> None:
    assert AssistantConfig.from_mapping({"format_version": 1}).watchers == WatchersConfig()


def test_a_configured_target_is_parsed_and_defaults_to_enabled() -> None:
    config = AssistantConfig.from_mapping(
        {
            "format_version": 1,
            "watchers": {
                "web": [{"id": "course-notices", "url": "https://example.edu/notices"}]
            },
        }
    )

    assert [target.id for target in config.watchers.web] == ["course-notices"]
    assert config.watchers.web[0].url == "https://example.edu/notices"
    assert config.watchers.web[0].enabled is True
    assert [target.id for target in config.watchers.enabled_targets] == ["course-notices"]


def test_a_disabled_target_is_configured_but_not_watched() -> None:
    config = AssistantConfig.from_mapping(
        {
            "format_version": 1,
            "watchers": {
                "web": [
                    {
                        "id": "course-notices",
                        "url": "https://example.edu/notices",
                        "enabled": False,
                    }
                ]
            },
        }
    )

    assert config.watchers.enabled_targets == ()


@pytest.mark.parametrize(
    "target_id", ["Course", "1course", "-course", "course_notices", "course.notices", "x" * 64, ""]
)
def test_a_target_id_must_use_the_stable_grammar(target_id: str) -> None:
    with pytest.raises(InvalidAssistantConfig):
        _target(id=target_id)


def test_a_target_id_of_the_maximum_length_is_accepted() -> None:
    assert _target(id="a" * 63).id == "a" * 63


@pytest.mark.parametrize(
    "url",
    [
        "http://example.edu/notices",
        "file:///etc/passwd",
        "ftp://example.edu/notices",
        "data:text/html,hello",
        "javascript:alert(1)",
        "https://user:secret@example.edu/notices",
        "https://user@example.edu/notices",
        "https://127.0.0.1/notices",
        "https://[::1]/notices",
        "https://example.edu:8443/notices",
        "",
    ],
)
def test_an_unsafe_url_is_refused_by_configuration(url: str) -> None:
    with pytest.raises(InvalidAssistantConfig):
        _target(url=url)
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {"web": [{"id": "x", "url": url}]}}
        )


def test_a_fragment_is_dropped_because_it_never_reaches_a_server() -> None:
    assert _target(url="https://example.edu/notices#section-2").url == (
        "https://example.edu/notices"
    )


def test_duplicate_target_ids_are_refused() -> None:
    with pytest.raises(InvalidAssistantConfig):
        WatchersConfig(web=(_target(), _target(url="https://example.edu/other")))


@pytest.mark.parametrize("key", ["headers", "cookie", "method", "follow_redirects", "selector"])
def test_a_key_that_would_widen_the_fetcher_is_rejected(key: str) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {
                "format_version": 1,
                "watchers": {
                    "web": [
                        {
                            "id": "course-notices",
                            "url": "https://example.edu/notices",
                            key: "x",
                        }
                    ]
                },
            }
        )


def test_unknown_watcher_keys_are_rejected() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {"interval": 10}}
        )
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "watchers": {"web": "yes"}})
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping({"format_version": 1, "watchers": "yes"})


def test_a_target_needs_an_id_and_a_url() -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {"web": [{"url": "https://example.edu"}]}}
        )
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {"web": [{"id": "x"}]}}
        )
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {
                "format_version": 1,
                "watchers": {"web": [{"id": "x", "url": "https://example.edu", "enabled": "yes"}]},
            }
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("poll_interval_seconds", 9),
        ("poll_interval_seconds", 86401),
        ("timeout_seconds", 0),
        ("timeout_seconds", 121),
        ("max_response_bytes", 1023),
        ("max_response_bytes", 32 * 1024 * 1024 + 1),
        ("full_fetch_every", 0),
        ("full_fetch_every", 1001),
    ],
)
def test_the_bounds_are_enforced(key: str, value: int) -> None:
    with pytest.raises(InvalidAssistantConfig):
        WatchersConfig(**{key: value})  # type: ignore[arg-type]
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {key: value}}
        )


@pytest.mark.parametrize(
    "value", ["300", 300.0, True]
)
def test_a_non_integer_setting_is_refused(value: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            {"format_version": 1, "watchers": {"poll_interval_seconds": value}}
        )
