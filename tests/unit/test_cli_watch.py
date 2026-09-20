"""`pw watch` and `pw ingest`: the observation workflow from the command line (ADR-0029).

Only `pw watch sync` uses the network, and these tests inject a scripted source in its place so the
command can be exercised without one. Everything else reads local state, which is the property the
tests are checking: a status view works with no configuration, no daemon and no connectivity.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.cli import app
from assistant.domain.config import WatchersConfig, WebTargetConfig
from tests.support.watchers import TARGET_ID, TARGET_URL, FakeWebSource

runner = CliRunner()

PAGE_A = "Notices\nRegistration closes Oct 20.\n"
PAGE_B = "Notices\nRegistration closes Oct 25.\n"

WATCHER_CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[watchers]",
        "poll_interval_seconds = 300",
        "full_fetch_every = 24",
        "",
        "[[watchers.web]]",
        f'id = "{TARGET_ID}"',
        f'url = "{TARGET_URL}"',
        "enabled = true",
        "",
    )
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


@pytest.fixture
def configured(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeWebSource:
    """A host with one configured watcher and a scripted source standing in for the network."""
    directory = isolated / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(WATCHER_CONFIG, encoding="utf-8")
    source = FakeWebSource(
        {TARGET_URL: PAGE_A}, snapshots=bootstrap.web_snapshot_store()
    )
    monkeypatch.setattr(bootstrap, "web_source", lambda: source)
    return source


def _prefix(output: str) -> str:
    match = re.search(r"\b([0-9a-f]{8})\b", output)
    assert match is not None, output
    return match.group(1)


# --------------------------------------------------------------------------- targets


def test_targets_and_status_work_with_no_configuration(isolated: Path) -> None:
    targets = runner.invoke(app, ["watch", "targets"])
    status = runner.invoke(app, ["watch", "status"])

    assert targets.exit_code == 0, targets.output
    assert "no watcher targets are configured" in targets.output
    assert status.exit_code == 0, status.output
    assert "no enabled watcher targets" in status.output


def test_targets_lists_the_configured_pages(configured: FakeWebSource) -> None:
    result = runner.invoke(app, ["watch", "targets"])

    assert result.exit_code == 0, result.output
    assert TARGET_ID in result.output
    assert TARGET_URL in result.output
    assert "never" in result.output
    assert "Full fetch every 24 checks" in result.output
    assert configured.requests == []  # listing contacts nothing


# ------------------------------------------------------------------------------ sync


def test_sync_records_a_baseline_and_emits_nothing(configured: FakeWebSource) -> None:
    result = runner.invoke(app, ["watch", "sync"])

    assert result.exit_code == 0, result.output
    assert "baseline" in result.output
    observations = runner.invoke(app, ["watch", "observations"])
    assert "baseline" in observations.output
    assert "no observations recorded yet" not in observations.output


def test_sync_reports_a_change_with_its_observation_and_event(
    configured: FakeWebSource,
) -> None:
    runner.invoke(app, ["watch", "sync"])
    configured.set_page(TARGET_URL, PAGE_B)

    result = runner.invoke(app, ["watch", "sync"])

    assert result.exit_code == 0, result.output
    assert "changed" in result.output
    assert "No external side effect was attempted" in result.output


def test_sync_can_target_one_page(configured: FakeWebSource) -> None:
    ok = runner.invoke(app, ["watch", "sync", "--target", TARGET_ID])
    unknown = runner.invoke(app, ["watch", "sync", "--target", "nope"])

    assert ok.exit_code == 0, ok.output
    assert unknown.exit_code == 1
    assert "no watcher target is configured" in unknown.output


def test_status_shows_the_baseline_and_the_counter(configured: FakeWebSource) -> None:
    runner.invoke(app, ["watch", "sync"])

    result = runner.invoke(app, ["watch", "status"])

    assert result.exit_code == 0, result.output
    assert "yes" in result.output  # a baseline exists
    assert "An initial fetch establishes a baseline" in result.output


# ---------------------------------------------------------------------- observations


def test_observations_can_be_filtered_and_read(configured: FakeWebSource) -> None:
    runner.invoke(app, ["watch", "sync"])
    configured.set_page(TARGET_URL, PAGE_B)
    runner.invoke(app, ["watch", "sync"])
    listed = runner.invoke(app, ["watch", "observations"])
    prefix = _prefix(listed.output)

    shown = runner.invoke(app, ["watch", "observation", "show", prefix])

    assert shown.exit_code == 0, shown.output
    assert TARGET_ID in shown.output
    assert TARGET_URL in shown.output
    assert "Content" in shown.output
    assert "Registration closes" in shown.output
    assert "Analysis" in shown.output
    assert "pending" in shown.output
    assert "never shows a path" in shown.output
    assert "web/snapshots" not in shown.output


def test_observations_can_be_filtered_by_target(configured: FakeWebSource) -> None:
    runner.invoke(app, ["watch", "sync"])

    ok = runner.invoke(app, ["watch", "observations", "--target", TARGET_ID])
    unknown = runner.invoke(app, ["watch", "observations", "--target", "nope"])

    assert ok.exit_code == 0, ok.output
    assert unknown.exit_code == 1


def test_an_unknown_observation_prefix_fails_cleanly(configured: FakeWebSource) -> None:
    result = runner.invoke(app, ["watch", "observation", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_observations_without_any_fetch_report_nothing(configured: FakeWebSource) -> None:
    result = runner.invoke(app, ["watch", "observations"])

    assert result.exit_code == 0, result.output
    assert "no observations recorded yet" in result.output


# -------------------------------------------------------------------------- ingest


def test_ingest_stores_text_and_says_what_happens_next(isolated: Path) -> None:
    result = runner.invoke(
        app, ["ingest", "text", "Forwarded notice", "--source", "qq-forward"]
    )

    assert result.exit_code == 0, result.output
    assert "Stored manual input:" in result.output
    assert "Queued inbound event:" in result.output
    assert "may be sent to the configured model provider" in result.output


def test_ingest_defaults_to_manual_and_lists_newest_first(isolated: Path) -> None:
    runner.invoke(app, ["ingest", "text", "first note"])
    runner.invoke(app, ["ingest", "text", "second note"])

    listed = runner.invoke(app, ["ingest", "list"])

    assert listed.exit_code == 0, listed.output
    assert "manual" in listed.output
    assert "untrusted quoted text" in listed.output
    assert listed.output.index("second note") < listed.output.index("first note")


def test_ingest_show_prints_the_text_and_a_pending_analysis(isolated: Path) -> None:
    runner.invoke(app, ["ingest", "text", "a note about the lab"])
    prefix = _prefix(runner.invoke(app, ["ingest", "list"]).output)

    shown = runner.invoke(app, ["ingest", "show", prefix])

    assert shown.exit_code == 0, shown.output
    assert "a note about the lab" in shown.output
    assert "pending" in shown.output
    assert "Source" in shown.output


def test_ingest_refuses_blank_text_and_unknown_sources(isolated: Path) -> None:
    blank = runner.invoke(app, ["ingest", "text", "   "])
    unknown = runner.invoke(app, ["ingest", "text", "note", "--source", "shell"])

    assert blank.exit_code == 1
    assert "blank" in blank.output
    assert unknown.exit_code == 1
    assert "expected one of" in unknown.output
    assert "no manual input stored" in runner.invoke(app, ["ingest", "list"]).output


def test_an_unknown_input_prefix_fails_cleanly(isolated: Path) -> None:
    result = runner.invoke(app, ["ingest", "show", "ffffffff"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


# ------------------------------------------------------------- absent conveniences


@pytest.mark.parametrize(
    "arguments",
    [
        ["watch", "add", "x", "--url", "https://example.edu"],
        ["watch", "fetch", "https://127.0.0.1/"],
        ["watch", "sync", "--url", "https://127.0.0.1/"],
        ["ingest", "run", "some text"],
        ["ingest", "text", "note", "--url", "https://example.edu"],
    ],
)
def test_there_is_no_way_to_watch_or_ingest_an_arbitrary_url(
    isolated: Path, arguments: list[str]
) -> None:
    """The URL is configuration, never a command-line argument, and never a model's choice."""
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0, arguments


def test_the_default_watchers_config_watches_nothing() -> None:
    assert WatchersConfig().enabled_targets == ()
    assert WebTargetConfig(id=TARGET_ID, url=TARGET_URL).enabled is True
