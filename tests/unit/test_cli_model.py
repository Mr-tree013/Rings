"""`pw model` and the doctor's model diagnostics (ADR-0017).

The status command is inspection only; the smoke test is the one place a real call happens, so
these tests drive it with a mock transport and check that nothing — prompt, raw body, key —
leaks into the output.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from assistant import cli_model
from assistant.adapters.model.deepseek import DeepSeekAdapter
from assistant.cli import app

runner = CliRunner()
API_KEY = "super-secret-key-123"

MODEL_SECTION = "\n".join(
    (
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
        'reasoning_effort = "low"',
        "max_output_tokens = 4096",
        "timeout_seconds = 120",
        "",
    )
)


def _env(tmp_path: Path, *, model: bool = True, key: bool = True) -> dict[str, str]:
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    body = "format_version = 1\n\n" + (MODEL_SECTION if model else "")
    (directory / "config.toml").write_text(body, encoding="utf-8")
    env = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "COLUMNS": "200",
    }
    if key:
        env["DEEPSEEK_API_KEY"] = API_KEY
    return env


def _response(
    status: int = 200,
    *,
    body: dict[str, object] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    if content is not None:
        return httpx.Response(status, content=content)
    return httpx.Response(status, json=body)


def _completed(text: str, *, usage: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "resp_1",
        "status": "completed",
        "model": "deepseek-flash",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _install_adapter(
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> list[dict[str, object]]:
    """Replace the composition root's adapter factory with one backed by a mock transport."""
    bodies: list[dict[str, object]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.content:
            bodies.append(json.loads(request.content.decode()))
        return handler(request)  # type: ignore[operator]

    def factory(config: object) -> DeepSeekAdapter:
        del config
        return DeepSeekAdapter(
            api_key=API_KEY,
            model="deepseek-flash",
            timeout_seconds=30,
            client=httpx.AsyncClient(
                base_url="https://api.deepseek.com",
                transport=httpx.MockTransport(wrapped),
            ),
        )

    monkeypatch.setattr(cli_model.bootstrap, "model_adapter", factory)
    return bodies


# ------------------------------------------------------------------- status


def test_model_status_without_a_model_section(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model", "status"], env=_env(tmp_path, model=False))

    assert result.exit_code == 0, result.output
    assert "Model not configured." in result.output
    assert API_KEY not in result.output


def test_model_status_reports_the_configuration_without_the_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model", "status"], env=_env(tmp_path, key=False))

    assert result.exit_code == 0, result.output
    assert "deepseek-flash" in result.output
    assert "low" in result.output
    assert "4096" in result.output
    assert "120s" in result.output
    assert "missing" in result.output
    assert "not implemented yet" in result.output


def test_model_status_reports_a_present_key_without_printing_it(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model", "status"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "present" in result.output
    assert "missing" not in result.output
    assert API_KEY not in result.output


# --------------------------------------------------------------------- test


def test_model_test_without_a_model_section(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path, model=False))

    assert result.exit_code == 1
    assert "Model not configured." in result.output


def test_model_test_without_a_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path, key=False))

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_model_test_reports_a_validated_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = _install_adapter(
        monkeypatch,
        lambda request: _response(
            200,
            body=_completed(
                '{"ok": true, "message": "reachable"}',
                usage={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
            ),
        ),
    )

    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "deepseek-flash" in result.output
    assert "OK" in result.output
    assert "reachable" in result.output
    assert "total 13" in result.output
    assert API_KEY not in result.output
    # The request asked for a schema, so the provider could constrain its own output.
    assert bodies[0]["text"]["format"]["type"] == "json_schema"  # type: ignore[index]
    assert "tools" not in bodies[0]


def test_model_test_reports_a_schema_violation_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_adapter(
        monkeypatch,
        lambda request: _response(200, body=_completed('{"ok": "yes", "message": 5}')),
    )

    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "schema" in result.output
    assert API_KEY not in result.output


def test_model_test_reports_a_non_json_answer_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_adapter(
        monkeypatch, lambda request: _response(200, body=_completed("Certainly! Here you go."))
    )

    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "JSON" in result.output


def test_model_test_reports_an_auth_failure_without_leaking_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_adapter(
        monkeypatch,
        lambda request: _response(401, body={"error": {"message": "bad key", "code": "auth"}}),
    )

    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "401" in result.output
    assert API_KEY not in result.output
    assert "Traceback" not in result.output


def test_model_test_reports_rate_limiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_adapter(
        monkeypatch,
        lambda request: _response(429, body={"error": {"message": "slow down"}}),
    )

    result = runner.invoke(app, ["model", "test"], env=_env(tmp_path))

    assert result.exit_code == 1
    assert "429" in result.output


def test_model_test_help_mentions_the_cost_and_the_network() -> None:
    result = runner.invoke(app, ["model", "test", "--help"])

    assert result.exit_code == 0
    assert "may incur provider usage charges" in result.output


# ------------------------------------------------------------------- doctor


def test_doctor_is_healthy_without_a_model_section(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"], env=_env(tmp_path, model=False))

    assert result.exit_code == 0, result.output
    assert "model config" in result.output
    assert "not configured" in result.output


def test_doctor_warns_when_the_model_is_configured_without_a_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"], env=_env(tmp_path, key=False))

    assert result.exit_code == 1
    assert "configured (deepseek, deepseek-flash)" in result.output
    assert "missing" in result.output
    assert "DEEPSEEK_API_KEY" in result.output


def test_doctor_reports_a_present_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"], env=_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert "present" in result.output
    assert API_KEY not in result.output


def test_doctor_and_status_never_contact_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("pw doctor/status must not build an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)

    for command in (["doctor"], ["status"], ["model", "status"]):
        result = runner.invoke(app, command, env=_env(tmp_path))
        assert result.exit_code == 0, (command, result.output)
