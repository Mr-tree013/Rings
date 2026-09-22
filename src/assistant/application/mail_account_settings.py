"""Mail account settings: typed metadata, safe views and honest diagnostics (ADR-0043).

```text
list_safe()            configuration ──► MailAccountSettings (never a secret)
create/update/enable   typed draft ──► validate ──► managed overlay (atomic, complete list)
test_imap/test_smtp    provider ──► MailProbeReport (connect, TLS, maybe auth, close)
```

Three rules shape everything below.

**The secret never reaches this layer at all.** The service learns *whether* a credential exists
and what environment variable names it; the connection probes look the value up themselves, inside
the adapter package that already owns that lookup. There is no method here that returns one, no DTO
field that holds one, and no parameter through which one could arrive: the architecture test that
bans the identifier outright still passes, and it passes for the right reason. A reference name is
safe to show precisely because it is derived from the account id rather than chosen by anyone.

**A write is atomic and complete.** The overlay is written with the whole effective account list, so
the first edit imports whatever was already in `config.toml` instead of replacing it. The user's own
file is never touched.

**The truth about restarting is spoken.** Reloading accounts would mean rebuilding the sync service
and the registered executors, which the daemon composes once and holds for the process lifetime.
This service therefore reports `restart_required = True` and says so, rather than pretending the
change is live — which would be a promise the running process cannot keep.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from assistant.domain.config import (
    SMTP_SECURITY_MODES,
    MailAccountConfig,
    parse_mail_account,
)
from assistant.domain.errors import (
    InvalidAssistantConfig,
    InvalidMailSettings,
    MailAccountSettingsNotFound,
)
from assistant.domain.mail_settings import MailProbeReport
from assistant.ports.clock import Clock
from assistant.ports.mail_connection import MailConnectionProbe, MailCredentialSource
from assistant.ports.mail_settings_store import MailSettingsStore

ENVIRONMENT_SOURCE_KIND = "environment"
"""The one place a mail credential comes from today (ADR-0043 §15)."""

RESTART_NOTE = "配置已保存。重启 assistantd 后生效。"
"""What every write says, because no write is live until the process is restarted."""


@dataclass(frozen=True, slots=True)
class MailCredentialStatus:
    """Whether a credential exists, where it would come from, and what names it."""

    configured: bool
    source_kind: str
    reference: str

    def to_payload(self) -> dict[str, object]:
        """The safe representation. There is no field here that could hold a value."""
        return {
            "configured": self.configured,
            "source_kind": self.source_kind,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class MailEndpointSettings:
    """One side of an account: where it is, and whether it is complete enough to use."""

    host: str | None
    port: int | None
    security: str | None
    configured: bool

    def to_payload(self) -> dict[str, object]:
        """The safe representation."""
        return {
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "configured": self.configured,
        }


@dataclass(frozen=True, slots=True)
class MailAccountSettings:
    """Everything a browser may know about one account, and nothing more."""

    id: str
    label: str
    enabled: bool
    username: str
    mailbox: str
    from_address: str | None
    imap: MailEndpointSettings
    smtp: MailEndpointSettings
    credentials: MailCredentialStatus

    @property
    def receive_ready(self) -> bool:
        """Whether this host could receive mail for this account right now."""
        return self.enabled and self.imap.configured and self.credentials.configured

    @property
    def send_ready(self) -> bool:
        """Whether this host could deliver an approved message right now."""
        return (
            self.enabled
            and self.smtp.configured
            and self.from_address is not None
            and self.credentials.configured
        )

    def to_payload(self) -> dict[str, object]:
        """The JSON shape the settings page receives."""
        return {
            "id": self.id,
            "label": self.label,
            "enabled": self.enabled,
            "username": self.username,
            "mailbox": self.mailbox,
            "from_address": self.from_address,
            "imap": self.imap.to_payload(),
            "smtp": self.smtp.to_payload(),
            "credential": self.credentials.to_payload(),
            "receive_ready": self.receive_ready,
            "send_ready": self.send_ready,
        }


@dataclass(frozen=True, slots=True)
class MailAccountDraft:
    """The typed fields a user may set. A label is local display text, never an identity."""

    id: str
    host: str
    username: str
    mailbox: str = "INBOX"
    label: str = ""
    port: int = 993
    enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: str | None = None
    smtp_username: str | None = None
    from_address: str | None = None
    sent_mailbox: str | None = None


@dataclass(frozen=True, slots=True)
class MailSettingsWriteResult:
    """What one write did, and what the user has to do about it."""

    accounts: tuple[MailAccountSettings, ...]
    restart_required: bool
    detail: str

    @property
    def applied_immediately(self) -> bool:
        """Whether the running process is already using the new configuration. It is not."""
        return not self.restart_required

    def to_payload(self) -> dict[str, object]:
        """The JSON shape the settings page receives."""
        return {
            "accounts": [account.to_payload() for account in self.accounts],
            "restart_required": self.restart_required,
            "applied_immediately": self.applied_immediately,
            "detail": self.detail,
        }


class MailAccountSettingsService:
    """Read safe account settings, write typed metadata, and test connectivity."""

    def __init__(
        self,
        *,
        store: MailSettingsStore,
        accounts: Sequence[MailAccountConfig],
        credentials: MailCredentialSource,
        clock: Clock,
        probe: MailConnectionProbe | None = None,
    ) -> None:
        self._store = store
        self._accounts = tuple(accounts)
        self._credentials = credentials
        self._clock = clock
        self._probe = probe

    # ------------------------------------------------------------------ reading

    async def list_safe(self) -> tuple[MailAccountSettings, ...]:
        """Every configured account, in configuration order, without a single secret."""
        return tuple(self._safe(account) for account in self._accounts)

    async def get(self, account_id: str) -> MailAccountSettings:
        """One account's safe view.

        Raises:
            MailAccountSettingsNotFound: no configured account has that id.
        """
        return self._safe(self._require(account_id))

    # ------------------------------------------------------------------ writing

    async def create(self, draft: MailAccountDraft) -> MailSettingsWriteResult:
        """Add one account.

        Raises:
            InvalidMailSettings: the fields are not a usable account, or the id already exists.
        """
        if any(account.id == draft.id for account in self._accounts):
            raise InvalidMailSettings(f"已经有一个叫 {draft.id!r} 的邮箱账号了。")
        return await self._write((*self._accounts, _to_config(draft)))

    async def update(self, account_id: str, draft: MailAccountDraft) -> MailSettingsWriteResult:
        """Replace one account's metadata, keeping its place in the list.

        Raises:
            MailAccountSettingsNotFound: no such account.
            InvalidMailSettings: the fields are not a usable account.
        """
        if draft.id != account_id:
            raise InvalidMailSettings("邮箱账号的 id 不能通过编辑修改。")
        self._require(account_id)
        replaced = tuple(
            _to_config(draft) if account.id == account_id else account
            for account in self._accounts
        )
        return await self._write(replaced)

    async def set_enabled(
        self, account_id: str, *, enabled: bool
    ) -> MailSettingsWriteResult:
        """Switch one account on or off without touching any other field.

        Raises:
            MailAccountSettingsNotFound: no such account.
        """
        account = self._require(account_id)
        disabled = dataclass_replace(account, enabled=enabled)
        return await self._write(
            tuple(
                disabled if candidate.id == account_id else candidate
                for candidate in self._accounts
            )
        )

    async def _write(self, accounts: tuple[MailAccountConfig, ...]) -> MailSettingsWriteResult:
        """Validate, then write the complete effective list atomically."""
        for account in accounts:
            _validated(account)
        await self._store.save_accounts(accounts)
        self._accounts = accounts
        return MailSettingsWriteResult(
            accounts=await self.list_safe(),
            restart_required=True,
            detail=RESTART_NOTE,
        )

    # ------------------------------------------------------------------ testing

    async def test_imap(self, account_id: str) -> MailProbeReport:
        """Run the read-only inbound diagnostic for one account.

        Raises:
            MailAccountSettingsNotFound: no such account.
            InvalidMailSettings: this host has no connectivity probe installed.
        """
        account = self._require(account_id)
        return await self._require_probe().probe_imap(account)

    async def test_smtp(self, account_id: str) -> MailProbeReport:
        """Run the outbound diagnostic: connect, TLS, maybe authenticate, NOOP, close.

        It cannot send: there is no message, and the probe has no `DATA`. Nothing here creates an
        `ActionRequest`, an `Approval` or an `ExecutionRun`.

        Raises:
            MailAccountSettingsNotFound: no such account.
            InvalidMailSettings: this host has no connectivity probe installed.
        """
        account = self._require(account_id)
        return await self._require_probe().probe_smtp(account)

    # ------------------------------------------------------------------ internals

    def _require(self, account_id: str) -> MailAccountConfig:
        for account in self._accounts:
            if account.id == account_id:
                return account
        raise MailAccountSettingsNotFound(account_id)

    def _require_probe(self) -> MailConnectionProbe:
        probe = self._probe
        if probe is None:  # pragma: no cover - the composition root always installs one
            raise InvalidMailSettings("这台主机还不能测试邮箱连接。")
        return probe

    def _safe(self, account: MailAccountConfig) -> MailAccountSettings:
        """One account as the browser may see it. The credential value is never read here."""
        has_credential = self._credentials.has_inbound(account.id)
        return MailAccountSettings(
            id=account.id,
            label=account.username,
            enabled=account.enabled,
            username=account.username,
            mailbox=account.mailbox,
            from_address=account.from_address,
            imap=MailEndpointSettings(
                host=account.host,
                port=account.port,
                security="tls",
                configured=bool(account.host and account.username),
            ),
            smtp=MailEndpointSettings(
                host=account.smtp_host,
                port=account.smtp_port,
                security=account.smtp_security,
                configured=account.smtp_configured and bool(account.from_address),
            ),
            credentials=MailCredentialStatus(
                configured=has_credential,
                source_kind=ENVIRONMENT_SOURCE_KIND,
                reference=self._credentials.inbound_reference(account.id),
            ),
        )


def _to_config(draft: MailAccountDraft) -> MailAccountConfig:
    """Turn typed fields into a validated account, through the one strict parser.

    Raises:
        InvalidMailSettings: the fields do not describe a usable account.
    """
    entry: dict[str, object] = {
        "id": draft.id,
        "host": draft.host,
        "port": draft.port,
        "username": draft.username,
        "mailbox": draft.mailbox,
        "enabled": draft.enabled,
    }
    for key, value in (
        ("smtp_host", draft.smtp_host),
        ("smtp_port", draft.smtp_port),
        ("smtp_security", draft.smtp_security),
        ("smtp_username", draft.smtp_username),
        ("from_address", draft.from_address),
        ("sent_mailbox", draft.sent_mailbox),
    ):
        if value is not None:
            entry[key] = value
    try:
        return parse_mail_account(entry)
    except InvalidAssistantConfig as exc:
        raise InvalidMailSettings(str(exc)) from exc


def _validated(account: MailAccountConfig) -> None:
    """Re-check one account, so a write can never persist something unreadable.

    Raises:
        InvalidMailSettings: the account would not load back.
    """
    try:
        parse_mail_account(_as_entry(account))
    except InvalidAssistantConfig as exc:
        raise InvalidMailSettings(str(exc)) from exc
    if account.smtp_security is not None and account.smtp_security not in SMTP_SECURITY_MODES:
        raise InvalidMailSettings(
            "smtp_security 只能是 "
            + " 或 ".join(SMTP_SECURITY_MODES)
            + "；这个项目不支持明文 SMTP。"
        )


def _as_entry(account: MailAccountConfig) -> dict[str, object]:
    """The stored form of one account, as the parser expects to read it back."""
    entry: dict[str, object] = {
        "id": account.id,
        "host": account.host,
        "port": account.port,
        "username": account.username,
        "mailbox": account.mailbox,
        "enabled": account.enabled,
    }
    for key, value in (
        ("smtp_host", account.smtp_host),
        ("smtp_port", account.smtp_port),
        ("smtp_security", account.smtp_security),
        ("smtp_username", account.smtp_username),
        ("from_address", account.from_address),
        ("sent_mailbox", account.sent_mailbox),
    ):
        if value is not None:
            entry[key] = value
    return entry


def dataclass_replace(account: MailAccountConfig, *, enabled: bool) -> MailAccountConfig:
    """One account with a different enabled flag, rebuilt through the parser."""
    entry = _as_entry(account)
    entry["enabled"] = enabled
    try:
        return parse_mail_account(entry)
    except InvalidAssistantConfig as exc:  # pragma: no cover - the account already parsed once
        raise InvalidMailSettings(str(exc)) from exc


def credential_summary(accounts: Mapping[str, MailAccountSettings]) -> dict[str, int]:
    """Bounded counts for a status line, computed from configuration rather than from messages."""
    return {
        "accounts": len(accounts),
        "enabled": sum(1 for account in accounts.values() if account.enabled),
        "receive_ready": sum(1 for account in accounts.values() if account.receive_ready),
        "send_ready": sum(1 for account in accounts.values() if account.send_ready),
    }


__all__ = [
    "ENVIRONMENT_SOURCE_KIND",
    "RESTART_NOTE",
    "MailAccountDraft",
    "MailAccountSettings",
    "MailAccountSettingsService",
    "MailCredentialStatus",
    "MailEndpointSettings",
    "MailSettingsWriteResult",
    "credential_summary",
]
