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


def chromium_runtime_available() -> bool:
    """Whether a Chromium build looks installed. A filesystem probe, never a launch.

    Playwright keeps its browsers under `PLAYWRIGHT_BROWSERS_PATH` (by default
    `~/.cache/ms-playwright`)
    and the directory name carries the build number, so the check is a glob rather than a fixed
    path. `pw doctor` must never download anything, and must never start a browser to find out.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    root = Path(override).expanduser() if override else Path.home() / ".cache" / "ms-playwright"
    if not root.is_dir():
        return False
    return any(
        child.is_dir() and child.name.startswith("chromium")
        for child in root.iterdir()
    )


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


def session_state() -> EHallSessionState:
    """Describe the local session state. Read-only: nothing is created or launched."""
    profile = ehall_profile_dir()
    return EHallSessionState(
        profile_dir=profile,
        profile_exists=profile.is_dir(),
        playwright_installed=playwright_installed(),
        chromium_available=chromium_runtime_available(),
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
