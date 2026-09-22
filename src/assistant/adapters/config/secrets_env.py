"""The user-owned secrets file: an env file the person writes, not a key store (ADR-0046).

Credentials in this project come from the environment. Typing two `export`s into every new shell
before starting the daemon was the last manual step left in the startup path, so the entry points
now load one file the *user* owns:

```ini
# ~/.config/growing-assistant/secrets.env   (mode 0600)
GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD=…
GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD=…
```

Three properties make it safe to read automatically:

* **the environment wins.** A variable already set is never overwritten, so a one-off `export`
  still overrides the file;
* **the vocabulary is closed.** Only the model key and the derived
  `GROWING_ASSISTANT_MAIL_<ID>_PASSWORD|_SMTP_PASSWORD` names are accepted; a single other key
  refuses the whole file, so it cannot quietly become a general secret store. The model key's
  *name* is passed in by the composition root rather than written here — the variable that holds a
  provider credential is named in exactly one build, and this file is deliberately not a second
  place where it is written down;
* **it is refused, not repaired.** Loose permissions or an unparsable line disable the file and say
  why. Nothing here writes, prints, logs or repairs anything — the caller decides what to say, and
  no branch ever includes a value.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from assistant.adapters.mail.credentials import PREFIX as MAIL_ENV_PREFIX
from assistant.application.paths import AppPaths

LOGGER = logging.getLogger("assistant.secrets")

SECRETS_FILENAME = "secrets.env"
"""The file's name under the XDG configuration directory."""

_MAIL_KEY = re.compile(rf"^{MAIL_ENV_PREFIX}_[A-Z0-9_]+_(?:SMTP_)?PASSWORD$")
"""The mail variable names this project derives, mirrored from the module that derives them."""


class SecretEnvStatus(StrEnum):
    """What one load attempt found."""

    LOADED = "loaded"
    MISSING = "missing"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class SecretEnvResult:
    """The outcome, safe to print: names and counts, never values."""

    path: Path
    status: SecretEnvStatus
    names: tuple[str, ...] = ()
    """Every key the file declared, in sorted order."""
    applied: tuple[str, ...] = ()
    """The keys that were actually copied into the environment."""
    reason: str | None = None
    """Why a file was refused, in the user's words and without any value."""

    @property
    def configured(self) -> bool:
        """Whether the file was usable at all (a missing file is not a refusal)."""
        return self.status is not SecretEnvStatus.REFUSED


def secrets_env_path(config_root: Path | None = None) -> Path:
    """Where the secrets file lives: beside `config.toml`, never in the repository."""
    root = Path(config_root) if config_root is not None else AppPaths.resolve().config
    return root / SECRETS_FILENAME


def _allowed(key: str, *, model_key: str) -> bool:
    return key == model_key or bool(_MAIL_KEY.match(key))


def _parse(text: str, *, model_key: str) -> dict[str, str] | str:
    """Return the file's key/value pairs, or a refusal reason."""
    parsed: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            return f"第 {number} 行不是 KEY=VALUE"
        if not _allowed(key, model_key=model_key):
            return f"第 {number} 行的键 {key} 不被接受"
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
            cleaned = cleaned[1:-1]
        if not cleaned:
            return f"第 {number} 行的值为空"
        parsed[key] = cleaned
    return parsed


def load_secret_environment(
    *,
    path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    model_key: str,
) -> SecretEnvResult:
    """Load the user's secrets file into `environ` (the process environment by default).

    `model_key` is the name of the provider credential variable, passed in by the caller so this
    module never becomes a second authority for it (see the module docstring).

    Never raises for a bad file: a broken or missing secrets file must not stop the daemon, it must
    be reported. Nothing is printed here — the caller owns every message.
    """
    target = Path(path) if path is not None else secrets_env_path()
    into: MutableMapping[str, str] = os.environ if environ is None else environ
    if not target.is_file():
        return SecretEnvResult(path=target, status=SecretEnvStatus.MISSING)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as exc:  # pragma: no cover - the file vanished between checks
        return SecretEnvResult(
            path=target, status=SecretEnvStatus.REFUSED, reason=f"读不到文件：{exc.strerror}"
        )
    if mode & 0o077:
        return SecretEnvResult(
            path=target,
            status=SecretEnvStatus.REFUSED,
            reason=f"权限是 {mode:04o}，必须是 0600（chmod 600 {target}）",
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SecretEnvResult(
            path=target, status=SecretEnvStatus.REFUSED, reason=f"读不到文件：{exc}"
        )
    parsed = _parse(text, model_key=model_key)
    if isinstance(parsed, str):
        return SecretEnvResult(path=target, status=SecretEnvStatus.REFUSED, reason=parsed)
    applied: list[str] = []
    for key in sorted(parsed):
        if into.get(key, "").strip():
            continue  # the environment wins
        into[key] = parsed[key]
        applied.append(key)
    return SecretEnvResult(
        path=target,
        status=SecretEnvStatus.LOADED,
        names=tuple(sorted(parsed)),
        applied=tuple(applied),
    )


def load_secret_environment_into_process(*, model_key: str) -> SecretEnvResult:
    """Load the secrets file for a real entry point, logging names only.

    The log line is the *only* place this is mentioned, it carries no value, and it goes through
    the logger — never stdout, because `growing-assistant-mcp` speaks a stdio protocol there.
    """
    result = load_secret_environment(model_key=model_key)
    if result.status is SecretEnvStatus.REFUSED:
        LOGGER.warning("secrets file refused: %s", result.reason)
    elif result.status is SecretEnvStatus.LOADED:
        LOGGER.info(
            "secrets file loaded: %d key(s), %d applied",
            len(result.names),
            len(result.applied),
        )
    return result


__all__ = [
    "SECRETS_FILENAME",
    "SecretEnvResult",
    "SecretEnvStatus",
    "load_secret_environment",
    "load_secret_environment_into_process",
    "secrets_env_path",
]
