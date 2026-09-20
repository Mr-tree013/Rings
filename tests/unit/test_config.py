"""Unit tests for host configuration: parsing, validation and defaults (ADR-0013)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.adapters.config.toml_config import TomlConfigLoader, default_config_path
from assistant.domain.config import (
    DEFAULT_DEADLINE_REMINDER_OFFSETS,
    DEFAULT_INDEX_INTERVAL_SECONDS,
    DEFAULT_MODEL_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODEL_REASONING_EFFORT,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_REPLAN_DEBOUNCE_SECONDS,
    DEFAULT_SCHEDULER_POLL_SECONDS,
    AssistantConfig,
    ConfiguredLocalRoot,
    ConfiguredVaultRoot,
    IndexingConfig,
    ModelConfig,
    ReminderConfig,
    SchedulerConfig,
)
from assistant.domain.errors import InvalidAssistantConfig
from assistant.domain.storage import StorageKind


def _mapping(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "format_version": 1,
        "storage": {
            "roots": [
                {
                    "kind": "local",
                    "id": "university",
                    "label": "University",
                    "path": "/home/someone/Documents/University",
                }
            ]
        },
    }
    values.update(overrides)
    return values


def test_empty_config_has_no_roots_and_default_policy() -> None:
    config = AssistantConfig.empty()

    assert config.roots == ()
    assert config.indexing == IndexingConfig()
    assert config.indexing.interval_seconds == DEFAULT_INDEX_INTERVAL_SECONDS == 300
    assert config.indexing.run_on_startup is True


def test_valid_local_root() -> None:
    config = AssistantConfig.from_mapping(_mapping())

    root = config.roots[0]
    assert isinstance(root, ConfiguredLocalRoot)
    assert root.root_id == "university"
    assert root.kind is StorageKind.LOCAL
    assert root.label == "University"
    assert root.enabled is True


def test_valid_vault_root_ignores_a_configured_label() -> None:
    config = AssistantConfig.from_mapping(
        _mapping(
            storage={
                "roots": [
                    {
                        "kind": "vault",
                        "id": "archive-main",
                        "label": "not authoritative",
                        "path": "/mnt/e/archive",
                        "enabled": False,
                    }
                ]
            }
        )
    )

    root = config.roots[0]
    assert isinstance(root, ConfiguredVaultRoot)
    assert root.kind is StorageKind.VAULT
    assert root.label == ""  # the manifest decides the label
    assert root.enabled is False


def test_declaration_order_is_preserved() -> None:
    config = AssistantConfig.from_mapping(
        _mapping(
            storage={
                "roots": [
                    {"kind": "vault", "id": "archive-main", "path": "/mnt/e/archive"},
                    {
                        "kind": "local",
                        "id": "university",
                        "label": "University",
                        "path": "/home/someone/uni",
                    },
                    {
                        "kind": "local",
                        "id": "projects",
                        "label": "Projects",
                        "path": "/home/someone/projects",
                    },
                ]
            }
        )
    )

    assert [root.root_id for root in config.roots] == [
        "archive-main",
        "university",
        "projects",
    ]
    assert [root.root_id for root in config.enabled_roots] == [
        "archive-main",
        "university",
        "projects",
    ]
    assert config.find("university") is not None
    assert config.find("missing") is None


def test_duplicate_root_ids_are_rejected() -> None:
    with pytest.raises(InvalidAssistantConfig, match="duplicate storage root id"):
        AssistantConfig.from_mapping(
            _mapping(
                storage={
                    "roots": [
                        {
                            "kind": "local",
                            "id": "university",
                            "label": "A",
                            "path": "/home/a",
                        },
                        {
                            "kind": "vault",
                            "id": "university",
                            "path": "/mnt/a",
                        },
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "usb", "id": "archive-main", "path": "/mnt/e/archive"},
        {"kind": "local", "id": "../usb", "label": "x", "path": "/home/x"},
        {"kind": "local", "id": "university", "label": "   ", "path": "/home/x"},
        {"kind": "local", "id": "university", "label": "x", "path": "relative/path"},
        {"kind": "local", "id": "university", "path": "/home/x"},
        {"kind": "vault", "id": "archive-main"},
        {"kind": "vault", "id": "archive-main", "path": "/mnt/e/archive", "enabled": "yes"},
        {"kind": "vault", "id": "archive-main", "path": "/mnt/e/archive", "owner": "me"},
    ],
)
def test_invalid_root_entries_are_rejected(entry: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(_mapping(storage={"roots": [entry]}))


def test_unsupported_format_version_is_rejected() -> None:
    with pytest.raises(InvalidAssistantConfig, match="format_version"):
        AssistantConfig.from_mapping(_mapping(format_version=2))


@pytest.mark.parametrize("interval", [9, 0, 86401, "300", True])
def test_interval_bounds_are_enforced(interval: object) -> None:
    with pytest.raises(InvalidAssistantConfig, match="interval_seconds"):
        AssistantConfig.from_mapping(_mapping(indexing={"interval_seconds": interval}))


@pytest.mark.parametrize("interval", [10, 300, 86400])
def test_interval_bounds_accept_the_documented_range(interval: int) -> None:
    config = AssistantConfig.from_mapping(_mapping(indexing={"interval_seconds": interval}))

    assert config.indexing.interval_seconds == interval


def test_run_on_startup_must_be_a_boolean() -> None:
    with pytest.raises(InvalidAssistantConfig, match="run_on_startup"):
        AssistantConfig.from_mapping(_mapping(indexing={"run_on_startup": "yes"}))


@pytest.mark.parametrize(
    "document",
    [
        {"format_version": 1, "unknown_section": {}},
        {"format_version": 1, "indexing": {"interval": 300}},
        {"format_version": 1, "storage": {"root": []}},
        {"format_version": 1, "storage": {"roots": [{"kind": "local", "id": "x"}]}},
    ],
)
def test_unknown_keys_and_shapes_are_rejected(document: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(document)


def test_missing_config_file_is_an_empty_configuration(tmp_path: Path) -> None:
    loader = TomlConfigLoader(tmp_path / "absent.toml")

    config = asyncio.run(loader.load())

    assert config == AssistantConfig.empty()
    assert loader.path == tmp_path / "absent.toml"


def test_malformed_toml_is_reported_as_a_config_problem(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml", encoding="utf-8")

    with pytest.raises(InvalidAssistantConfig, match="not valid TOML"):
        asyncio.run(TomlConfigLoader(path).load())


def test_configured_paths_are_expanded_but_not_resolved(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            (
                "format_version = 1",
                "[[storage.roots]]",
                'kind = "local"',
                'id = "university"',
                'label = "University"',
                'path = "~/Documents/University"',
                "",
            )
        ),
        encoding="utf-8",
    )

    config = asyncio.run(TomlConfigLoader(path).load())

    assert config.roots[0].path == str(Path.home() / "Documents" / "University")


def test_offline_paths_are_allowed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            (
                "format_version = 1",
                "[[storage.roots]]",
                'kind = "vault"',
                'id = "archive-main"',
                'path = "/mnt/not-mounted/archive"',
                "",
            )
        ),
        encoding="utf-8",
    )

    config = asyncio.run(TomlConfigLoader(path).load())

    assert config.roots[0].path == "/mnt/not-mounted/archive"


def test_default_config_path_follows_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    assert default_config_path() == (
        tmp_path / "xdg-config" / "growing-assistant" / "config.toml"
    )


def test_reminder_and_scheduler_sections_have_documented_defaults() -> None:
    config = AssistantConfig.from_mapping(_mapping())

    assert config.reminders == ReminderConfig(DEFAULT_DEADLINE_REMINDER_OFFSETS)
    assert config.reminders.deadline_offsets_minutes == (1440, 120)
    assert config.scheduler == SchedulerConfig(
        poll_interval_seconds=DEFAULT_SCHEDULER_POLL_SECONDS,
        replan_debounce_seconds=DEFAULT_REPLAN_DEBOUNCE_SECONDS,
    )


def test_reminder_offsets_are_validated_and_normalised() -> None:
    config = AssistantConfig.from_mapping(
        _mapping(reminders={"deadline_offsets_minutes": [120, 1440]})
    )
    disabled = AssistantConfig.from_mapping(
        _mapping(reminders={"deadline_offsets_minutes": []})
    )

    assert config.reminders.deadline_offsets_minutes == (1440, 120)  # longest first
    assert disabled.reminders.deadline_offsets_minutes == ()


@pytest.mark.parametrize(
    "offsets",
    (
        [-1],
        [1440, 1440],
        [30 * 24 * 60 + 1],
        ["soon"],
    ),
)
def test_invalid_reminder_offsets_are_rejected(offsets: list[object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            _mapping(reminders={"deadline_offsets_minutes": offsets})
        )


@pytest.mark.parametrize(
    ("poll", "debounce"),
    (
        (0, 60),
        (301, 60),
        (15, 4),
        (15, 3601),
        ("15", 60),
        (15, True),
    ),
)
def test_invalid_scheduler_bounds_are_rejected(poll: object, debounce: object) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(
            _mapping(
                scheduler={
                    "poll_interval_seconds": poll,
                    "replan_debounce_seconds": debounce,
                }
            )
        )


@pytest.mark.parametrize(
    "document",
    (
        {"scheduler": {"unknown": 1}},
        {"reminders": {"unknown": 1}},
        {"scheduler": "every minute"},
        {"reminders": "before it is too late"},
        {"scheduler": {"poll_interval_seconds": 15.5}},
    ),
)
def test_unknown_scheduler_shapes_are_rejected(document: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(_mapping(**document))


def test_a_missing_model_section_means_no_model_capability() -> None:
    """Every Phase 1-3 feature keeps working on a host that never configures a model."""
    config = AssistantConfig.from_mapping(_mapping())

    assert config.model is None


def test_a_valid_model_section_is_parsed() -> None:
    config = AssistantConfig.from_mapping(
        _mapping(
            model={
                "provider": "deepseek",
                "model": "deepseek-flash",
                "reasoning_effort": "high",
                "max_output_tokens": 8192,
                "timeout_seconds": 60,
            }
        )
    )

    assert config.model == ModelConfig(
        provider="deepseek",
        model="deepseek-flash",
        reasoning_effort="high",
        max_output_tokens=8192,
        timeout_seconds=60,
    )


def test_model_defaults_are_applied() -> None:
    config = AssistantConfig.from_mapping(_mapping(model={"provider": "deepseek"}))

    assert config.model is not None
    assert config.model.model == DEFAULT_MODEL_NAME
    assert config.model.provider == DEFAULT_MODEL_PROVIDER
    assert config.model.reasoning_effort == DEFAULT_MODEL_REASONING_EFFORT
    assert config.model.max_output_tokens == DEFAULT_MODEL_MAX_OUTPUT_TOKENS
    assert config.model.timeout_seconds == DEFAULT_MODEL_TIMEOUT_SECONDS


@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_every_documented_reasoning_effort_is_accepted(effort: str) -> None:
    config = AssistantConfig.from_mapping(_mapping(model={"reasoning_effort": effort}))

    assert config.model is not None and config.model.reasoning_effort == effort


@pytest.mark.parametrize(
    "section",
    (
        {"provider": "openai"},
        {"model": "   "},
        {"reasoning_effort": "medium"},
        {"reasoning_effort": "minimal"},
        {"max_output_tokens": 63},
        {"max_output_tokens": 32769},
        {"timeout_seconds": 9},
        {"timeout_seconds": 601},
        {"api_key": "sk-should-never-live-here"},
        {"authorization": "Bearer x"},
        {"secret": "x"},
        {"unknown": 1},
    ),
)
def test_invalid_model_sections_are_rejected(section: dict[str, object]) -> None:
    with pytest.raises(InvalidAssistantConfig):
        AssistantConfig.from_mapping(_mapping(model=section))


def test_a_credential_key_is_rejected_by_name() -> None:
    """The strict parser is what keeps secrets out of `config.toml`, not a convention."""
    with pytest.raises(InvalidAssistantConfig) as excinfo:
        AssistantConfig.from_mapping(_mapping(model={"api_key": "sk-live"}))

    assert "api_key" in str(excinfo.value)
    assert "sk-live" not in str(excinfo.value)


def test_other_sections_keep_working_alongside_model_config() -> None:
    config = AssistantConfig.from_mapping(
        _mapping(
            model={"provider": "deepseek"},
            planning={"timezone": "Asia/Shanghai"},
            reminders={"deadline_offsets_minutes": [60]},
            scheduler={"poll_interval_seconds": 30, "replan_debounce_seconds": 120},
        )
    )

    assert config.model is not None
    assert config.planning is not None and config.planning.timezone == "Asia/Shanghai"
    assert config.reminders.deadline_offsets_minutes == (60,)
    assert config.scheduler.replan_debounce_seconds == 120


def test_the_shipped_example_configuration_is_valid() -> None:
    """`docs/examples/config.toml` is documentation that must not rot."""
    import tomllib

    example = Path(__file__).resolve().parents[2] / "docs" / "examples" / "config.toml"
    with example.open("rb") as handle:
        data = tomllib.load(handle)

    config = AssistantConfig.from_mapping(data)

    assert config.model is not None
    assert config.model.provider == DEFAULT_MODEL_PROVIDER
    assert config.planning is not None
    assert config.reminders.deadline_offsets_minutes == (1440, 120)
