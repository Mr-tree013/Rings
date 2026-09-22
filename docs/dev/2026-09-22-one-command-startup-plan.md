# One-command Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `rings up` start (or reuse) the daemon, pair the browser automatically and open
`/chat`; make `rings down` stop it; let credentials and Windows logon autostart remove the last two
manual steps — without adding any new authority.

**Architecture:** The daemon keeps its existing `flock` lifecycle; a thin adapter
(`adapters/runtime/daemon_process.py`) answers "is it running", spawns it detached, waits for
readiness and stops it. `cli_up.py` orchestrates and renders, with every side effect injected as a
callable so the decision logic is unit-testable. Pairing reuses the existing `POST /api/pair`
capability: `rings up` mints one token and opens `/chat#pair=<token>`, and the page redeems it only
when it is not already paired. Credentials move from "export by hand" to a user-owned `0600`
`secrets.env` that the entry points load (environment still wins).

**Tech Stack:** Python 3.13, existing Typer/rich CLI, `flock` + `subprocess` + `urllib.request` in
adapters, vanilla JS in `chat.js`, pytest + pytest-asyncio, Playwright for the browser check.

**Spec:** `docs/specs/0002-one-command-startup.md` (read it; every decision below is argued there).

## Global Constraints

- No new HTTP route, no new dependency, no new migration, no new executor. Version becomes `1.4.0`.
- User-facing text is Chinese, with full-width punctuation where the product already uses it;
  modules that answer in Chinese are added to the `RUF001` per-file ignores in `pyproject.toml`.
- Secrets never enter the repository, a log line, a status line, an argument, a prompt or a test
  fixture: only environment variable *names* may be printed.
- `application/` modules must not call `datetime.now(`; time comes from `Clock`.
- The conversation path must not gain `subprocess`, `webbrowser` or `urllib`: the new process and
  HTTP code lives under `adapters/runtime/` and `cli_up.py` (a console entry point, like
  `cli_chat.py`).
- Every task ends green on `uv run ruff check .`, `uv run mypy src` and the tests named in the task.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/assistant/adapters/config/secrets_env.py` (new) | Parse, validate and load `secrets.env`; never print, never write |
| `src/assistant/adapters/runtime/daemon_process.py` (new) | Lock state, detached spawn, log rotation, HTTP readiness, SIGTERM stop |
| `src/assistant/adapters/runtime/windows_autostart.py` (new) | Render/install/status/remove the Windows Startup `.cmd` |
| `src/assistant/cli_up.py` (new) | `up` / `down` / `autostart` orchestration + pure renderers + `UpDeps` seam |
| `src/assistant/cli_chat.py` (modify) | Verb dispatch in `main()`, usage text |
| `src/assistant/cli.py`, `src/assistant/daemon/app.py`, `src/assistant/adapters/mcp/server.py` (modify) | Load `secrets.env` at startup |
| `src/assistant/store/mobile_sessions.py` (modify) | Prune expired/consumed pairing tokens when minting |
| `src/assistant/adapters/web/static/chat.js` (modify) | Read `#pair=<token>`, redeem on 401, strip fragment |
| `docs/adr/0046-one-command-startup.md`, `README.md`, `README_en.md`, `docs/guides/getting-started.md`, `docs/guides/mobile.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/releases/1.4.0.md` | Release documentation |

---

### Task 1: The user-owned secrets file

**Files:**
- Create: `src/assistant/adapters/config/secrets_env.py`
- Create: `tests/unit/test_secrets_env.py`
- Modify: `pyproject.toml` (add `"src/assistant/adapters/config/secrets_env.py" = ["RUF001"]` to
  `[tool.ruff.lint.per-file-ignores]`)

**Interfaces:**
- Produces: `secrets_env_path(config_root: Path | None = None) -> Path`,
  `SecretEnvStatus`, `SecretEnvResult(path, status, names, applied, reason)`,
  `load_secret_environment(path=None, environ=None) -> SecretEnvResult`,
  `load_secret_environment_into_process() -> SecretEnvResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_secrets_env.py
"""The user-owned secrets file (ADR-0046). Nothing here ever prints a value."""

from __future__ import annotations

from pathlib import Path

from assistant.adapters.config.secrets_env import (
    SecretEnvStatus,
    load_secret_environment,
    secrets_env_path,
)


def _write(path: Path, body: str, mode: int = 0o600) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_missing_file_is_reported_not_created(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"

    result = load_secret_environment(path, environ={})

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

    result = load_secret_environment(path, environ=environ)

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
    assert environ["DEEPSEEK_API_KEY"] == "from-env"  # environment wins
    assert environ["GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD"] == "quoted value"
    assert environ["GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD"] == "spaced"


def test_loose_permissions_refuse_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "DEEPSEEK_API_KEY=x\n", mode=0o644)
    environ: dict[str, str] = {}

    result = load_secret_environment(path, environ=environ)

    assert result.status is SecretEnvStatus.REFUSED
    assert "0600" in (result.reason or "")
    assert environ == {}


def test_an_unknown_key_refuses_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "DEEPSEEK_API_KEY=x\npassword=y\n")
    environ: dict[str, str] = {}

    result = load_secret_environment(path, environ=environ)

    assert result.status is SecretEnvStatus.REFUSED
    assert "password" in (result.reason or "")
    assert environ == {}  # nothing is half-loaded


def test_an_empty_value_refuses_the_whole_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "secrets.env", "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD=\n")

    result = load_secret_environment(path, environ={})

    assert result.status is SecretEnvStatus.REFUSED
    assert "空" in (result.reason or "")


def test_the_path_is_the_xdg_config_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert secrets_env_path() == tmp_path / "config" / "growing-assistant" / "secrets.env"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_secrets_env.py -q`
Expected: collection error — `ModuleNotFoundError: assistant.adapters.config.secrets_env`

- [ ] **Step 3: Write minimal implementation**

```python
# src/assistant/adapters/config/secrets_env.py
"""The user-owned secrets file: an env file the person writes, not a key store (ADR-0046).

Credentials in this project come from the environment. Typing two `export`s into every new shell
before starting the daemon was the last manual step left in the startup path, so the entry points
now load one file the *user* owns:

```ini
# ~/.config/growing-assistant/secrets.env   (mode 0600)
DEEPSEEK_API_KEY=…
GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD=…
```

Three properties make it safe to read automatically:

* **the environment wins.** A variable already set is never overwritten, so a one-off `export`
  still overrides the file;
* **the vocabulary is closed.** Only `DEEPSEEK_API_KEY` and the derived
  `GROWING_ASSISTANT_MAIL_<ID>_PASSWORD|_SMTP_PASSWORD` names are accepted; a single other key
  refuses the whole file, so it cannot quietly become a general secret store;
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

from assistant.application.paths import AppPaths

LOGGER = logging.getLogger("assistant.secrets")

SECRETS_FILENAME = "secrets.env"
"""The file's name under the XDG configuration directory."""

_EXACT_KEYS = frozenset({"DEEPSEEK_API_KEY"})
_MAIL_KEY = re.compile(r"^GROWING_ASSISTANT_MAIL_[A-Z0-9_]+_(?:SMTP_)?PASSWORD$")


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
        """Whether the file was usable at all."""
        return self.status is not SecretEnvStatus.REFUSED


def secrets_env_path(config_root: Path | None = None) -> Path:
    """Where the secrets file lives: beside `config.toml`, never in the repository."""
    root = Path(config_root) if config_root is not None else AppPaths.resolve().config
    return root / SECRETS_FILENAME


def _allowed(key: str) -> bool:
    return key in _EXACT_KEYS or bool(_MAIL_KEY.match(key))


def _parse(text: str) -> dict[str, str] | str:
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
        if not _allowed(key):
            return f"第 {number} 行的键 {key} 不被接受"
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
            cleaned = cleaned[1:-1]
        if not cleaned:
            return f"第 {number} 行的值为空"
        parsed[key] = cleaned
    return parsed


def load_secret_environment(
    path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> SecretEnvResult:
    """Load the user's secrets file into `environ` (the process environment by default).

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
    parsed = _parse(text)
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


def load_secret_environment_into_process() -> SecretEnvResult:
    """Load the secrets file for a real entry point, logging names only.

    The log line is the *only* place this is mentioned, it carries no value, and it goes through
    the logger — never stdout, because `growing-assistant-mcp` speaks a stdio protocol there.
    """
    result = load_secret_environment()
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_secrets_env.py -q && uv run ruff check . && uv run mypy src`
Expected: PASS, `All checks passed!`, `Success: no issues found`

- [ ] **Step 5: Commit**

```bash
git add src/assistant/adapters/config/secrets_env.py tests/unit/test_secrets_env.py pyproject.toml
git commit -m "feat(config): load a user-owned secrets env file"
```

---

### Task 2: Pairing tokens stop accumulating

`rings up` mints one token per launch (Task 5), so the table would otherwise grow with the number
of launches rather than with the 10-minute window it describes.

**Files:**
- Modify: `src/assistant/store/mobile_sessions.py:88-104` (`_add_pairing_token_sync`)
- Modify: `tests/integration/test_mobile_auth.py` (append one test)

**Interfaces:**
- Consumes: `SqliteMobileSessionRepository.add_pairing_token` (unchanged signature).
- Produces: the same signature; the side effect is that tokens which are already consumed or
  already expired are deleted inside the same transaction.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mobile_auth.py  (append at the end of the file)
async def test_minting_a_pairing_code_prunes_the_spent_ones(
    repository: SqliteMobileSessionRepository, clock: FakeClock
) -> None:
    """The token table is bounded by the TTL, not by how often the launcher ran."""
    first = await repository.add_pairing_token(_pairing_at(clock.now()))
    second = await repository.add_pairing_token(_pairing_at(clock.now()))
    consumed = await repository.consume_pairing_and_add_session(
        token_hash=second.token_hash, session=_session_at(clock.now()), now=clock.now()
    )

    clock.advance(seconds=PAIRING_TTL_SECONDS + 1)
    third = await repository.add_pairing_token(_pairing_at(clock.now()))

    stored = {token.id for token in await repository.list_pairing_tokens()}
    assert stored == {third.id}
    assert consumed.session.id is not None
```

`tests/integration/test_mobile_auth.py` already has the fixtures and helper builders for pairing
tokens and sessions; reuse them (`_pairing_at`, `_session_at` may be named differently in that file —
use the builders already defined there, and `PAIRING_TTL_SECONDS` from `assistant.domain.mobile`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mobile_auth.py -q -k prunes`
Expected: FAIL — the expired `first` token and the consumed `second` token are still listed.

- [ ] **Step 3: Write minimal implementation**

```python
    def _add_pairing_token_sync(self, pairing: MobilePairingToken) -> MobilePairingToken:
        try:
            with self._database.connect() as connection, transaction(connection):
                # A pairing code is a 10-minute capability, not history: once it is spent or past
                # its window it has no reader, and `rings up` mints one per launch. Pruning here
                # keeps the table's size a function of the TTL instead of the number of launches.
                connection.execute(
                    "DELETE FROM mobile_pairing_tokens "
                    "WHERE consumed_at IS NOT NULL OR expires_at <= ?",
                    (to_utc_iso(pairing.created_at),),
                )
                connection.execute(
                    f"INSERT INTO mobile_pairing_tokens ({_PAIRING_FIELDS}) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (
                        str(pairing.id),
                        pairing.token_hash,
                        to_utc_iso(pairing.created_at),
                        to_utc_iso(pairing.expires_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommitmentStoreError(f"could not store a pairing token: {exc}") from exc
        return pairing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_mobile_auth.py tests/integration/test_mobile_web.py -q`
Expected: PASS (the existing pairing tests keep passing: a live, unconsumed token is never pruned).

- [ ] **Step 5: Commit**

```bash
git add src/assistant/store/mobile_sessions.py tests/integration/test_mobile_auth.py
git commit -m "fix(mobile): prune spent pairing tokens when minting a new one"
```

---

### Task 3: The chat page redeems a fragment pairing token

**Files:**
- Modify: `src/assistant/adapters/web/static/chat.js` (`start()`, `reportStartupFailure()`)
- Modify: `tests/e2e/test_chat_browser.py` (append one browser test)
- Modify: `tests/integration/test_chat_web.py` (append one static test)

**Interfaces:**
- Consumes: the existing `POST /api/pair` route (`{"token": …}` → sets both cookies) and the
  existing `api()` helper in `chat.js`.
- Produces: `readPairFragment()` and `clearPairFragment()` inside `chat.js` (module-local, like every
  other helper there).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_chat_web.py  (append)
def test_the_chat_page_redeems_a_fragment_pairing_token(repo_root: Path) -> None:
    """The auto-pair contract lives in the page: read the fragment, strip it, redeem once."""
    script = (repo_root / "src/assistant/adapters/web/static/chat.js").read_text(encoding="utf-8")

    assert "readPairFragment" in script
    assert "clearPairFragment" in script
    assert "history.replaceState" in script
    # The token is a capability that must never travel in a query string.
    assert "?pair=" not in script
    # Nothing user-controlled may become markup.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in script
```

```python
# tests/e2e/test_chat_browser.py  (append)
@requires_browser
def test_a_fragment_link_pairs_the_browser_without_typing_a_code(tmp_path: Path) -> None:
    """`rings up` opens this URL; a browser with no cookies must land in the chat."""
    from playwright.sync_api import sync_playwright

    stack = asyncio.run(build_chat(tmp_path))
    issued = asyncio.run(stack.mobile.auth.create_pairing_token())
    with _Server(build_app(stack.dependencies(), is_private=is_private_client)) as server:
        base = f"http://127.0.0.1:{server.port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 420, "height": 780})
            page = context.new_page()
            page.goto(f"{base}/chat#pair={issued.token}")
            page.wait_for_selector("#composer", timeout=UI_TIMEOUT_MS)
            page.wait_for_function(
                "() => document.querySelectorAll('.card, #messages .message').length >= 0",
                timeout=UI_TIMEOUT_MS,
            )
            assert "#pair=" not in page.url  # the fragment is gone before anything else happens
            sessions = asyncio.run(stack.mobile.auth.list_sessions(include_inactive=False))
            assert len(sessions) == 1
            # A second visit is already paired and must not mint another session.
            page.goto(f"{base}/chat#pair={issued.token}")
            page.wait_for_selector("#composer", timeout=UI_TIMEOUT_MS)
            sessions = asyncio.run(stack.mobile.auth.list_sessions(include_inactive=False))
            assert len(sessions) == 1
            browser.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_chat_web.py -q -k fragment && uv run pytest tests/e2e/test_chat_browser.py -q -k fragment`
Expected: FAIL — `readPairFragment` does not exist (the static test fails immediately; the browser
test lands on a page that says it is not paired).

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/assistant/adapters/web/static/chat.js
  function start() {
    // `rings up` opens /chat#pair=<token>. The fragment never reaches the server, so it is read
    // here, saved for the 401 path below, and removed before anything else can observe it.
    var pairToken = readPairFragment();
    clearPairFragment();
    loadBootstrap().catch(function (error) {
      reportStartupFailure(error, pairToken);
    });
  }

  function readPairFragment() {
    var raw = window.location.hash || "";
    var match = /(?:^#|&)pair=([^&]+)/.exec(raw);
    if (!match) {
      return "";
    }
    try {
      return decodeURIComponent(match[1]);
    } catch (error) {
      return "";
    }
  }

  function clearPairFragment() {
    if (window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }

  function reportStartupFailure(error, pairToken) {
    if (error && error.status === 401) {
      if (pairToken) {
        redeemPairToken(pairToken);
        return;
      }
      if (hasSessionCookie()) {
        showPairing("配对状态已失效，请重新配对。", "重新配对");
      } else {
        showPairing("这个浏览器还没有与 Rings 配对。", "去配对");
      }
      return;
    }
    setConnection("无法连接主机，正在重试…", "offline");
  }

  /* The token is a one-time, 600 s capability, exactly like the approval link: it is redeemed
   * through the route that already exists, and a failure is reported rather than retried. */
  function redeemPairToken(token) {
    setConnection("正在配对这个浏览器…", "connecting");
    api("POST", "/api/pair", { token: token }).then(
      function () {
        window.location.replace(window.location.pathname);
      },
      function () {
        showPairing("这次自动配对没有成功（配对码可能已过期），请重新配对。", "去配对");
      }
    );
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_chat_web.py -q && uv run pytest tests/e2e/test_chat_browser.py -q`
Expected: PASS (the browser test is skipped when the host has no Chromium, as today).

- [ ] **Step 5: Commit**

```bash
git add src/assistant/adapters/web/static/chat.js tests/e2e/test_chat_browser.py tests/integration/test_chat_web.py
git commit -m "feat(web): pair a browser from a fragment link"
```

---

### Task 4: The daemon-process adapter

**Files:**
- Create: `src/assistant/adapters/runtime/daemon_process.py`
- Create: `tests/unit/test_daemon_process.py`

**Interfaces:**
- Consumes: `inspect_lock`, `daemon_lock_path` from `assistant.adapters.runtime.instance_lock`;
  `ensure_private_directory`, `ensure_private_file` from `assistant.adapters.runtime.permissions`.
- Produces: `DaemonState(running, pid, version, started_at)`, `StopOutcome(stopped, pid)`,
  `daemon_state(runtime_root) -> DaemonState`, `daemon_log_path(runtime_root) -> Path`,
  `rotate_log(path, *, limit_bytes=LOG_LIMIT_BYTES) -> bool`, `assistantd_argv() -> list[str]`,
  `spawn_daemon(runtime_root, *, environ=None) -> int`, `probe_http(url, *, timeout_seconds=0.5)
  -> bool`, `stop_daemon(runtime_root, *, timeout_seconds=15.0, sleep=time.sleep,
  monotonic=time.monotonic) -> StopOutcome`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_daemon_process.py
"""The process side of the launcher, tested without starting a daemon (ADR-0046)."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from assistant.adapters.runtime.daemon_process import (
    DaemonState,
    StopOutcome,
    assistantd_argv,
    daemon_log_path,
    daemon_state,
    rotate_log,
    stop_daemon,
)
from assistant.adapters.runtime.instance_lock import daemon_lock_path


def test_no_lock_file_means_not_running_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)

    state = daemon_state(root)

    assert state == DaemonState(running=False)
    assert daemon_lock_path(root).exists() is False


def test_a_held_lock_reports_the_holder(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    lock_path = daemon_lock_path(root)
    lock_path.write_text(
        json.dumps(
            {"pid": os.getpid(), "version": "1.4.0", "started_at": "2026-09-22T10:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    handle = os.open(lock_path, os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        state = daemon_state(root)
    finally:
        os.close(handle)

    assert state.running is True
    assert state.pid == os.getpid()
    assert state.version == "1.4.0"
    assert state.started_at == "2026-09-22T10:00:00+00:00"


def test_stop_daemon_reports_nothing_running(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)

    assert stop_daemon(root) == StopOutcome(stopped=False, pid=None)


def test_stop_daemon_signals_the_holder_and_waits_for_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "assistant.adapters.runtime.daemon_process.daemon_state",
        lambda _root: DaemonState(running=True, pid=4242),
    )
    monkeypatch.setattr(
        "assistant.adapters.runtime.daemon_process.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    outcome = stop_daemon(root, timeout_seconds=1.0, sleep=lambda _s: None)

    assert signals == [(4242, 15)]
    assert outcome == StopOutcome(stopped=False, pid=4242)  # it never became free in the window


def test_rotation_keeps_one_previous_log(tmp_path: Path) -> None:
    log = daemon_log_path(tmp_path / "runtime")
    log.parent.mkdir(parents=True)
    log.write_text("old\n", encoding="utf-8")

    assert rotate_log(log, limit_bytes=1) is True
    assert log.exists() is False
    assert (log.parent / "assistantd.log.1").read_text(encoding="utf-8") == "old\n"

    log.write_text("small\n", encoding="utf-8")
    assert rotate_log(log, limit_bytes=10_000) is False
    assert log.read_text(encoding="utf-8") == "small\n"


def test_the_command_is_the_console_script_when_this_environment_has_one() -> None:
    argv = assistantd_argv()

    assert argv
    assert "assistantd" in argv[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_daemon_process.py -q`
Expected: collection error — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# src/assistant/adapters/runtime/daemon_process.py
"""Starting, waiting for and stopping the daemon process (ADR-0046).

`rings up` and `rings down` need four process-level answers, and nothing else in the project does:
is a daemon running for this runtime root, start one detached, wait until its control plane
answers, and stop one. They live here — an adapter, like the instance lock and the permission
helpers next to it — because they touch the process table, the socket layer and the log file.

Nothing here guesses: "is it running" comes from the same `flock` probe the daemon itself uses, and
the pid that gets signalled is the pid that holds the lock.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from assistant.adapters.runtime.instance_lock import daemon_lock_path, inspect_lock
from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)

LOG_FILENAME = "assistantd.log"
LOG_LIMIT_BYTES = 5 * 1024 * 1024
"""One previous log is kept; a daemon log is diagnostic, not an archive."""

READINESS_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class DaemonState:
    """What the lock says about the daemon, taken from the live holder's own metadata."""

    running: bool
    pid: int | None = None
    version: str | None = None
    started_at: str | None = None


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """Whether the daemon actually stopped, and which pid was asked."""

    stopped: bool
    pid: int | None


def daemon_state(runtime_root: Path) -> DaemonState:
    """Whether a live daemon holds this runtime root's lock, and what it reported about itself."""
    state = inspect_lock(daemon_lock_path(Path(runtime_root)))
    if not state.held:
        return DaemonState(running=False)
    metadata = state.metadata
    pid = metadata.get("pid")
    return DaemonState(
        running=True,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        version=_text(metadata.get("version")),
        started_at=_text(metadata.get("started_at")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def daemon_log_path(runtime_root: Path) -> Path:
    """Where a detached daemon's output goes: the runtime directory, owner-only."""
    return Path(runtime_root) / LOG_FILENAME


def rotate_log(path: Path, *, limit_bytes: int = LOG_LIMIT_BYTES) -> bool:
    """Rename an oversized log to `<name>.1` once per start; return whether it rotated."""
    if not path.is_file() or path.stat().st_size <= limit_bytes:
        return False
    rotated = path.with_name(f"{path.name}.1")
    rotated.unlink(missing_ok=True)
    path.replace(rotated)
    ensure_private_file(rotated)
    return True


def assistantd_argv() -> list[str]:
    """How to start this installation's daemon: its console script, or the module."""
    script = Path(sys.executable).parent / "assistantd"
    if script.is_file():
        return [str(script)]
    found = shutil.which("assistantd")
    if found:
        return [found]
    return [sys.executable, "-c", "from assistant.daemon import main; main()"]


def spawn_daemon(
    runtime_root: Path, *, environ: Mapping[str, str] | None = None
) -> int:
    """Start the daemon detached, logging into the runtime directory. Returns its pid.

    It is detached on purpose: closing the terminal that ran `rings up` must not stop Rings, and
    the child is the only long-lived holder of the instance lock.
    """
    root = Path(runtime_root)
    ensure_private_directory(root)
    log_path = daemon_log_path(root)
    rotate_log(log_path)
    handle = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        ensure_private_file(log_path)
        process = subprocess.Popen(
            assistantd_argv(),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            cwd=str(Path.cwd()),
            env=dict(os.environ if environ is None else environ),
        )
    finally:
        os.close(handle)
    return process.pid


def probe_http(url: str, *, timeout_seconds: float = 0.5) -> bool:
    """Whether the control plane answered *anything* at `url`.

    A `401` is a perfectly good answer: it proves the server is listening and merely does not know
    this browser yet. Only a connection-level failure counts as "not ready".
    """
    try:
        urllib.request.urlopen(url, timeout=timeout_seconds)
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def stop_daemon(
    runtime_root: Path,
    *,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StopOutcome:
    """Ask the lock holder to stop, and wait for the lock to be free.

    `SIGTERM` is the same signal the daemon's own shutdown path already handles: services stop, the
    lock is released, and a turn that was mid-flight is recorded as interrupted rather than
    replayed. Nothing is killed forcefully here.
    """
    state = daemon_state(runtime_root)
    if not state.running or state.pid is None:
        return StopOutcome(stopped=False, pid=None)
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:  # pragma: no cover - it exited between the probe and the signal
        return StopOutcome(stopped=True, pid=state.pid)
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if not daemon_state(runtime_root).running:
            return StopOutcome(stopped=True, pid=state.pid)
        sleep(0.2)
    return StopOutcome(stopped=False, pid=state.pid)


__all__ = [
    "LOG_FILENAME",
    "LOG_LIMIT_BYTES",
    "READINESS_TIMEOUT_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "DaemonState",
    "StopOutcome",
    "assistantd_argv",
    "daemon_log_path",
    "daemon_state",
    "probe_http",
    "rotate_log",
    "spawn_daemon",
    "stop_daemon",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_daemon_process.py -q && uv run ruff check . && uv run mypy src`
Expected: PASS. (Add `"src/assistant/adapters/runtime/daemon_process.py" = ["RUF001"]` to
`pyproject.toml` only if ruff flags the Chinese strings in the docstrings.)

- [ ] **Step 5: Commit**

```bash
git add src/assistant/adapters/runtime/daemon_process.py tests/unit/test_daemon_process.py
git commit -m "feat(runtime): start, probe and stop the daemon process"
```

---

### Task 5: `rings up` and `rings down`

**Files:**
- Create: `src/assistant/cli_up.py`
- Create: `tests/unit/test_cli_up.py`
- Create: `tests/integration/test_rings_up_down.py`
- Modify: `src/assistant/cli_chat.py` (dispatch + usage)
- Modify: `src/assistant/cli.py`, `src/assistant/daemon/app.py`,
  `src/assistant/adapters/mcp/server.py` (load the secrets file at startup)
- Modify: `pyproject.toml` (RUF001 ignore for `cli_up.py`)

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: `UpDeps` (the injectable seam), `UpReport`, `render_up(report) -> str`,
  `run_up(*, deps=None, foreground=False, open_browser=True) -> int`,
  `run_down(*, deps=None) -> int`, `main(arguments: Sequence[str]) -> int | None` in `cli_up.py`.

- [ ] **Step 1: Write the failing unit test**

```python
# tests/unit/test_cli_up.py
"""`rings up` / `rings down` decision logic, with every side effect injected (ADR-0046)."""

from __future__ import annotations

from pathlib import Path

from assistant.adapters.config.secrets_env import SecretEnvResult, SecretEnvStatus
from assistant.adapters.runtime.daemon_process import DaemonState, StopOutcome
from assistant.cli_up import (
    EHallSummary,
    MailSummary,
    UpDeps,
    UpReport,
    render_up,
    run_down,
    run_up,
)


class _Fake:
    """One scripted dependency set: no process, no socket, no browser."""

    def __init__(self, *, running: bool = False, web: str | None = "http://127.0.0.1:8765/chat"):
        self.running = running
        self.web = web
        self.states: list[DaemonState] = []
        self.spawned = 0
        self.probes: list[str] = []
        self.opened: list[str] = []
        self.minted = 0
        self.signalled = 0
        self.slept = 0.0
        self.now = 0.0

    def deps(self, runtime_root: Path) -> UpDeps:
        def state(_root: Path) -> DaemonState:
            self.states.append(
                DaemonState(running=self.running, pid=4242 if self.running else None, version="1.4.0")
            )
            return self.states[-1]

        def spawn(_root: Path) -> int:
            self.spawned += 1
            self.running = True
            return 4242

        def probe(url: str) -> bool:
            self.probes.append(url)
            return self.running

        async def mint():
            self.minted += 1
            return type("Issued", (), {"token": "token-value"})()

        async def load_config():
            return None

        def secrets() -> SecretEnvResult:
            return SecretEnvResult(path=runtime_root / "secrets.env", status=SecretEnvStatus.MISSING)

        def monotonic() -> float:
            self.now += 1.0
            return self.now

        return UpDeps(
            load_config=load_config,
            daemon_state=state,
            spawn_daemon=spawn,
            probe_http=probe,
            mint_pairing_token=mint,
            open_browser=lambda url: (self.opened.append(url), True)[1],
            load_secrets=secrets,
            runtime_root=lambda: runtime_root,
            web_url=lambda _config: self.web,
            mail_summary=_mail,
            ehall_summary=_ehall,
            sleep=lambda seconds: setattr(self, "slept", self.slept + seconds),
            monotonic=monotonic,
        )


async def _mail(_config: object) -> MailSummary:
    return MailSummary(accounts=1, ready=1, stored=6)


def _ehall(_config: object) -> EHallSummary:
    return EHallSummary(enabled=True, profile_present=True)


def test_up_starts_pairs_and_opens(tmp_path: Path) -> None:
    fake = _Fake()

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.spawned == 1
    assert fake.minted == 1
    assert fake.opened == ["http://127.0.0.1:8765/chat#pair=token-value"]
    assert report.daemon_action == "started"
    text = render_up(report)
    assert "已启动" in text
    assert "配对" in text
    assert "token-value" not in text  # the token never reaches the status block


def test_up_reuses_a_running_daemon_and_still_opens_the_chat(tmp_path: Path) -> None:
    fake = _Fake(running=True)

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.spawned == 0
    assert report.daemon_action == "reused"
    assert fake.opened  # the browser is opened either way


def test_up_without_a_control_plane_says_so_and_opens_nothing(tmp_path: Path) -> None:
    fake = _Fake(web=None)

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.opened == []
    assert report.web_url is None
    assert "[mobile]" in render_up(report)


def test_down_reports_when_nothing_was_running(tmp_path: Path) -> None:
    fake = _Fake()

    assert run_down(deps=fake.deps(tmp_path)) == 0


def test_the_status_block_never_prints_a_secret_name_value(tmp_path: Path) -> None:
    report = UpReport(
        daemon_action="started",
        pid=4242,
        version="1.4.0",
        web_url="http://127.0.0.1:8765/chat",
        paired=True,
        secrets=SecretEnvResult(
            path=tmp_path / "secrets.env",
            status=SecretEnvStatus.LOADED,
            names=("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",),
            applied=("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",),
        ),
        mail=MailSummary(accounts=1, ready=1, stored=6),
        ehall=EHallSummary(enabled=True, profile_present=False),
        note=None,
    )

    text = render_up(report)

    assert "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD" in text  # the name is the useful part
    assert "未登录" in text or "pw ehall login" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_up.py -q`
Expected: collection error — `assistant.cli_up` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# src/assistant/cli_up.py
"""`rings up` / `rings down` / `rings autostart`: the one-command startup path (ADR-0046).

The whole file is two halves, kept apart on purpose:

* **decision logic** — `run_up`, `run_down`, `run_autostart` — which takes an `UpDeps` of callables
  and is therefore testable without a process, a socket or a browser;
* **rendering** — `render_up`, `render_down` — pure functions over the reports above, because the
  sentences a person reads are part of the product and deserve their own tests.

`default_deps()` is the only place that touches the real world.
"""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant import bootstrap
from assistant.adapters.config.secrets_env import (
    SecretEnvResult,
    SecretEnvStatus,
    load_secret_environment_into_process,
)
from assistant.adapters.ehall.session import session_state
from assistant.adapters.runtime.daemon_process import (
    READINESS_TIMEOUT_SECONDS,
    DaemonState,
    daemon_state,
    probe_http,
    spawn_daemon,
    stop_daemon,
)
from assistant.application.paths import AppPaths

from assistant.cli_support import console


@dataclass(frozen=True, slots=True)
class MailSummary:
    """What the status block may say about mail: counts and readiness, never content."""

    accounts: int
    ready: int
    stored: int


@dataclass(frozen=True, slots=True)
class EHallSummary:
    """The eHall pipeline's own state, as far as a local check can tell."""

    enabled: bool
    profile_present: bool


@dataclass(frozen=True, slots=True)
class UpReport:
    """Everything `rings up` learned, in the shape the renderer wants."""

    daemon_action: str
    pid: int | None
    version: str | None
    web_url: str | None
    paired: bool
    secrets: SecretEnvResult
    mail: MailSummary
    ehall: EHallSummary
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UpDeps:
    """Every side effect `rings up` performs, as a callable."""

    load_config: Callable[[], Awaitable[Any]]
    daemon_state: Callable[[Path], DaemonState]
    spawn_daemon: Callable[[Path], int]
    probe_http: Callable[[str], bool]
    mint_pairing_token: Callable[[], Awaitable[Any]]
    open_browser: Callable[[str], bool]
    load_secrets: Callable[[], SecretEnvResult]
    runtime_root: Callable[[], Path]
    web_url: Callable[[Any], str | None]
    mail_summary: Callable[[Any], Awaitable[MailSummary]]
    ehall_summary: Callable[[Any], EHallSummary]
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]


def render_up(report: UpReport) -> str:
    """The status block: six honest lines, and never a secret or a token."""
    daemon = (
        f"已启动（pid {report.pid}，{report.version}）"
        if report.daemon_action == "started"
        else f"已在运行（pid {report.pid}，{report.version}）"
        if report.daemon_action == "reused"
        else "前台运行中"
    )
    web = report.web_url or "未启用"
    lines = [
        f"daemon        {daemon}",
        f"web           {web}",
        f"配对          {'本次已自动配对' if report.paired else '未执行'}",
    ]
    if report.secrets.status is SecretEnvStatus.REFUSED:
        lines.append(f"凭据          {report.secrets.reason}")
    elif report.secrets.status is SecretEnvStatus.LOADED:
        lines.append(
            f"凭据          从 {report.secrets.path.name} 读取了 {len(report.secrets.names)} 项"
        )
    else:
        lines.append("凭据          未配置（可选：~/.config/growing-assistant/secrets.env）")
    lines.append(
        f"邮件          {report.mail.accounts} 个账号，{report.mail.ready} 个凭据就位，"
        f"已存 {report.mail.stored} 封"
    )
    if not report.ehall.enabled:
        lines.append("eHall         未启用（[ehall] enabled = false）")
    elif report.ehall.profile_present:
        lines.append("eHall         profile 已存在；只有实时检查才知道登录是否仍然有效")
    else:
        lines.append("eHall         还没有登录，运行 `uv run pw ehall login` 自己登录一次")
    if report.web_url is None:
        lines.append("")
        lines.append("要在浏览器里用，请在配置里启用 [mobile]（enabled = true, bind = \"loopback\"）。")
        lines.append("终端对话可以直接用：`rings`。")
    if report.note:
        lines.append(report.note)
    return "\n".join(lines)


def run_up(
    *, deps: UpDeps | None = None, foreground: bool = False, open_browser: bool = True
) -> UpReport:
    """Start or reuse the daemon, pair this browser and open `/chat`."""
    return asyncio.run(_run_up(deps=deps, foreground=foreground, open_browser=open_browser))


async def _run_up(
    *, deps: UpDeps | None, foreground: bool, open_browser: bool
) -> UpReport:
    resolved = deps or default_deps()
    root = resolved.runtime_root()
    secrets = resolved.load_secrets()
    config = await resolved.load_config()
    state = resolved.daemon_state(root)
    action = "reused" if state.running else "started"
    if not state.running and not foreground:
        resolved.spawn_daemon(root)
        state = _wait_for_daemon(resolved, root, url=resolved.web_url(config))
        action = "started"
    url = resolved.web_url(config)
    paired = False
    if url is not None and not foreground:
        issued = await resolved.mint_pairing_token()
        if open_browser:
            resolved.open_browser(f"{url}#pair={issued.token}")
        paired = True
    note = None
    if foreground:
        note = "本次为前台运行，不自动配对；已配对过的浏览器可直接打开上面的地址。"
    return UpReport(
        daemon_action=action,
        pid=state.pid,
        version=state.version,
        web_url=url,
        paired=paired,
        secrets=secrets,
        mail=await resolved.mail_summary(config),
        ehall=resolved.ehall_summary(config),
        note=note,
    )


def _wait_for_daemon(deps: UpDeps, root: Path, *, url: str | None) -> DaemonState:
    """Wait for the lock (and, when there is one, the control plane) to be ready."""
    deadline = deps.monotonic() + READINESS_TIMEOUT_SECONDS
    while deps.monotonic() < deadline:
        state = deps.daemon_state(root)
        if state.running and (url is None or deps.probe_http(url)):
            return state
        deps.sleep(0.25)
    raise TimeoutError(f"assistantd 没有在限定时间内就绪；看日志：{daemon_log_path(root)}")


def run_down(*, deps: UpDeps | None = None) -> int:
    """Stop the daemon, or say that there was nothing to stop."""
    resolved = deps or default_deps()
    outcome = stop_daemon(resolved.runtime_root())
    if outcome.pid is None:
        console.print("没有在运行。")
        return 0
    if outcome.stopped:
        console.print(f"已停止（pid {outcome.pid}）。进行中的对话会被标记为 interrupted，不会重放。")
        return 0
    console.print(f"还没有停下来（pid {outcome.pid} 仍在运行），可以再看一眼日志再试。")
    return 1


def default_deps() -> UpDeps:
    """The real world: bootstrap services, the process adapter, the browser and the clock."""
    ...


def main(arguments: Sequence[str]) -> int:
    """Dispatch `rings up|down|autostart …`, or explain the usage."""
    ...
```

Fill `default_deps()` and `main()` with the real wiring: `default_deps()` builds `bootstrap`-based
callables (`bootstrap.config_loader().load`, `bootstrap.mobile_auth_service(...).create_pairing_token`,
`bootstrap.runtime_database(...).count_messages()` for the mail summary, `session_state()` for eHall,
`webbrowser.open` for the browser, `AppPaths.resolve().runtime` for the root, and `web_chat_url` from
`cli_chat` — import it, do not copy it). `main()` parses exactly these forms and returns
`0`/`1`/`2`:

```text
up [--foreground] [--no-open]
down
autostart install|status|remove
```

- [ ] **Step 4: Wire the dispatch and the entry points**

```python
# src/assistant/cli_chat.py  (in main())
    arguments = sys.argv[1:]
    if not arguments:
        load_secret_environment_into_process()
        raise SystemExit(run_conversation(announce="Rings — 本地个人运营系统"))
    if arguments == [WEB_FLAG]:
        load_secret_environment_into_process()
        raise SystemExit(open_web_chat())
    if arguments[0] in {"up", "down", "autostart"}:
        raise SystemExit(cli_up.main(arguments))
    error_console.print(f"无法识别的参数：{' '.join(arguments)}")
    error_console.print(
        "用法：rings [--web] | rings up [--foreground] [--no-open] | rings down | "
        "rings autostart install|status|remove"
    )
    raise SystemExit(2)
```

Call `load_secret_environment_into_process()` at the top of `assistant.cli:main`,
`assistant.daemon:main` and `assistant.adapters.mcp.server:main` as well.

- [ ] **Step 5: Write the real-process integration test**

```python
# tests/integration/test_rings_up_down.py
"""`rings up` / `rings down` against a real daemon in a temporary XDG root (ADR-0046).

The config has no control plane, so nothing in this module needs a socket: readiness is the lock.
The real HTTP readiness probe and the auto-pair URL are covered by the browser module, which is
already allowed loopback.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from assistant.adapters.runtime.daemon_process import daemon_state

CONFIG = """format_version = 1

[indexing]
interval_seconds = 3600
run_on_startup = false

[scheduler]
poll_interval_seconds = 3600
replan_debounce_seconds = 60
"""


def _environment(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "growing-assistant"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG, encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    script = Path(sys.executable).parent / "rings"
    command = [str(script)] if script.is_file() else [sys.executable, "-m", "assistant.cli_chat"]
    return subprocess.run(
        [*command, *arguments],
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_up_starts_a_daemon_and_down_stops_it(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"

    up = _run(tmp_path, "up", "--no-open")
    try:
        assert up.returncode == 0, up.stderr
        assert "已启动" in up.stdout
        assert daemon_state(root).running is True
        assert (root / "assistantd.log").is_file()
    finally:
        down = _run(tmp_path, "down")

    assert down.returncode == 0, down.stderr
    assert "已停止" in down.stdout
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and daemon_state(root).running:
        time.sleep(0.2)
    assert daemon_state(root).running is False


def test_down_without_a_daemon_is_not_an_error(tmp_path: Path) -> None:
    down = _run(tmp_path, "down")

    assert down.returncode == 0
    assert "没有在运行" in down.stdout
```

- [ ] **Step 6: Run everything and commit**

Run: `uv run pytest tests/unit/test_cli_up.py tests/integration/test_rings_up_down.py tests/unit/test_cli_chat.py -q`
Expected: PASS. If `tests/unit/test_cli_chat.py` asserts the old usage line, update that assertion in
the same commit (the usage text genuinely changed).

```bash
git add src/assistant/cli_up.py src/assistant/cli_chat.py src/assistant/cli.py \
        src/assistant/daemon/app.py src/assistant/adapters/mcp/server.py \
        tests/unit/test_cli_up.py tests/integration/test_rings_up_down.py pyproject.toml
git commit -m "feat(cli): add rings up and rings down"
```

---

### Task 6: Windows logon autostart

**Files:**
- Create: `src/assistant/adapters/runtime/windows_autostart.py`
- Create: `tests/unit/test_windows_autostart.py`
- Modify: `src/assistant/cli_up.py` (`run_autostart`, `main()` verb)
- Modify: `pyproject.toml` (RUF001 ignore for the new adapter if ruff flags its Chinese strings)

**Interfaces:**
- Produces: `AUTOSTART_FILENAME`, `AutostartSpec(distro, user, repo, runtime_root, uv)`,
  `AutostartStatus(startup_dir, path, installed, content)`,
  `render_autostart_cmd(spec) -> str`, `windows_startup_directory(*, appdata=None) -> Path | None`,
  `current_distro() -> str | None`, `current_user() -> str`,
  `install_autostart(spec, *, startup_dir) -> Path`, `autostart_status(*, startup_dir) -> AutostartStatus`,
  `remove_autostart(*, startup_dir) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_windows_autostart.py
"""The one Startup-folder file, rendered and managed without touching the real folder (ADR-0046)."""

from __future__ import annotations

from pathlib import Path

from assistant.adapters.runtime.windows_autostart import (
    AUTOSTART_FILENAME,
    AutostartSpec,
    autostart_status,
    install_autostart,
    remove_autostart,
    render_autostart_cmd,
    windows_startup_directory,
)


def _spec(tmp_path: Path, **overrides: object) -> AutostartSpec:
    values: dict[str, object] = {
        "distro": "Debian",
        "user": "mrtree",
        "repo": Path("/home/mrtree/projects/growing-assistant"),
        "runtime_root": Path("/home/mrtree/.local/share/growing-assistant"),
        "uv": "/home/mrtree/.local/bin/uv",
    }
    values.update(overrides)
    return AutostartSpec(**values)  # type: ignore[arg-type]


def test_the_command_is_one_wsl_line_with_windows_line_endings(tmp_path: Path) -> None:
    rendered = render_autostart_cmd(_spec(tmp_path))

    assert rendered.endswith("\r\n")
    assert "\r\n" in rendered and "\n\n" not in rendered.replace("\r\n", "\n\n")
    assert "wsl.exe -d Debian -u mrtree -- bash -lc" in rendered
    assert "cd '/home/mrtree/projects/growing-assistant'" in rendered
    assert "'/home/mrtree/.local/bin/uv' run assistantd" in rendered
    assert ">> '/home/mrtree/.local/share/growing-assistant/assistantd.log' 2>&1" in rendered
    # A path with a space must still survive: the quoting above is what makes that true.
    spaced = render_autostart_cmd(_spec(tmp_path, repo=Path("/home/mrtree/my projects/rings")))
    assert "cd '/home/mrtree/my projects/rings'" in spaced


def test_install_status_and_remove_round_trip(tmp_path: Path) -> None:
    startup = tmp_path / "Startup"
    spec = _spec(tmp_path)

    assert autostart_status(startup_dir=startup).installed is False

    written = install_autostart(spec, startup_dir=startup)

    assert written == startup / AUTOSTART_FILENAME
    status = autostart_status(startup_dir=startup)
    assert status.installed is True
    assert status.content == render_autostart_cmd(spec)
    assert remove_autostart(startup_dir=startup) is True
    assert autostart_status(startup_dir=startup).installed is False
    assert remove_autostart(startup_dir=startup) is False  # idempotent


def test_the_startup_directory_comes_from_appdata_roaming(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"

    resolved = windows_startup_directory(appdata=appdata)

    assert resolved == appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_windows_autostart.py -q`
Expected: collection error — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# src/assistant/adapters/runtime/windows_autostart.py
"""Starting `assistantd` when Windows logs in: one reversible file (ADR-0046).

A WSL distro has no user-level systemd here (`systemctl --user` is offline), and installing a
service would need privileges the user should not have to grant. So autostart is the same thing
Windows itself uses for "run this when I log in": a single `.cmd` in the per-user Startup folder,
which the user can inspect, keep, or delete with one command.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

AUTOSTART_FILENAME = "rings-assistantd.cmd"
"""The file's name in the Startup folder. One file, and its name says what it does."""

_STARTUP_TAIL = Path("Microsoft/Windows/Start Menu/Programs/Startup")


@dataclass(frozen=True, slots=True)
class AutostartSpec:
    """Everything the generated command needs, resolved rather than guessed."""

    distro: str
    user: str
    repo: Path
    runtime_root: Path
    uv: str


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    """What is installed right now."""

    startup_dir: Path
    path: Path
    installed: bool
    content: str | None = None


def render_autostart_cmd(spec: AutostartSpec) -> str:
    """The exact file content, with Windows line endings and quoting that survives spaces.

    The quoted strings are *Linux* paths: `bash -lc` runs inside WSL, so `cd`, `uv` and the log
    redirection are all resolved there.
    """
    command = (
        f"wsl.exe -d {spec.distro} -u {spec.user} -- bash -lc "
        f'"cd \'{spec.repo}\' && \'{spec.uv}\' run assistantd '
        f">> '{spec.runtime_root}/assistantd.log' 2>&1\""
    )
    return "\r\n".join(["@echo off", command, ""])


def windows_startup_directory(*, appdata: Path | None = None) -> Path | None:
    """The per-user Startup folder, or `None` when this is not a Windows-visible machine."""
    roaming = Path(appdata) if appdata is not None else _appdata_roaming()
    return None if roaming is None else roaming / _STARTUP_TAIL


def _appdata_roaming() -> Path | None:
    """`%APPDATA%`, translated to a WSL path, with a single-user fallback."""
    try:
        completed = subprocess.run(
            ["cmd.exe", "/c", "echo", "%APPDATA%"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        raw = completed.stdout.strip()
        if raw and "%APPDATA%" not in raw:
            try:
                translated = subprocess.run(
                    ["wslpath", "-u", raw], capture_output=True, text=True, timeout=10
                )
            except (OSError, subprocess.SubprocessError):
                translated = None
            if translated is not None and translated.returncode == 0 and translated.stdout.strip():
                return Path(translated.stdout.strip())
    candidates = sorted(Path("/mnt/c/Users").glob("*/AppData/Roaming"))
    return candidates[0] if len(candidates) == 1 else None


def current_distro() -> str | None:
    """The WSL distribution this process runs in."""
    named = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if named:
        return named
    try:
        completed = subprocess.run(
            ["wsl.exe", "-l", "-q"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return names[0] if len(names) == 1 else None


def current_user() -> str:
    """This process's user name, from the password database rather than an environment variable."""
    return pwd.getpwuid(os.getuid()).pw_name


def resolved_uv() -> str:
    """The absolute `uv` to embed, so a non-interactive login shell does not need it on PATH."""
    return shutil.which("uv") or "uv"


def autostart_status(*, startup_dir: Path) -> AutostartStatus:
    """Whether the Startup file exists, and what it says."""
    path = Path(startup_dir) / AUTOSTART_FILENAME
    if not path.is_file():
        return AutostartStatus(startup_dir=Path(startup_dir), path=path, installed=False)
    return AutostartStatus(
        startup_dir=Path(startup_dir),
        path=path,
        installed=True,
        content=path.read_text(encoding="utf-8"),
    )


def install_autostart(spec: AutostartSpec, *, startup_dir: Path) -> Path:
    """Write the Startup file. An existing file is replaced deliberately."""
    directory = Path(startup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / AUTOSTART_FILENAME
    path.write_text(render_autostart_cmd(spec), encoding="utf-8", newline="")
    return path


def remove_autostart(*, startup_dir: Path) -> bool:
    """Delete the Startup file; `False` when there was nothing to delete."""
    path = Path(startup_dir) / AUTOSTART_FILENAME
    if not path.is_file():
        return False
    path.unlink()
    return True


__all__ = [
    "AUTOSTART_FILENAME",
    "AutostartSpec",
    "AutostartStatus",
    "autostart_status",
    "current_distro",
    "current_user",
    "install_autostart",
    "remove_autostart",
    "render_autostart_cmd",
    "resolved_uv",
    "windows_startup_directory",
]
```

- [ ] **Step 4: Add the verbs to `cli_up.py`**

```python
def run_autostart(action: str, *, repo: Path | None = None) -> int:
    """`install` / `status` / `remove` the one Startup file, printing exactly what it did."""
    startup = windows_startup_directory()
    if startup is None:
        console.print("没有找到 Windows 启动文件夹（这个环境看起来不是 WSL，或者 %APPDATA% 读不到）。")
        return 1
    if action == "status":
        status = autostart_status(startup_dir=startup)
        console.print(f"启动项：{status.path}")
        console.print("状态：已安装" if status.installed else "状态：未安装")
        if status.content:
            console.print(status.content.rstrip("\r\n"))
        return 0
    if action == "remove":
        removed = remove_autostart(startup_dir=startup)
        console.print("已删除启动项。" if removed else "本来就没有安装启动项。")
        return 0
    if action != "install":
        console.print("用法：rings autostart install|status|remove")
        return 2
    checkout = Path(repo) if repo is not None else Path(bootstrap.__file__).resolve().parents[1]
    if not (checkout / "pyproject.toml").is_file():
        console.print(f"{checkout} 看起来不是 Rings 仓库；用 --repo 指定仓库路径。")
        return 2
    distro = current_distro()
    if distro is None:
        console.print("读不到 WSL 发行版名（wsl.exe 不可用），无法生成启动项。")
        return 1
    spec = AutostartSpec(
        distro=distro,
        user=current_user(),
        repo=checkout,
        runtime_root=AppPaths.resolve().runtime,
        uv=resolved_uv(),
    )
    path = install_autostart(spec, startup_dir=startup)
    console.print(f"已写入 {path}：")
    console.print(render_autostart_cmd(spec).rstrip("\r\n"))
    console.print("删掉这个文件（或 `rings autostart remove`）即可撤销。")
    return 0
```

`main()` gains `autostart install|status|remove [--repo PATH]` alongside `up`/`down`.

- [ ] **Step 5: Run everything and commit**

Run: `uv run pytest tests/unit/test_windows_autostart.py tests/unit/test_cli_up.py -q && uv run ruff check . && uv run mypy src`
Expected: PASS. Nothing in the suite installs into the real Startup folder: every test passes an
explicit `startup_dir`.

```bash
git add src/assistant/adapters/runtime/windows_autostart.py tests/unit/test_windows_autostart.py \
        src/assistant/cli_up.py pyproject.toml
git commit -m "feat(runtime): install one Windows Startup entry for the daemon"
```

---

### Task 7: ADR, documentation and the 1.4.0 release

**Files:**
- Create: `docs/adr/0046-one-command-startup.md`
- Create: `docs/releases/1.4.0.md`
- Modify: `CHANGELOG.md`, `README.md`, `README_en.md`, `docs/guides/getting-started.md`,
  `docs/guides/mobile.md`, `AGENTS.md`, `docs/specs/0002-one-command-startup.md` (Status → Accepted)
- Modify: `pyproject.toml`, `src/assistant/__init__.py`, `uv.lock`,
  `tests/release/test_version_consistency.py`, `tests/release/test_cli_and_version_surface.py`,
  `tests/release/test_daemon_single_instance.py`, `tests/release/test_readme_consistency.py`,
  `tests/release/test_readme_structure.py`

- [ ] **Step 1: Write ADR-0046**

`docs/adr/0046-one-command-startup.md`, Status **Accepted**, with the eleven decisions from
`docs/specs/0002-one-command-startup.md` §4 restated in ADR form (context → decision → consequences
→ rejected alternatives). The rejected list is the one in that spec's §8, verbatim in substance.

- [ ] **Step 2: Write the release notes and changelog**

`docs/releases/1.4.0.md` must contain the tokens the release test looks for
(`Known limitations`, `exactly-once`, `trusted LAN`, `non-executing`) and must not contain
`fully autonomous`, `automatic form filling` or `cloud sync`. Cover: `rings up` / `rings down`,
automatic pairing (with the fragment explanation), `secrets.env`, `rings autostart`, the honest
status block, what `rings down` does to an in-flight turn, and the same limitations list 1.3.1 has.

`CHANGELOG.md` gets `## [1.4.0] - 2026-09-22` with Added / Changed / Security sections.

- [ ] **Step 3: Update the documentation**

- `README.md` / `README_en.md`: replace the four-step quick start with
  ```bash
  uv run rings up          # 起服务 + 自动配对 + 打开 /chat
  uv run rings down        # 优雅停止
  uv run rings autostart install   # 可选：登录时自动常驻
  ```
  plus one short paragraph on `~/.config/growing-assistant/secrets.env` (0600, which variable names,
  environment wins) and a pointer to `docs/specs/0002-one-command-startup.md`.
- `docs/guides/getting-started.md`: a "一键启动" section with the same three commands, the secrets
  file example, and what to do when the status block says the control plane is disabled.
- `docs/guides/mobile.md`: auto-pair replaces the paste step; the manual `/pair` page stays for
  phones and other browsers; sessions are 30 days; the fragment never reaches the server.
- `AGENTS.md` (project rules) gains four bullets: credentials only from a user-owned `0600`
  `secrets.env` (the project never writes, prints or backs it up); `rings up`/`rings down` are the
  only start/stop path; auto-pairing uses the existing `/api/pair` route with the token in the URL
  fragment and adds no route; autostart is one reversible Startup `.cmd`.
- `docs/specs/0002-one-command-startup.md`: `**Status:** Accepted` (the author approved it).

- [ ] **Step 4: Bump the version everywhere it is written**

```text
pyproject.toml                              version = "1.4.0"
src/assistant/__init__.py                   __version__ = "1.4.0"
tests/release/test_version_consistency.py   VERSION = "1.4.0"  (+ its two docstrings)
tests/release/test_cli_and_version_surface.py  VERSION = "1.4.0"  (+ its docstring)
tests/release/test_daemon_single_instance.py   "version": "1.4.0" and the assertion
tests/release/test_readme_consistency.py       "1.4.0", "docs/releases/1.4.0.md"
tests/release/test_readme_structure.py         "docs/releases/1.4.0.md"
README.md, README_en.md                        version line + release-notes link
```

Then `uv lock` (the project's own version is in the lock) and confirm `uv lock --check` is clean.
`REVIEWED_MIGRATION_COUNT` stays `23`: this release adds no migration.

- [ ] **Step 5: Run the full gate**

Run:
```bash
env -u DEEPSEEK_API_KEY uv run pytest -q
TZ=UTC CI=true GITHUB_ACTIONS=true env -u DEEPSEEK_API_KEY uv run pytest -q
uv run ruff check . && uv run mypy src && uv lock --check && git diff --check
rm -rf dist && uv build
```
Then install the wheel outside the checkout, run `pw --version` / `assistantd --version`,
`rings --help`-equivalent usage check, and a `rings up --no-open` + `rings down` smoke in a temporary
XDG root.

- [ ] **Step 6: Commit, merge, push, tag**

```bash
git add -A
git commit -m "chore(release): 1.4.0"
git checkout main && git merge --ff-only <branch> && git push origin main
# wait for remote CI to go green, then:
git tag -a v1.4.0 -m "Rings v1.4.0" && git push origin v1.4.0
```

The tag is created only after remote `main` CI is green, and it is never moved afterwards.

---

## Self-Review

**Spec coverage.** §4's D1 → Task 5 (`rings` verbs); D2 → Task 4 (`inspect_lock`); D3 → Task 4
(`spawn_daemon`, `rotate_log`); D4 → Task 4 (`probe_http`) + Task 5 (`_wait_for_daemon`); D5/D6 →
Task 3 (fragment) + Task 5 (mint + open); D7 → Task 2; D8 → Task 1; D9 → Task 6; D10 →
Task 5 (`render_up`); D11 → Task 4 (`stop_daemon`) + Task 5 (`run_down`). §6's tests are the tests in
Tasks 1–6; §7 is Task 7. No spec requirement is left without a task.

**Placeholders.** Two spots in Task 5's first draft were ambiguous and are resolved here: the fake
dependency set imports `MailSummary` / `EHallSummary` from `assistant.cli_up` (the same dataclasses
the implementation exports) instead of defining stand-ins, and `_wait_for_daemon`'s timeout message
names the log file with `daemon_log_path(root)` instead of ending in a colon.

**Type consistency.** `DaemonState`, `StopOutcome`, `SecretEnvResult`, `MailSummary`,
`EHallSummary`, `UpReport` and `UpDeps` are defined once each and used with the same field names in
every task; `probe_http(url, *, timeout_seconds=0.5)`, `spawn_daemon(runtime_root, *, environ=None)`,
`rotate_log(path, *, limit_bytes=LOG_LIMIT_BYTES)` and
`stop_daemon(runtime_root, *, timeout_seconds=STOP_TIMEOUT_SECONDS, sleep=..., monotonic=...)` keep
the signatures Task 4 defines wherever later tasks call them.
