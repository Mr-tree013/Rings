"""Host configuration: which storage roots this host manages (ADR-0013).

Configuration is *explicit authorisation*, not discovery: the assistant never guesses that
`/mnt/e` or a directory in `$HOME` is user data. A configured physical path is a host-local
runtime location; the stable identity is still `root_id` (plus a vault's manifest).

Vault labels come from `.pa/vault.toml`, so a label written in the config is accepted but
never treated as authority.

Paths are validated as POSIX-absolute strings here; `~` expansion happens in the config
adapter, which keeps this module free of filesystem access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.domain.errors import InvalidAssistantConfig, InvalidStorageRoot
from assistant.domain.storage import StorageKind, validate_root_id

CONFIG_FORMAT_VERSION: Final[int] = 1
DEFAULT_INDEX_INTERVAL_SECONDS: Final[int] = 300
MIN_INDEX_INTERVAL_SECONDS: Final[int] = 10
MAX_INDEX_INTERVAL_SECONDS: Final[int] = 86400

DEFAULT_SCHEDULER_POLL_SECONDS: Final[int] = 15
MIN_SCHEDULER_POLL_SECONDS: Final[int] = 1
MAX_SCHEDULER_POLL_SECONDS: Final[int] = 300

DEFAULT_REPLAN_DEBOUNCE_SECONDS: Final[int] = 60
MIN_REPLAN_DEBOUNCE_SECONDS: Final[int] = 5
MAX_REPLAN_DEBOUNCE_SECONDS: Final[int] = 3600

DEFAULT_DEADLINE_REMINDER_OFFSETS: Final[tuple[int, ...]] = (1440, 120)
"""A day before and two hours before: this project exists to avoid missed deadlines."""

MAX_DEADLINE_REMINDER_OFFSET_MINUTES: Final[int] = 30 * 24 * 60

_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "format_version",
        "indexing",
        "storage",
        "planning",
        "reminders",
        "scheduler",
        "model",
    }
)
_KNOWN_INDEXING_KEYS = frozenset({"interval_seconds", "run_on_startup"})
_KNOWN_ROOT_KEYS = frozenset({"kind", "id", "label", "path", "enabled"})
_KNOWN_PLANNING_KEYS = frozenset(
    {"timezone", "min_block_minutes", "max_block_minutes", "deadline_buffer_minutes",
     "availability"}
)
_KNOWN_AVAILABILITY_KEYS = frozenset({"days", "start", "end"})
_KNOWN_REMINDER_KEYS = frozenset({"deadline_offsets_minutes"})
_KNOWN_SCHEDULER_KEYS = frozenset({"poll_interval_seconds", "replan_debounce_seconds"})
_KNOWN_MODEL_KEYS = frozenset(
    {"provider", "model", "reasoning_effort", "max_output_tokens", "timeout_seconds"}
)

SUPPORTED_MODEL_PROVIDERS: Final[tuple[str, ...]] = ("deepseek",)
DEFAULT_MODEL_PROVIDER: Final[str] = "deepseek"
DEFAULT_MODEL_NAME: Final[str] = "deepseek-flash"
DEFAULT_MODEL_REASONING_EFFORT: Final[str] = "low"
DEFAULT_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 4096
MIN_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 64
MAX_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 32768
DEFAULT_MODEL_TIMEOUT_SECONDS: Final[int] = 120
MIN_MODEL_TIMEOUT_SECONDS: Final[int] = 10
MAX_MODEL_TIMEOUT_SECONDS: Final[int] = 600
MODEL_REASONING_EFFORTS: Final[tuple[str, ...]] = ("none", "low", "high", "max")


class Weekday(StrEnum):
    """Weekday names accepted in availability rules."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


WEEKDAY_ORDER: Final[tuple[Weekday, ...]] = (
    Weekday.MON,
    Weekday.TUE,
    Weekday.WED,
    Weekday.THU,
    Weekday.FRI,
    Weekday.SAT,
    Weekday.SUN,
)


@dataclass(frozen=True, slots=True)
class WeeklyAvailabilityRule:
    """Local wall-clock availability, e.g. Mon-Fri 09:00-22:00."""

    days: tuple[Weekday, ...]
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not self.days:
            raise InvalidAssistantConfig("an availability rule needs at least one day")
        if len(set(self.days)) != len(self.days):
            raise InvalidAssistantConfig("availability days must not repeat")
        if not 0 <= self.start_minute < self.end_minute <= 24 * 60:
            raise InvalidAssistantConfig(
                "availability must satisfy 0 <= start < end <= 24:00 (overnight windows "
                "are not supported)"
            )


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    """How the deterministic planner may use the week."""

    timezone: str
    min_block_minutes: int = 30
    max_block_minutes: int = 120
    deadline_buffer_minutes: int = 120
    availability: tuple[WeeklyAvailabilityRule, ...] = ()

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidAssistantConfig(
                f"planning.timezone must be a valid IANA timezone, not {self.timezone!r}"
            ) from exc
        if self.min_block_minutes < 1:
            raise InvalidAssistantConfig("planning.min_block_minutes must be at least 1")
        if self.max_block_minutes < self.min_block_minutes:
            raise InvalidAssistantConfig(
                "planning.max_block_minutes must not be smaller than min_block_minutes"
            )
        if self.max_block_minutes > 8 * 60:
            raise InvalidAssistantConfig("planning.max_block_minutes must be at most 480")
        if not 0 <= self.deadline_buffer_minutes <= 7 * 24 * 60:
            raise InvalidAssistantConfig(
                "planning.deadline_buffer_minutes must be between 0 and 10080"
            )


@dataclass(frozen=True, slots=True)
class ReminderConfig:
    """When deadline reminders become due, expressed as offsets before the deadline."""

    deadline_offsets_minutes: tuple[int, ...] = DEFAULT_DEADLINE_REMINDER_OFFSETS

    def __post_init__(self) -> None:
        seen: set[int] = set()
        for offset in self.deadline_offsets_minutes:
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise InvalidAssistantConfig(
                    "reminders.deadline_offsets_minutes must contain integers"
                )
            if offset < 0:
                raise InvalidAssistantConfig(
                    "reminders.deadline_offsets_minutes must not contain negative offsets"
                )
            if offset > MAX_DEADLINE_REMINDER_OFFSET_MINUTES:
                raise InvalidAssistantConfig(
                    "reminders.deadline_offsets_minutes must not exceed 43200 (30 days)"
                )
            if offset in seen:
                raise InvalidAssistantConfig(
                    f"reminders.deadline_offsets_minutes repeats the offset {offset}"
                )
            seen.add(offset)
        normalized = tuple(sorted(seen, reverse=True))
        if normalized != self.deadline_offsets_minutes:
            object.__setattr__(self, "deadline_offsets_minutes", normalized)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """How the daemon scheduler polls, and how long it coalesces replan requests."""

    poll_interval_seconds: int = DEFAULT_SCHEDULER_POLL_SECONDS
    replan_debounce_seconds: int = DEFAULT_REPLAN_DEBOUNCE_SECONDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.poll_interval_seconds, int)
            or isinstance(self.poll_interval_seconds, bool)
        ):
            raise InvalidAssistantConfig("scheduler.poll_interval_seconds must be an integer")
        if not (
            MIN_SCHEDULER_POLL_SECONDS
            <= self.poll_interval_seconds
            <= MAX_SCHEDULER_POLL_SECONDS
        ):
            raise InvalidAssistantConfig(
                "scheduler.poll_interval_seconds must be between "
                f"{MIN_SCHEDULER_POLL_SECONDS} and {MAX_SCHEDULER_POLL_SECONDS}"
            )
        if (
            not isinstance(self.replan_debounce_seconds, int)
            or isinstance(self.replan_debounce_seconds, bool)
        ):
            raise InvalidAssistantConfig("scheduler.replan_debounce_seconds must be an integer")
        if not (
            MIN_REPLAN_DEBOUNCE_SECONDS
            <= self.replan_debounce_seconds
            <= MAX_REPLAN_DEBOUNCE_SECONDS
        ):
            raise InvalidAssistantConfig(
                "scheduler.replan_debounce_seconds must be between "
                f"{MIN_REPLAN_DEBOUNCE_SECONDS} and {MAX_REPLAN_DEBOUNCE_SECONDS}"
            )


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Which provider the model boundary talks to, and how.

    Credentials are deliberately absent: an API key is never configuration, never a repository
    file and never a log line. The composition root reads it from the process environment.
    """

    provider: str = DEFAULT_MODEL_PROVIDER
    model: str = DEFAULT_MODEL_NAME
    reasoning_effort: str = DEFAULT_MODEL_REASONING_EFFORT
    max_output_tokens: int = DEFAULT_MODEL_MAX_OUTPUT_TOKENS
    timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_MODEL_PROVIDERS:
            allowed = ", ".join(SUPPORTED_MODEL_PROVIDERS)
            raise InvalidAssistantConfig(
                f"unknown model.provider {self.provider!r}; supported providers: {allowed}"
            )
        if not self.model.strip():
            raise InvalidAssistantConfig("model.model must not be blank")
        if "\x00" in self.model:
            raise InvalidAssistantConfig("model.model must not contain NUL bytes")
        if self.reasoning_effort not in MODEL_REASONING_EFFORTS:
            allowed = ", ".join(MODEL_REASONING_EFFORTS)
            raise InvalidAssistantConfig(
                f"model.reasoning_effort must be one of: {allowed}"
            )
        if not (
            MIN_MODEL_MAX_OUTPUT_TOKENS
            <= self.max_output_tokens
            <= MAX_MODEL_MAX_OUTPUT_TOKENS
        ):
            raise InvalidAssistantConfig(
                "model.max_output_tokens must be between "
                f"{MIN_MODEL_MAX_OUTPUT_TOKENS} and {MAX_MODEL_MAX_OUTPUT_TOKENS}"
            )
        if not MIN_MODEL_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_MODEL_TIMEOUT_SECONDS:
            raise InvalidAssistantConfig(
                "model.timeout_seconds must be between "
                f"{MIN_MODEL_TIMEOUT_SECONDS} and {MAX_MODEL_TIMEOUT_SECONDS}"
            )


@dataclass(frozen=True, slots=True)
class IndexingConfig:
    """How often the daemon reconciles storage roots."""

    interval_seconds: int = DEFAULT_INDEX_INTERVAL_SECONDS
    run_on_startup: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.interval_seconds, int) or isinstance(self.interval_seconds, bool):
            raise InvalidAssistantConfig("indexing.interval_seconds must be an integer")
        if not MIN_INDEX_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INDEX_INTERVAL_SECONDS:
            raise InvalidAssistantConfig(
                "indexing.interval_seconds must be between "
                f"{MIN_INDEX_INTERVAL_SECONDS} and {MAX_INDEX_INTERVAL_SECONDS}"
            )
        if not isinstance(self.run_on_startup, bool):
            raise InvalidAssistantConfig("indexing.run_on_startup must be a boolean")


@dataclass(frozen=True, slots=True)
class ConfiguredStorageRoot:
    """One explicitly authorised storage root."""

    root_id: str
    kind: StorageKind
    path: str
    label: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        try:
            validate_root_id(self.root_id)
        except InvalidStorageRoot as exc:
            raise InvalidAssistantConfig(str(exc)) from exc
        _validate_absolute_path(self.path)


@dataclass(frozen=True, slots=True)
class ConfiguredLocalRoot(ConfiguredStorageRoot):
    """A local folder; the caller supplies its label."""

    kind: StorageKind = field(default=StorageKind.LOCAL, init=False)

    def __post_init__(self) -> None:
        # Called explicitly: with `slots=True` the dataclass decorator rebuilds the class, and
        # a zero-argument `super()` in the original body would bind to the wrong class.
        ConfiguredStorageRoot.__post_init__(self)
        if not self.label.strip():
            raise InvalidAssistantConfig("a local root needs a non-blank label")


@dataclass(frozen=True, slots=True)
class ConfiguredVaultRoot(ConfiguredStorageRoot):
    """An archive vault; its label comes from the manifest, never from the config."""

    kind: StorageKind = field(default=StorageKind.VAULT, init=False)


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """The whole host configuration."""

    roots: tuple[ConfiguredStorageRoot, ...] = ()
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    planning: PlanningConfig | None = None
    reminders: ReminderConfig = field(default_factory=ReminderConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    model: ModelConfig | None = None
    format_version: int = CONFIG_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != CONFIG_FORMAT_VERSION:
            raise InvalidAssistantConfig(
                f"unsupported config format_version {self.format_version}; "
                f"this build understands {CONFIG_FORMAT_VERSION}"
            )
        seen: set[str] = set()
        for root in self.roots:
            if root.root_id in seen:
                raise InvalidAssistantConfig(f"duplicate storage root id {root.root_id!r}")
            seen.add(root.root_id)

    @classmethod
    def empty(cls) -> AssistantConfig:
        """A configuration with no roots: the daemon runs, it just has nothing to watch."""
        return cls()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AssistantConfig:
        """Parse a config document that has already been read from TOML."""
        unknown = sorted(set(data) - _KNOWN_TOP_LEVEL_KEYS)
        if unknown:
            raise InvalidAssistantConfig(f"unknown config keys: {', '.join(unknown)}")
        format_version = data.get("format_version")
        if not isinstance(format_version, int) or isinstance(format_version, bool):
            raise InvalidAssistantConfig("format_version must be an integer")
        indexing = _parse_indexing(data.get("indexing"))
        roots = _parse_roots(data.get("storage"))
        planning = _parse_planning(data.get("planning"))
        return cls(
            roots=roots,
            indexing=indexing,
            planning=planning,
            reminders=_parse_reminders(data.get("reminders")),
            scheduler=_parse_scheduler(data.get("scheduler")),
            model=_parse_model(data.get("model")),
            format_version=format_version,
        )

    @property
    def enabled_roots(self) -> tuple[ConfiguredStorageRoot, ...]:
        """Roots the user wants reconciled."""
        return tuple(root for root in self.roots if root.enabled)

    def find(self, root_id: str) -> ConfiguredStorageRoot | None:
        """Return the configured root with that id, or `None`."""
        for root in self.roots:
            if root.root_id == root_id:
                return root
        return None


def _validate_absolute_path(path: str) -> None:
    if not path.strip():
        raise InvalidAssistantConfig("a configured root needs a non-blank path")
    if not path.startswith("/"):
        raise InvalidAssistantConfig(f"configured root path must be absolute: {path!r}")
    if "\x00" in path:
        raise InvalidAssistantConfig("configured root path must not contain NUL bytes")


def _parse_indexing(value: object) -> IndexingConfig:
    if value is None:
        return IndexingConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[indexing] must be a table")
    unknown = sorted(set(value) - _KNOWN_INDEXING_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [indexing] keys: {', '.join(unknown)}")
    interval = value.get("interval_seconds", DEFAULT_INDEX_INTERVAL_SECONDS)
    run_on_startup = value.get("run_on_startup", True)
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise InvalidAssistantConfig("indexing.interval_seconds must be an integer")
    if not isinstance(run_on_startup, bool):
        raise InvalidAssistantConfig("indexing.run_on_startup must be a boolean")
    return IndexingConfig(interval_seconds=interval, run_on_startup=run_on_startup)


def _parse_roots(value: object) -> tuple[ConfiguredStorageRoot, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[storage] must be a table")
    unknown = sorted(set(value) - {"roots"})
    if unknown:
        raise InvalidAssistantConfig(f"unknown [storage] keys: {', '.join(unknown)}")
    entries = value.get("roots", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise InvalidAssistantConfig("[[storage.roots]] must be a list of tables")
    roots: list[ConfiguredStorageRoot] = []
    for entry in entries:
        roots.append(_parse_root(entry))
    return tuple(roots)


def _parse_root(entry: object) -> ConfiguredStorageRoot:
    if not isinstance(entry, Mapping):
        raise InvalidAssistantConfig("each [[storage.roots]] entry must be a table")
    unknown = sorted(set(entry) - _KNOWN_ROOT_KEYS)
    if unknown:
        raise InvalidAssistantConfig(
            f"unknown storage root keys: {', '.join(unknown)}"
        )
    kind_value = entry.get("kind")
    if not isinstance(kind_value, str):
        raise InvalidAssistantConfig("a storage root needs a string kind")
    try:
        kind = StorageKind(kind_value)
    except ValueError as exc:
        raise InvalidAssistantConfig(f"unknown storage root kind {kind_value!r}") from exc
    root_id = entry.get("id")
    if not isinstance(root_id, str):
        raise InvalidAssistantConfig("a storage root needs a string id")
    path = entry.get("path")
    if not isinstance(path, str):
        raise InvalidAssistantConfig("a storage root needs a string path")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidAssistantConfig("storage root enabled must be a boolean")
    label = entry.get("label", "")
    if not isinstance(label, str):
        raise InvalidAssistantConfig("storage root label must be a string")
    if kind is StorageKind.LOCAL:
        return ConfiguredLocalRoot(
            root_id=root_id, path=path, label=label, enabled=enabled
        )
    return ConfiguredVaultRoot(root_id=root_id, path=path, label="", enabled=enabled)


def _parse_planning(value: object) -> PlanningConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[planning] must be a table")
    unknown = sorted(set(value) - _KNOWN_PLANNING_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [planning] keys: {', '.join(unknown)}")
    timezone = value.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise InvalidAssistantConfig(
            "planning.timezone is required (IANA name, e.g. Asia/Shanghai)"
        )
    availability_value = value.get("availability", ())
    if not isinstance(availability_value, Sequence) or isinstance(availability_value, (str, bytes)):
        raise InvalidAssistantConfig("[[planning.availability]] must be a list of tables")
    rules = tuple(_parse_availability(entry) for entry in availability_value)
    return PlanningConfig(
        timezone=timezone.strip(),
        min_block_minutes=_int_option(value, "min_block_minutes", 30),
        max_block_minutes=_int_option(value, "max_block_minutes", 120),
        deadline_buffer_minutes=_int_option(value, "deadline_buffer_minutes", 120),
        availability=rules,
    )


def _int_option(value: Mapping[str, object], key: str, default: int) -> int:
    raw = value.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise InvalidAssistantConfig(f"planning.{key} must be an integer")
    return raw


def _parse_reminders(value: object) -> ReminderConfig:
    if value is None:
        return ReminderConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[reminders] must be a table")
    unknown = sorted(set(value) - _KNOWN_REMINDER_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [reminders] keys: {', '.join(unknown)}")
    offsets = value.get("deadline_offsets_minutes")
    if offsets is None:
        return ReminderConfig()
    if not isinstance(offsets, Sequence) or isinstance(offsets, (str, bytes)):
        raise InvalidAssistantConfig(
            "reminders.deadline_offsets_minutes must be a list of minutes"
        )
    return ReminderConfig(deadline_offsets_minutes=tuple(offsets))


def _parse_scheduler(value: object) -> SchedulerConfig:
    if value is None:
        return SchedulerConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[scheduler] must be a table")
    unknown = sorted(set(value) - _KNOWN_SCHEDULER_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [scheduler] keys: {', '.join(unknown)}")
    return SchedulerConfig(
        poll_interval_seconds=_scheduler_int(
            value, "poll_interval_seconds", DEFAULT_SCHEDULER_POLL_SECONDS
        ),
        replan_debounce_seconds=_scheduler_int(
            value, "replan_debounce_seconds", DEFAULT_REPLAN_DEBOUNCE_SECONDS
        ),
    )


def _scheduler_int(value: Mapping[str, object], key: str, default: int) -> int:
    raw = value.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise InvalidAssistantConfig(f"scheduler.{key} must be an integer")
    return raw


def _parse_model(value: object) -> ModelConfig | None:
    """Parse `[model]`. Credentials are rejected here on purpose: they are never config."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[model] must be a table")
    unknown = sorted(set(value) - _KNOWN_MODEL_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [model] keys: {', '.join(unknown)}")
    provider = value.get("provider", DEFAULT_MODEL_PROVIDER)
    if not isinstance(provider, str):
        raise InvalidAssistantConfig("model.provider must be a string")
    model_name = value.get("model", DEFAULT_MODEL_NAME)
    if not isinstance(model_name, str):
        raise InvalidAssistantConfig("model.model must be a string")
    reasoning_effort = value.get("reasoning_effort", DEFAULT_MODEL_REASONING_EFFORT)
    if not isinstance(reasoning_effort, str):
        raise InvalidAssistantConfig("model.reasoning_effort must be a string")
    return ModelConfig(
        provider=provider.strip().lower(),
        model=model_name.strip(),
        reasoning_effort=reasoning_effort.strip().lower(),
        max_output_tokens=_model_int(
            value, "max_output_tokens", DEFAULT_MODEL_MAX_OUTPUT_TOKENS
        ),
        timeout_seconds=_model_int(value, "timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS),
    )


def _model_int(value: Mapping[str, object], key: str, default: int) -> int:
    raw = value.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise InvalidAssistantConfig(f"model.{key} must be an integer")
    return raw


def _parse_availability(entry: object) -> WeeklyAvailabilityRule:
    if not isinstance(entry, Mapping):
        raise InvalidAssistantConfig("each [[planning.availability]] entry must be a table")
    unknown = sorted(set(entry) - _KNOWN_AVAILABILITY_KEYS)
    if unknown:
        raise InvalidAssistantConfig(
            f"unknown availability keys: {', '.join(unknown)}"
        )
    days_value = entry.get("days")
    if not isinstance(days_value, Sequence) or isinstance(days_value, (str, bytes)):
        raise InvalidAssistantConfig("availability.days must be a list of weekday names")
    days: list[Weekday] = []
    for raw in days_value:
        if not isinstance(raw, str):
            raise InvalidAssistantConfig("availability.days must contain strings")
        try:
            day = Weekday(raw.strip().lower())
        except ValueError as exc:
            allowed = ", ".join(member.value for member in WEEKDAY_ORDER)
            raise InvalidAssistantConfig(
                f"unknown weekday {raw!r}; expected one of: {allowed}"
            ) from exc
        if day in days:
            raise InvalidAssistantConfig(f"weekday {day.value!r} is listed twice")
        days.append(day)
    ordered = tuple(day for day in WEEKDAY_ORDER if day in days)
    return WeeklyAvailabilityRule(
        days=ordered,
        start_minute=_parse_clock(entry.get("start"), "start"),
        end_minute=_parse_clock(entry.get("end"), "end"),
    )


def _parse_clock(value: object, field_name: str) -> int:
    if not isinstance(value, str):
        raise InvalidAssistantConfig(f"availability.{field_name} must be HH:MM")
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise InvalidAssistantConfig(f"availability.{field_name} must be HH:MM, not {text!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 24 or minutes > 59 or (hours == 24 and minutes != 0):
        raise InvalidAssistantConfig(f"availability.{field_name} is not a valid time: {text!r}")
    return hours * 60 + minutes


__all__ = [
    "CONFIG_FORMAT_VERSION",
    "DEFAULT_DEADLINE_REMINDER_OFFSETS",
    "DEFAULT_INDEX_INTERVAL_SECONDS",
    "DEFAULT_MODEL_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_MODEL_PROVIDER",
    "DEFAULT_MODEL_REASONING_EFFORT",
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "DEFAULT_REPLAN_DEBOUNCE_SECONDS",
    "DEFAULT_SCHEDULER_POLL_SECONDS",
    "MAX_DEADLINE_REMINDER_OFFSET_MINUTES",
    "MAX_INDEX_INTERVAL_SECONDS",
    "MAX_MODEL_MAX_OUTPUT_TOKENS",
    "MAX_MODEL_TIMEOUT_SECONDS",
    "MAX_REPLAN_DEBOUNCE_SECONDS",
    "MAX_SCHEDULER_POLL_SECONDS",
    "MIN_INDEX_INTERVAL_SECONDS",
    "MIN_MODEL_MAX_OUTPUT_TOKENS",
    "MIN_MODEL_TIMEOUT_SECONDS",
    "MIN_REPLAN_DEBOUNCE_SECONDS",
    "MIN_SCHEDULER_POLL_SECONDS",
    "MODEL_REASONING_EFFORTS",
    "SUPPORTED_MODEL_PROVIDERS",
    "WEEKDAY_ORDER",
    "AssistantConfig",
    "ConfiguredLocalRoot",
    "ConfiguredStorageRoot",
    "ConfiguredVaultRoot",
    "IndexingConfig",
    "ModelConfig",
    "PlanningConfig",
    "ReminderConfig",
    "SchedulerConfig",
    "Weekday",
    "WeeklyAvailabilityRule",
]
