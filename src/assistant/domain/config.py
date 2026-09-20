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

from assistant.domain.errors import (
    InvalidAssistantConfig,
    InvalidMailMessage,
    InvalidStorageRoot,
)
from assistant.domain.mail import validate_account_id
from assistant.domain.mail_draft import mailbox_address
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
        "mail",
        "ehall",
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
_KNOWN_MAIL_KEYS = frozenset(
    {
        "poll_interval_seconds",
        "max_messages_per_poll",
        "initial_fetch_limit",
        "reconciliation_window",
        "max_message_bytes",
        "timeout_seconds",
        "accounts",
    }
)
_KNOWN_MAIL_ACCOUNT_KEYS = frozenset(
    {
        "id",
        "host",
        "port",
        "username",
        "mailbox",
        "enabled",
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "smtp_username",
        "from_address",
        "sent_mailbox",
    }
)

DEFAULT_EHALL_ENABLED: Final[bool] = False
DEFAULT_EHALL_TIMEOUT_SECONDS: Final[int] = 30
MIN_EHALL_TIMEOUT_SECONDS: Final[int] = 10
MAX_EHALL_TIMEOUT_SECONDS: Final[int] = 120
_KNOWN_EHALL_KEYS = frozenset({"enabled", "timeout_seconds"})

SMTP_SECURITY_MODES: Final[tuple[str, ...]] = ("starttls", "ssl")
"""The only two ways this project will speak SMTP. There is no plaintext mode and no switch to
skip certificate verification."""

DEFAULT_SMTP_PORT: Final[int] = 587
DEFAULT_SMTP_SECURITY: Final[str] = "starttls"
DEFAULT_SENT_MAILBOX: Final[str] = "Sent"

DEFAULT_MAIL_POLL_SECONDS: Final[int] = 60
MIN_MAIL_POLL_SECONDS: Final[int] = 10
MAX_MAIL_POLL_SECONDS: Final[int] = 3600

DEFAULT_MAIL_MAX_MESSAGES_PER_POLL: Final[int] = 100
DEFAULT_MAIL_INITIAL_FETCH_LIMIT: Final[int] = 500
DEFAULT_MAIL_RECONCILIATION_WINDOW: Final[int] = 500
MAX_MAIL_BATCH: Final[int] = 10000

DEFAULT_MAIL_MAX_MESSAGE_BYTES: Final[int] = 25 * 1024 * 1024
MIN_MAIL_MAX_MESSAGE_BYTES: Final[int] = 1024 * 1024
MAX_MAIL_MAX_MESSAGE_BYTES: Final[int] = 100 * 1024 * 1024

DEFAULT_MAIL_TIMEOUT_SECONDS: Final[int] = 30
MIN_MAIL_TIMEOUT_SECONDS: Final[int] = 10
MAX_MAIL_TIMEOUT_SECONDS: Final[int] = 300

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
class MailAccountConfig:
    """One explicitly configured mail account: inbound always, outbound only when asked for.

    There is no credential field on purpose: a password never lives in configuration, and the
    identity is a stable local id, never the address or the host.

    The SMTP fields are optional as a group. An account with none of them is receive-only, which is
    a perfectly good configuration; an account with any of them must carry a complete, TLS-only
    outbound configuration, because a half-configured sender is a trap rather than a convenience.
    """

    id: str
    host: str
    username: str
    mailbox: str
    port: int = 993
    enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: str | None = None
    smtp_username: str | None = None
    from_address: str | None = None
    sent_mailbox: str | None = None

    def __post_init__(self) -> None:
        try:
            validate_account_id(self.id)
        except InvalidMailMessage as exc:
            raise InvalidAssistantConfig(str(exc)) from exc
        for value, field_name in (
            (self.host, "host"),
            (self.username, "username"),
            (self.mailbox, "mailbox"),
        ):
            if not value.strip():
                raise InvalidAssistantConfig(f"mail account {field_name} must not be blank")
            if "\x00" in value:
                raise InvalidAssistantConfig(
                    f"mail account {field_name} must not contain NUL bytes"
                )
        if not 1 <= self.port <= 65535:
            raise InvalidAssistantConfig("mail account port must be between 1 and 65535")
        if not isinstance(self.enabled, bool):
            raise InvalidAssistantConfig("mail account enabled must be a boolean")
        self._validate_smtp()

    def _validate_smtp(self) -> None:
        """Accept a complete TLS-only outbound block, or no outbound block at all."""
        provided = {
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_security": self.smtp_security,
            "smtp_username": self.smtp_username,
            "from_address": self.from_address,
            "sent_mailbox": self.sent_mailbox,
        }
        if all(value is None for value in provided.values()):
            return
        for key in ("smtp_host", "smtp_username", "from_address"):
            value = provided[key]
            if not isinstance(value, str) or not value.strip():
                raise InvalidAssistantConfig(
                    f"mail account {self.id!r} configures SMTP, so {key} is required"
                )
            if "\x00" in value:
                raise InvalidAssistantConfig(
                    f"mail account {key} must not contain NUL bytes"
                )
            provided[key] = value.strip()
        port = provided["smtp_port"]
        if port is not None and (
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
        ):
            raise InvalidAssistantConfig("mail account smtp_port must be between 1 and 65535")
        security = provided["smtp_security"]
        if security is not None and (
            not isinstance(security, str) or security.strip() not in SMTP_SECURITY_MODES
        ):
            allowed = ", ".join(SMTP_SECURITY_MODES)
            raise InvalidAssistantConfig(
                f"mail account smtp_security must be one of: {allowed}"
            )
        sent_mailbox = provided["sent_mailbox"]
        if sent_mailbox is not None and (
            not isinstance(sent_mailbox, str) or not sent_mailbox.strip()
        ):
            raise InvalidAssistantConfig("mail account sent_mailbox must not be blank")
        sender = mailbox_address(str(provided["from_address"]))
        if sender != str(provided["from_address"]):
            raise InvalidAssistantConfig(
                f"mail account from_address must be a bare mailbox, not "
                f"{provided['from_address']!r}"
            )
        object.__setattr__(self, "smtp_host", str(provided["smtp_host"]))
        object.__setattr__(self, "smtp_username", str(provided["smtp_username"]))
        object.__setattr__(self, "from_address", str(provided["from_address"]))
        object.__setattr__(
            self,
            "smtp_port",
            DEFAULT_SMTP_PORT if port is None else int(port),
        )
        object.__setattr__(
            self,
            "smtp_security",
            DEFAULT_SMTP_SECURITY if security is None else str(security).strip(),
        )
        object.__setattr__(
            self,
            "sent_mailbox",
            DEFAULT_SENT_MAILBOX if sent_mailbox is None else str(sent_mailbox).strip(),
        )

    @property
    def smtp_configured(self) -> bool:
        """Whether this account can send at all."""
        return self.smtp_host is not None


@dataclass(frozen=True, slots=True)
class MailConfig:
    """How inbound mail is synchronized. A host with no accounts polls nothing."""

    accounts: tuple[MailAccountConfig, ...] = ()
    poll_interval_seconds: int = DEFAULT_MAIL_POLL_SECONDS
    max_messages_per_poll: int = DEFAULT_MAIL_MAX_MESSAGES_PER_POLL
    initial_fetch_limit: int = DEFAULT_MAIL_INITIAL_FETCH_LIMIT
    reconciliation_window: int = DEFAULT_MAIL_RECONCILIATION_WINDOW
    max_message_bytes: int = DEFAULT_MAIL_MAX_MESSAGE_BYTES
    timeout_seconds: int = DEFAULT_MAIL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for account in self.accounts:
            if account.id in seen:
                raise InvalidAssistantConfig(f"duplicate mail account id {account.id!r}")
            seen.add(account.id)
        _validate_range(
            "mail.poll_interval_seconds",
            self.poll_interval_seconds,
            MIN_MAIL_POLL_SECONDS,
            MAX_MAIL_POLL_SECONDS,
        )
        _validate_range(
            "mail.max_messages_per_poll", self.max_messages_per_poll, 1, 1000
        )
        _validate_range(
            "mail.initial_fetch_limit", self.initial_fetch_limit, 1, MAX_MAIL_BATCH
        )
        _validate_range(
            "mail.reconciliation_window",
            self.reconciliation_window,
            1,
            MAX_MAIL_BATCH,
        )
        _validate_range(
            "mail.max_message_bytes",
            self.max_message_bytes,
            MIN_MAIL_MAX_MESSAGE_BYTES,
            MAX_MAIL_MAX_MESSAGE_BYTES,
        )
        _validate_range(
            "mail.timeout_seconds",
            self.timeout_seconds,
            MIN_MAIL_TIMEOUT_SECONDS,
            MAX_MAIL_TIMEOUT_SECONDS,
        )

    @property
    def enabled_accounts(self) -> tuple[MailAccountConfig, ...]:
        """The accounts the daemon should synchronize."""
        return tuple(account for account in self.accounts if account.enabled)

    def find(self, account_id: str) -> MailAccountConfig | None:
        """Return the configured account with that id, or `None`."""
        for account in self.accounts:
            if account.id == account_id:
                return account
        return None


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidAssistantConfig(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidAssistantConfig(f"{name} must be between {minimum} and {maximum}")


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
class EHallConfig:
    """Whether this host may drive the whitelisted eHall pipeline at all.

    There is deliberately nothing else to configure. The service is fixed by the pipeline, the
    browser runs headed so the user can see what it does, and the login is manual — so there is no
    URL, no selector, no origin list and no credential to put here.
    """

    enabled: bool = DEFAULT_EHALL_ENABLED
    timeout_seconds: int = DEFAULT_EHALL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise InvalidAssistantConfig("ehall.enabled must be a boolean")
        _validate_range(
            "ehall.timeout_seconds",
            self.timeout_seconds,
            MIN_EHALL_TIMEOUT_SECONDS,
            MAX_EHALL_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """The whole host configuration."""

    roots: tuple[ConfiguredStorageRoot, ...] = ()
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    planning: PlanningConfig | None = None
    reminders: ReminderConfig = field(default_factory=ReminderConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    model: ModelConfig | None = None
    mail: MailConfig = field(default_factory=MailConfig)
    ehall: EHallConfig = field(default_factory=lambda: EHallConfig())
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
            mail=_parse_mail(data.get("mail")),
            ehall=_parse_ehall(data.get("ehall")),
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


def _parse_ehall(value: object) -> EHallConfig:
    """Parse `[ehall]`. Credentials, URLs and selectors are rejected here on purpose."""
    if value is None:
        return EHallConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[ehall] must be a table")
    unknown = sorted(set(value) - _KNOWN_EHALL_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [ehall] keys: {', '.join(unknown)}")
    enabled = value.get("enabled", DEFAULT_EHALL_ENABLED)
    if not isinstance(enabled, bool):
        raise InvalidAssistantConfig("ehall.enabled must be a boolean")
    timeout = value.get("timeout_seconds", DEFAULT_EHALL_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise InvalidAssistantConfig("ehall.timeout_seconds must be an integer")
    return EHallConfig(enabled=enabled, timeout_seconds=timeout)


def _parse_mail(value: object) -> MailConfig:
    """Parse `[mail]`. Credentials are rejected here on purpose: they are never config."""
    if value is None:
        return MailConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[mail] must be a table")
    unknown = sorted(set(value) - _KNOWN_MAIL_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [mail] keys: {', '.join(unknown)}")
    entries = value.get("accounts", ())
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise InvalidAssistantConfig("[[mail.accounts]] must be a list of tables")
    return MailConfig(
        accounts=tuple(_parse_mail_account(entry) for entry in entries),
        poll_interval_seconds=_mail_int(
            value, "poll_interval_seconds", DEFAULT_MAIL_POLL_SECONDS
        ),
        max_messages_per_poll=_mail_int(
            value, "max_messages_per_poll", DEFAULT_MAIL_MAX_MESSAGES_PER_POLL
        ),
        initial_fetch_limit=_mail_int(
            value, "initial_fetch_limit", DEFAULT_MAIL_INITIAL_FETCH_LIMIT
        ),
        reconciliation_window=_mail_int(
            value, "reconciliation_window", DEFAULT_MAIL_RECONCILIATION_WINDOW
        ),
        max_message_bytes=_mail_int(
            value, "max_message_bytes", DEFAULT_MAIL_MAX_MESSAGE_BYTES
        ),
        timeout_seconds=_mail_int(value, "timeout_seconds", DEFAULT_MAIL_TIMEOUT_SECONDS),
    )


def _mail_int(value: Mapping[str, object], key: str, default: int) -> int:
    raw = value.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise InvalidAssistantConfig(f"mail.{key} must be an integer")
    return raw


def _parse_mail_account(entry: object) -> MailAccountConfig:
    if not isinstance(entry, Mapping):
        raise InvalidAssistantConfig("each [[mail.accounts]] entry must be a table")
    unknown = sorted(set(entry) - _KNOWN_MAIL_ACCOUNT_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown mail account keys: {', '.join(unknown)}")
    values: dict[str, object] = {}
    for key in ("id", "host", "username", "mailbox"):
        raw = entry.get(key)
        if not isinstance(raw, str):
            raise InvalidAssistantConfig(f"a mail account needs a string {key}")
        values[key] = raw
    port = entry.get("port", 993)
    if not isinstance(port, int) or isinstance(port, bool):
        raise InvalidAssistantConfig("mail account port must be an integer")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidAssistantConfig("mail account enabled must be a boolean")
    smtp = _parse_mail_account_smtp(entry)
    return MailAccountConfig(
        id=str(values["id"]).strip(),
        host=str(values["host"]).strip(),
        username=str(values["username"]).strip(),
        mailbox=str(values["mailbox"]).strip(),
        port=port,
        enabled=enabled,
        smtp_host=smtp.host,
        smtp_port=smtp.port,
        smtp_security=smtp.security,
        smtp_username=smtp.username,
        from_address=smtp.from_address,
        sent_mailbox=smtp.sent_mailbox,
    )


@dataclass(frozen=True, slots=True)
class _SmtpAccountFields:
    """The optional outbound block of one account, exactly as the user wrote it."""

    host: str | None = None
    port: int | None = None
    security: str | None = None
    username: str | None = None
    from_address: str | None = None
    sent_mailbox: str | None = None


def _parse_mail_account_smtp(entry: Mapping[str, object]) -> _SmtpAccountFields:
    """Parse the optional outbound block. A credential key is never accepted here."""
    for forbidden in ("password", "smtp_password", "secret", "api_key", "verify_tls"):
        if forbidden in entry:
            raise InvalidAssistantConfig(
                f"mail account key {forbidden!r} is not configuration: credentials come from the "
                "environment and certificate verification is never optional"
            )
    text_values: dict[str, str | None] = {}
    for key in ("smtp_host", "smtp_username", "from_address", "sent_mailbox"):
        raw = entry.get(key)
        if raw is None:
            text_values[key] = None
            continue
        if not isinstance(raw, str):
            raise InvalidAssistantConfig(f"mail account {key} must be a string")
        text_values[key] = raw.strip()
    smtp_port = entry.get("smtp_port")
    if smtp_port is not None and (
        not isinstance(smtp_port, int) or isinstance(smtp_port, bool)
    ):
        raise InvalidAssistantConfig("mail account smtp_port must be an integer")
    security = entry.get("smtp_security")
    if security is not None and not isinstance(security, str):
        raise InvalidAssistantConfig("mail account smtp_security must be a string")
    return _SmtpAccountFields(
        host=text_values["smtp_host"],
        port=smtp_port,
        security=None if security is None else security.strip(),
        username=text_values["smtp_username"],
        from_address=text_values["from_address"],
        sent_mailbox=text_values["sent_mailbox"],
    )


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
    "DEFAULT_MAIL_INITIAL_FETCH_LIMIT",
    "DEFAULT_MAIL_MAX_MESSAGES_PER_POLL",
    "DEFAULT_MAIL_MAX_MESSAGE_BYTES",
    "DEFAULT_MAIL_POLL_SECONDS",
    "DEFAULT_MAIL_RECONCILIATION_WINDOW",
    "DEFAULT_MAIL_TIMEOUT_SECONDS",
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
    "MailAccountConfig",
    "MailConfig",
    "ModelConfig",
    "PlanningConfig",
    "ReminderConfig",
    "SchedulerConfig",
    "Weekday",
    "WeeklyAvailabilityRule",
]
