"""The user-owned secrets file (ADR-0046). Nothing here ever prints a value."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.config.secrets_env import (
    SecretEnvStatus,
    load_secret_environment,
    secrets_env_path,
)

# The provider key's variable *name* is owned by the composition root; a test may spell it out,
# because the architecture check that pins that rule only scans `src/assistant`.
MODEL_KEY = "DEEPSEEK_API_KEY"


def _write(path: Path, body: str, mode: int = 0o600) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_missing_file_is_reported_not_created(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"

    result = load_secret_environment(path=path, environ={}, model_key=MODEL_KEY)

    assert result.status is SecretEnvStatus.MISSING
    assert result.names == ()
    assert path.exists() is False


def test_an_allowed_file_is_loaded_without_overriding_the_environment(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "secrets.env",
        "# comment\nDEEPSEEK_API_KEY=from-file\n"
        "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD='quoted value'\n"
        "GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD=  spaced  \n",
    )
    environ = {"DEEPSEEK_API_KEY": "from-env"}

    result = load_secret_environment(path=path, environ=environ, model_key=MODEL_KEY)

    assert result.status is SecretEnvStatus.LOADED
    assert result.names == (
        "DEEPSEEK_API_KEY",
        "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",
        "GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD",
    )
    assert result.applied == (
        "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",
        "GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD",
    )
    assert environ["DEEPSEEK_API_KEY"] == "from-env"  # the environment wins
    assert environ["GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD"] == "quoted value"
    assert environ["GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD"] == "spaced"


def test_loose_permissions_refuse_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "DEEPSEEK_API_KEY=x\n", mode=0o644)
    environ: dict[str, str] = {}

    result = load_secret_environment(path=path, environ=environ, model_key=MODEL_KEY)

    assert result.status is SecretEnvStatus.REFUSED
    assert "0600" in (result.reason or "")
    assert environ == {}


def test_an_unknown_key_refuses_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "DEEPSEEK_API_KEY=x\npassword=y\n")
    environ: dict[str, str] = {}

    result = load_secret_environment(path=path, environ=environ, model_key=MODEL_KEY)

    assert result.status is SecretEnvStatus.REFUSED
    assert "password" in (result.reason or "")
    assert environ == {}  # nothing is half-loaded


def test_an_empty_value_refuses_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD=\n")

    result = load_secret_environment(path=path, environ={}, model_key=MODEL_KEY)

    assert result.status is SecretEnvStatus.REFUSED
    assert "空" in (result.reason or "")


def test_the_path_is_the_xdg_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert secrets_env_path() == tmp_path / "config" / "growing-assistant" / "secrets.env"
