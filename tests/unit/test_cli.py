"""Phase 0 tests for the `pw` CLI: `status` runs, `doctor` passes on Python 3.13."""

from __future__ import annotations

from typer.testing import CliRunner

from assistant.cli import app

runner = CliRunner()


def test_status_runs_and_reports_phase_zero() -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Phase 0" in result.output
    assert "not implemented" in result.output


def test_help_lists_only_implemented_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "status" in result.output
    assert "doctor" in result.output
    assert "approve" not in result.output


def test_doctor_succeeds_on_python_313() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "3.13" in result.output
    assert "environment looks usable" in result.output


def test_doctor_reports_the_runtime_database_path() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "runtime db" in result.output
    assert "assistant.db" in result.output
