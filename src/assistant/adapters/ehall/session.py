"""The headed, manually-logged-in browser session for the whitelisted pipeline (ADR-0025).

Three properties, and every one of them is a refusal rather than a feature:

- **the login is the user's.** This module opens a headed Chromium at the eHall home page and then
  gets out of the way. It never types a username, never types a password, never reads one, and
  there is no configuration key, environment variable or payload field that could hold one.
  SSO and MFA happen between the user and the university;
- **the session is a browser profile, not a secret.** Cookies and storage live in a private
  directory under the XDG data root, created with owner-only permissions, and are never copied
  into the repository, the cache or a payload;
- **top-level navigation is allow-listed.** The pipeline may move between the NJU hosts it needs
  (`ehall`, `ehallapp`, `authserver`); anything else stops the run. Sub-resources — fonts, CDNs,
  images — are ordinary web traffic and are *not* restricted, because a page that cannot load its
  own assets is not a page anyone can use.

Playwright itself is imported lazily: `pw ehall status` and `pw doctor` must be able to say
"the package is installed but the browser is not" without a browser, and every test runs with no
Chromium at all.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from assistant.application.paths import AppPaths
from assistant.domain.errors import (
    EHallBrowserUnavailable,
    EHallUnexpectedOrigin,
)

EHALL_HOME_URL = "https://ehall.nju.edu.cn/"

ALLOWED_TOP_LEVEL_ORIGINS: tuple[str, ...] = (
    "https://ehall.nju.edu.cn",
    "https://ehallapp.nju.edu.cn",
    "https://authserver.nju.edu.cn",
)
"""The only hosts a *top-level* navigation may reach.

A URL, a service id and a selector are never accepted from configuration or the command line: the
pipeline is the thing that knows where it is going.
"""

PROFILE_DIR_NAME = "ehall"
PROFILE_NAME = "nju-profile"
DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900


def ehall_profile_dir() -> Path:
    """The private browser profile directory: XDG data root, never the repository."""
    return AppPaths.resolve().runtime / PROFILE_DIR_NAME / PROFILE_NAME


@dataclass(frozen=True, slots=True)
class ChromiumRuntime:
    """Whether the browser this Playwright would launch is actually installed."""

    available: bool
    detail: str
    """One local sentence, safe to print: no path outside the cache, no download attempt."""


def expected_chromium_builds() -> tuple[str, ...]:
    """The browser build *directories* this Playwright expects, from its own manifest.

    Playwright ships `driver/package/browsers.json`, which is the same list its launcher resolves
    against. Reading it is what makes the difference between "a Chromium of some version exists in
    the cache" and "the Chromium *this* Playwright will launch is here" — the first is what a
    stale cache looks like, and reporting it as available sends the user into a launch failure.

    Returns an empty tuple when the manifest cannot be read; the caller then says so instead of
    guessing. Nothing here downloads, launches or writes anything.
    """
    try:
        import playwright
    except Exception:  # pragma: no cover - only on a broken installation
        return ()
    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        entries = document["browsers"]
    except (OSError, ValueError, KeyError, TypeError):  # pragma: no cover - defensive
        return ()
    builds: list[str] = []
    if not isinstance(entries, list):  # pragma: no cover - defensive
        return ()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        revision = str(entry.get("revision", ""))
        if name.startswith("chromium") and revision:
            # Playwright's cache uses an underscore for a hyphenated browser name:
            # `chromium-headless-shell` → `chromium_headless_shell-1243`.
            builds.append(f"{name.replace('-', '_')}-{revision}")
    return tuple(builds)


def _browsers_root() -> Path:
    """Where Playwright keeps its browsers on this host."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".cache" / "ms-playwright"


def _chromium_executable(build: Path) -> Path | None:
    """The browser binary inside one build directory, across Playwright's two layouts."""
    if not build.is_dir():
        return None
    for pattern in ("chrome-linux*/chrome", "chrome-linux*/headless_shell"):
        for candidate in sorted(build.glob(pattern)):
            return candidate
    return None


def chromium_runtime_state() -> ChromiumRuntime:
    """Whether the headed Chromium build this Playwright needs is installed.

    A filesystem probe, never a launch: `pw doctor` and `pw ehall status` must be able to answer
    without downloading anything and without starting a browser. It is a *version-correct* probe
    though — an old build left in the cache is not "available", because launching it is exactly
    what fails.
    """
    builds = expected_chromium_builds()
    headed = [build for build in builds if build.startswith("chromium-")]
    if not builds:
        return ChromiumRuntime(
            False,
            "unknown (cannot read Playwright's browser manifest; "
            "run `uv run playwright install chromium`)",
        )
    if not headed:  # pragma: no cover - Playwright always declares a headed build
        return ChromiumRuntime(False, "missing (this Playwright declares no headed chromium)")
    expected = headed[0]
    executable = _chromium_executable(_browsers_root() / expected)
    if executable is None:
        return ChromiumRuntime(
            False,
            f"missing (expected {expected}; run `uv run playwright install chromium`)",
        )
    return ChromiumRuntime(True, f"available ({expected})")


def chromium_runtime_available() -> bool:
    """Whether the Chromium build this Playwright will launch is installed."""
    return chromium_runtime_state().available


def playwright_installed() -> bool:
    """Whether the Playwright package itself can be imported."""
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # pragma: no cover - only on a broken installation
        return False
    return True


def check_top_level_origin(url: str) -> None:
    """Refuse a top-level navigation outside the allow-list.

    Raises:
        EHallUnexpectedOrigin: the URL's origin is not one of the NJU hosts this pipeline uses.
    """
    split = urlsplit(url)
    origin = f"{split.scheme}://{split.netloc}"
    if origin not in ALLOWED_TOP_LEVEL_ORIGINS:
        raise EHallUnexpectedOrigin(url)


@dataclass(frozen=True, slots=True)
class EHallSessionState:
    """What a session looks like to the CLI without starting a browser."""

    profile_dir: Path
    profile_exists: bool
    playwright_installed: bool
    chromium_available: bool
    chromium_detail: str = ""
    """The same answer in words, so a status table can say *which* build is missing."""


def session_state() -> EHallSessionState:
    """Describe the local session state. Read-only: nothing is created or launched."""
    profile = ehall_profile_dir()
    chromium = chromium_runtime_state()
    return EHallSessionState(
        profile_dir=profile,
        profile_exists=profile.is_dir(),
        playwright_installed=playwright_installed(),
        chromium_available=chromium.available,
        chromium_detail=chromium.detail,
    )


def _ensure_private_directory(path: Path) -> None:
    """Create the profile directory, owner-only, and repair loose permissions."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


class EHallBrowserSession:
    """One headed Chromium context over the persistent NJU profile.

    Used as an async context manager. When the context exits the browser closes; the profile on
    disk keeps the login, which is the entire point of a persistent session.
    """

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        self._timeout_seconds = timeout_seconds
        self._playwright: object | None = None
        self._context: object | None = None

    @property
    def profile_dir(self) -> Path:
        """Where this session's cookies and storage live."""
        return ehall_profile_dir()

    @property
    def context(self) -> object:
        """The live browser context. Only the eHall adapter ever touches it."""
        if self._context is None:  # pragma: no cover - guarded by the context manager
            raise EHallBrowserUnavailable("the eHall browser session is not open")
        return self._context

    async def __aenter__(self) -> EHallBrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch headed Chromium over the private profile.

        Raises:
            EHallBrowserUnavailable: Playwright or Chromium is missing.
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover - only on a broken installation
            raise EHallBrowserUnavailable(
                "the playwright package is not importable; install the project dependencies"
            ) from exc
        _ensure_private_directory(self.profile_dir)
        playwright = await async_playwright().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": DEFAULT_VIEWPORT_WIDTH, "height": DEFAULT_VIEWPORT_HEIGHT},
            )
        except Exception as exc:
            await playwright.stop()
            raise EHallBrowserUnavailable(
                "Chromium is not available; run `uv run playwright install chromium` "
                f"({type(exc).__name__})"
            ) from exc
        context.set_default_timeout(self._timeout_seconds * 1000)
        self._playwright = playwright
        self._context = context

    async def close(self) -> None:
        """Close the browser and stop the driver. Safe to call twice."""
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()  # type: ignore[attr-defined]
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()  # type: ignore[attr-defined]

    async def new_page(self) -> object:
        """Open a page in this context, enforcing the origin allow-list on navigation."""
        context = self.context
        page = await context.new_page()  # type: ignore[attr-defined]
        page.on("framenavigated", _guard_main_frame(page))
        return page

    async def open_ehall_home(self) -> object:
        """Open the eHall home page and let the user complete SSO by hand.

        The user's typing goes into the browser, not through this process: there is no code here
        that could fill a credential field.
        """
        page = await self.new_page()
        await page.goto(EHALL_HOME_URL)  # type: ignore[attr-defined]
        return page


def _guard_main_frame(page: Any) -> Callable[[Any], None]:
    """A navigation listener that fails closed on a top-level navigation outside the allow-list."""

    def _on_framenavigated(frame: object) -> None:
        if frame is not getattr(page, "main_frame", None):
            return  # sub-frames and sub-resources are ordinary web traffic
        check_top_level_origin(str(getattr(frame, "url", "")))

    return _on_framenavigated


def iter_allowed_origins() -> Iterator[str]:
    """The allow-list, for tests and for `pw ehall status`."""
    yield from ALLOWED_TOP_LEVEL_ORIGINS


__all__ = [
    "ALLOWED_TOP_LEVEL_ORIGINS",
    "DEFAULT_VIEWPORT_HEIGHT",
    "DEFAULT_VIEWPORT_WIDTH",
    "EHALL_HOME_URL",
    "PROFILE_DIR_NAME",
    "PROFILE_NAME",
    "EHallBrowserSession",
    "EHallSessionState",
    "check_top_level_origin",
    "chromium_runtime_available",
    "ehall_profile_dir",
    "iter_allowed_origins",
    "playwright_installed",
    "session_state",
]
