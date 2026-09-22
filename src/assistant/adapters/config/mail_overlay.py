"""The managed mail-accounts overlay (ADR-0043).

The user's `config.toml` is *theirs*: it has comments, an order, and keys this build has never heard
of. A settings page that round-trips it will eventually delete something, so the settings surface
never writes it. Instead there is a second, small, fully-managed file next to it:

```text
<config dir>/config.toml          the user's file — read, never rewritten
<config dir>/mail-accounts.toml   entirely ours — the complete account list, rewritten on each edit
```

Three properties make this safe rather than merely tidy:

* **it is complete.** The service writes every effective account, so the first edit through the UI
  imports whatever was already in `config.toml` instead of replacing it. A host that never opens
  the settings page has no overlay at all and behaves exactly as before.
* **it is atomic and private.** The file is written beside itself and moved into place with
  `os.replace`, inside a directory created `0700`, and created `0600` — the same helpers the runtime
  database uses. A half-written configuration file would be a machine that cannot start.
* **it cannot hold a secret.** Entries are parsed by the *same* strict parser as `config.toml`,
  which refuses `password`, `secret`, `api_key` and `verify_tls`. There is no second, looser reader
  to disagree with it.

Everything here is blocking filesystem work, called from the application layer through
`asyncio.to_thread` like every other adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)
from assistant.domain.config import MailAccountConfig, parse_mail_account
from assistant.domain.errors import InvalidAssistantConfig

MAIL_ACCOUNTS_FILENAME = "mail-accounts.toml"
"""The managed overlay. Its absence is not an error: it means "nothing was changed here yet"."""

MANAGED_HEADER = (
    "# Managed by Rings (`pw` / the settings page). Avoid editing while Rings is running.\n"
    "# This file holds mail account *metadata* only. Passwords are never stored here: they come\n"
    "# from the environment, as GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD and\n"
    "# GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD. See docs/adr/0043.\n"
)
"""Written at the top of every managed file, so a curious human is told the rules that matter."""


def overlay_path(config_path: Path) -> Path:
    """Where the overlay lives for a given primary configuration file."""
    return Path(config_path).parent / MAIL_ACCOUNTS_FILENAME


def read_overlay(path: Path) -> tuple[MailAccountConfig, ...]:
    """Read the managed accounts, or `()` when the file does not exist.

    Raises:
        InvalidAssistantConfig: the file exists but is not valid TOML, or an entry is malformed.
    """
    target = Path(path)
    if not target.is_file():
        return ()
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidAssistantConfig(f"{target} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise InvalidAssistantConfig(f"{target} could not be read: {exc}") from exc
    mail = data.get("mail")
    if mail is None:
        return ()
    if not isinstance(mail, dict):
        raise InvalidAssistantConfig(f"{target}: [mail] must be a table")
    unknown = sorted(set(mail) - {"accounts"})
    if unknown:
        raise InvalidAssistantConfig(
            f"{target}: this file is managed and holds accounts only, but it names "
            f"{', '.join(unknown)}"
        )
    entries = mail.get("accounts", ())
    if not isinstance(entries, list):
        raise InvalidAssistantConfig(f"{target}: [[mail.accounts]] must be a list of tables")
    return tuple(parse_mail_account(entry) for entry in entries)


def write_overlay(path: Path, accounts: tuple[MailAccountConfig, ...]) -> None:
    """Write the complete managed account list atomically, with private permissions.

    Raises:
        InvalidAssistantConfig: the directory could not be created or the write failed.
    """
    target = Path(path)
    directory = target.parent
    try:
        ensure_private_directory(directory)
        document = MANAGED_HEADER + _render(accounts)
        # The temporary file lives in the destination directory so `os.replace` stays atomic: a
        # rename across filesystems is a copy, and a crash in the middle of a copy is a broken
        # configuration file.
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by the `with` below
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".mail-accounts-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            ensure_private_file(Path(handle.name))
            os.replace(handle.name, target)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        ensure_private_file(target)
    except OSError as exc:
        raise InvalidAssistantConfig(f"{target} could not be written: {exc}") from exc


def merge_accounts(
    base: tuple[MailAccountConfig, ...], overlay: tuple[MailAccountConfig, ...]
) -> tuple[MailAccountConfig, ...]:
    """The effective account list: the primary file first, then the overlay by id (it wins).

    Order is preserved deliberately: an account that exists only in `config.toml` keeps its place in
    the list, so enabling one account from the settings page does not reorder the others.
    """
    by_id: dict[str, MailAccountConfig] = {}
    order: list[str] = []
    for account in (*base, *overlay):
        if account.id not in by_id:
            order.append(account.id)
        by_id[account.id] = account
    return tuple(by_id[account_id] for account_id in order)


class OverlayMailSettingsStore:
    """`MailSettingsStore` over the managed overlay file (ADR-0043 §17-18).

    The application layer owns what may be stored; this owns *where*. It is the only writer, so
    there is exactly one code path that can change the file, and it is the atomic one.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file this store owns."""
        return self._path

    async def save_accounts(self, accounts: Sequence[MailAccountConfig]) -> None:
        """Write the complete managed list atomically, in a worker thread."""
        await asyncio.to_thread(write_overlay, self._path, tuple(accounts))


def _render(accounts: tuple[MailAccountConfig, ...]) -> str:
    """Render `[[mail.accounts]]` tables. Strings go through JSON escaping, which TOML accepts."""
    lines: list[str] = []
    for account in accounts:
        lines.append("[[mail.accounts]]")
        lines.append(f"id = {_string(account.id)}")
        lines.append(f"host = {_string(account.host)}")
        lines.append(f"port = {int(account.port)}")
        lines.append(f"username = {_string(account.username)}")
        lines.append(f"mailbox = {_string(account.mailbox)}")
        lines.append(f"enabled = {_boolean(account.enabled)}")
        text_fields = (
            ("smtp_host", account.smtp_host),
            ("smtp_security", account.smtp_security),
            ("smtp_username", account.smtp_username),
            ("from_address", account.from_address),
            ("sent_mailbox", account.sent_mailbox),
        )
        if account.smtp_port is not None:
            lines.append(f"smtp_port = {int(account.smtp_port)}")
        for key, value in text_fields:
            if value is None:
                continue
            lines.append(f"{key} = {_string(value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _string(value: str) -> str:
    """A TOML basic string. JSON escaping is a strict subset of what TOML accepts here."""
    return json.dumps(value, ensure_ascii=False)


def _boolean(value: bool) -> str:
    return "true" if value else "false"


__all__ = [
    "MAIL_ACCOUNTS_FILENAME",
    "MANAGED_HEADER",
    "OverlayMailSettingsStore",
    "merge_accounts",
    "overlay_path",
    "read_overlay",
    "write_overlay",
]
