"""Login and session safety: what this project never does with a university credential.

These are the properties that make a browser in this project different from a browser in a
generic agent: there is no password to leak, because there is no code that could hold one, and the
only navigation the pipeline is allowed to make is to three known NJU hosts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.ehall import session as session_module
from assistant.adapters.ehall.session import (
    ALLOWED_TOP_LEVEL_ORIGINS,
    EHALL_HOME_URL,
    EHallBrowserSession,
    check_top_level_origin,
    ehall_profile_dir,
    playwright_installed,
    session_state,
)
from assistant.domain.config import AssistantConfig, EHallConfig
from assistant.domain.errors import EHallUnexpectedOrigin


def test_the_allowlist_is_exactly_the_three_nju_hosts() -> None:
    assert ALLOWED_TOP_LEVEL_ORIGINS == (
        "https://ehall.nju.edu.cn",
        "https://ehallapp.nju.edu.cn",
        "https://authserver.nju.edu.cn",
    )
    assert EHALL_HOME_URL.startswith("https://ehall.nju.edu.cn")


@pytest.mark.parametrize(
    "url",
    [
        "https://ehall.nju.edu.cn/",
        "https://ehall.nju.edu.cn/service?id=1",
        "https://ehallapp.nju.edu.cn/application",
        "https://authserver.nju.edu.cn/cas/login",
    ],
)
def test_allowed_navigation_passes(url: str) -> None:
    check_top_level_origin(url)  # does not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://ehall.nju.edu.cn/",  # the scheme matters: plaintext is not the allow-listed origin
        "https://ehall.nju.edu.cn.evil.example/",
        "https://nju.edu.cn/",
        "https://mail.nju.edu.cn/",
        "file:///etc/passwd",
        "about:blank",
        "",
    ],
)
def test_anything_else_fails_closed(url: str) -> None:
    with pytest.raises(EHallUnexpectedOrigin):
        check_top_level_origin(url)


def test_the_profile_lives_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    profile = ehall_profile_dir()

    assert profile == tmp_path / "data" / "growing-assistant" / "ehall" / "nju-profile"
    assert Path.cwd() not in profile.parents
    assert "cache" not in str(profile)


def test_the_session_state_report_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))

    state = session_state()

    assert state.profile_exists is False
    assert state.chromium_available is False  # nothing has been downloaded
    assert state.playwright_installed is True
    assert not (tmp_path / "data").exists()  # describing the state creates nothing
    assert not (tmp_path / "browsers").exists()


def test_chromium_detection_looks_for_the_build_playwright_will_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-correct probe: an unrelated build in the cache is not "available".

    The cache is shared and grows as Playwright is upgraded, so a stale `chromium-1234` sitting
    next to the expected build is exactly what a user has after a dependency bump — and reporting
    it as available is what produced a "Chromium runtime: available" status followed by a launch
    failure.
    """
    browsers = tmp_path / "browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    expected = [
        build
        for build in session_module.expected_chromium_builds()
        if build.startswith("chromium-")
    ]
    assert expected, "the installed Playwright should declare a headed chromium build"
    build = expected[0]

    # An old build, complete with a binary, is still not the build this Playwright will launch.
    stale = browsers / "chromium-9999" / "chrome-linux64"
    stale.mkdir(parents=True)
    (stale / "chrome").write_text("", encoding="utf-8")
    assert session_module.chromium_runtime_available() is False
    assert build in session_module.chromium_runtime_state().detail

    # The expected build, with its binary, is.
    current = browsers / build / "chrome-linux64"
    current.mkdir(parents=True)
    (current / "chrome").write_text("", encoding="utf-8")

    assert session_module.chromium_runtime_available() is True
    assert build in session_module.chromium_runtime_state().detail
    assert playwright_installed() is True


def test_a_partially_downloaded_build_is_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted download leaves the directory behind; a directory is not a browser."""
    browsers = tmp_path / "browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    expected = [
        build
        for build in session_module.expected_chromium_builds()
        if build.startswith("chromium-")
    ]
    assert expected
    (browsers / expected[0]).mkdir(parents=True)

    assert session_module.chromium_runtime_available() is False
    assert "uv run playwright install chromium" in session_module.chromium_runtime_state().detail


def test_the_login_path_cannot_type_a_credential() -> None:
    """The session module has no identifier or call that could fill, type or read a credential.

    Checked on the syntax tree rather than on text: the docstring *explains* that no password is
    ever entered, and that explanation is the point.
    """
    import ast

    tree = ast.parse(Path(session_module.__file__).read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
            if isinstance(node.value, ast.Name):
                identifiers.add(node.value.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # Only *method* calls: a bare `type(exc)` builtin is not typing into a field.
            calls.add(node.func.attr)

    forbidden_names = {"password", "passwd", "credential", "get_by_label", "get_by_placeholder"}
    assert not identifiers & forbidden_names, identifiers & forbidden_names
    for forbidden_call in ("fill", "type", "press", "fill_form", "set_input_files"):
        assert forbidden_call not in calls, forbidden_call


def test_the_login_opens_the_home_page_and_stops() -> None:
    """`EHallBrowserSession` exposes exactly one navigation, to the eHall home page."""
    source = Path(session_module.__file__).read_text(encoding="utf-8")

    assert "goto(EHALL_HOME_URL)" in source
    assert source.count("goto(") == 1
    assert "headless=False" in source  # a person has to be able to see and use it


def test_the_config_has_no_credential_url_or_selector_field() -> None:
    fields = set(EHallConfig.__dataclass_fields__)

    assert fields == {"enabled", "timeout_seconds"}
    for forbidden in ("username", "password", "service_url", "submit_selector", "verify_tls",
                      "headless", "allowed_origins"):
        assert forbidden not in fields


def test_the_pipeline_is_disabled_by_default() -> None:
    config = AssistantConfig()

    assert config.ehall.enabled is False
    assert config.ehall.timeout_seconds == 30


def test_the_session_factory_takes_no_credential() -> None:
    session = EHallBrowserSession(timeout_seconds=15)

    assert session.profile_dir == ehall_profile_dir()
    assert "password" not in repr(session).lower()
