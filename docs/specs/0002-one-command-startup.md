# 0002 — One-command startup (`rings up` / `rings down` / autostart)

**Status:** Accepted — approved by the author, implemented in v1.4.0 (ADR-0046 records the
decisions).

**Related:** ADR-0026 (mobile control plane), ADR-0032 (runtime/upgrade contract), ADR-0041 (chat
UI, queue, pairing UX), ADR-0043 (mail settings, environment-only credentials), ADR-0025 (eHall
pipeline). ADR-0046 records the decisions below once implemented.

## 1. Problem

Starting Rings today takes four manual steps in three places, and two of them expire:

```text
terminal 1   uv run assistantd                 # dies with the terminal
terminal 2   rings  (or rings --web)           # the browser needs to be paired first
browser      pw mobile pair → copy a code → paste it into /pair
shell        export GROWING_ASSISTANT_MAIL_<ID>_PASSWORD=…   # and the SMTP one
browser      pw ehall login …                  # a separate manual SSO session
```

Measured on the reference host (2026-09-22, v1.3.1):

| Quantity | Measured |
| --- | --- |
| `assistantd` resident memory | 107 MB (all services in one process, 6 threads) |
| CPU while idle | ~3 s CPU per 9 min ≈ 0.5% of one core (scheduler 15 s, mail 60 s, index 300 s) |
| Runtime directory | 24 MB (db 1.2 MB, raw mail 14 MB, eHall profile 9.3 MB) |
| Web port reachable | < 1 s after start; first mail sync ~5 s |
| Recurring cost | one model call per new mail (6 mails → 6 calls, observed) |

The cost of staying resident is therefore small; the cost of *starting* is what is high, and it is
paid every time. This spec removes the manual steps without loosening the safety boundary.

## 2. Goals

- **G1** One command in a WSL terminal brings the whole product up and lands the user in `/chat`.
- **G2** One command stops it, gracefully, and says what that means for in-flight work.
- **G3** Nothing about *starting* is typed by hand: no pairing code, no credential export.
  (eHall SSO and mail app passwords are still typed by the person, once, into a browser or a
  user-owned file — the software never types them; see Non-goals.)
- **G4** Rings can be always available after Windows logon (opt-in, reversible with one file).
- **G5** Mail and eHall state are reported honestly, including "not configured" and "only a live
  check knows whether the session still works".
- **G6** No new authority: no new HTTP route, no new executor, no new capability, no secret stored
  by the project.

## 3. Non-goals

- No Windows-native GUI, installer, tray application or MSI.
- No service-manager integration: `systemctl --user` is `offline` in this distro, and this spec does
  not require it.
- No automated login to mail or eHall. Credentials are read, never typed by the software; eHall SSO
  stays a manual, headed-browser session (ADR-0025).
- No change to approval, execution, mobile-approval or conversation boundaries.
- **B (Windows double-click shortcut) is deferred.** Its launcher would be a thin wrapper around
  `rings up`, so it adds no design here.

## 4. Decisions

**D1 — the command surface lives in `rings`.** `assistant.cli_chat:main` gains three verbs and keeps
its two existing behaviours:

```text
rings                        terminal conversation (unchanged)
rings --web                  open /chat if the control plane is already running (unchanged)
rings up [--foreground]      start or reuse the daemon, auto-pair, open /chat, print status
rings down                   stop the daemon gracefully
rings autostart install|status|remove
```

`pw` keeps its own surface unchanged; `pw mobile pair` remains for phones and for any browser that
cannot be opened locally.

**D2 — lifecycle detection uses the existing instance lock and its existing probe.** `assistantd`
already holds an `flock` on `<runtime>/assistantd.lock` with `pid`, `started_at`, `version` and
`runtime_root` written as metadata, the kernel drops the lock when the process dies, and
`inspect_lock(path) -> LockState(held, metadata)` already answers "is a live process holding it"
without a side effect (it is what the v1.3 tests use to prove single-instance behaviour). So
`rings up`/`rings down` need no new probing machinery:

```text
inspect_lock(path) ── held=False ─► nobody is running (start the daemon)
                   └─ held=True  ─► a live process holds it; metadata names it (reuse / SIGTERM it)
```

No pid file, no `ps` parsing, no stale-lock handling: a lock file left by a power cut is not an
obstacle, which is the property ADR-0032 already tested.

The probe holds the lock for microseconds and releases it before the child starts (`inspect_lock`
does exactly this). If another process wins the race in between, the child fails with the existing,
already-clear
"Another assistantd instance is already running" error; `rings up` reports that verbatim rather
than retrying in a loop.

**D3 — `rings up` starts the daemon detached by default.** The child is started with
`start_new_session=True`, its stdout/stderr appended to `<runtime>/assistantd.log`
(`0600`; at each start, if that file is larger than 5 MB it is renamed to `assistantd.log.1`,
replacing any previous `.1`), so closing the terminal does not stop Rings.

`rings up --foreground` runs the daemon in the foreground instead, for watching the log. In that
mode this process *is* the daemon, so it does not auto-pair or open a browser: it prints the URL and
the sentence "本次为前台运行，不自动配对；已配对过的浏览器可直接打开该地址". Anyone using
`--foreground` is debugging, and the other two modes are the ones the product promises.

**D4 — readiness is lock held *and* web answering.** `rings up` waits up to 20 s for the control
plane URL to return *any* HTTP response — including `401`, which proves the server is listening.
On timeout it prints the log path and the last few lines and exits non-zero; it never claims a start
that did not happen.

If `[mobile] enabled = false` there is no HTTP surface to wait for: readiness falls back to "the
child holds the lock and is alive", `rings up` opens no browser, pairs nothing, and prints the
`[mobile]` snippet plus "终端对话可以直接用：`rings`".

**D5 — pairing is automatic, and only when it is needed.** `rings up` mints one pairing token
(the same one-time, 600 s, hash-only value `pw mobile pair` mints) and opens

```text
http://127.0.0.1:<port>/chat#pair=<token>
```

The chat page reads the fragment, and:

```text
/api/chat/bootstrap answers 200   → the browser is already paired: strip the fragment, do nothing
/api/chat/bootstrap answers 401   → redeem the fragment token via POST /api/pair, strip, reload
no fragment, 401                  → existing behaviour (say it is not paired, link to /pair)
```

Redemption uses the **existing** `POST /api/pair` route; the token is a capability, never in the
query string, and the page calls `history.replaceState` before anything else. This is the same
pattern the approval link already uses, so no new route is added and the frozen route set is
unchanged.

`rings up` learns the URL from the loaded configuration (`[mobile] bind` and `port`, the same
`web_chat_url` helper `rings --web` uses) — it never guesses a port and never asks the daemon.

**D6 — unconditional in outcome, conditional in effect.** The user never types or pastes a code in
any bind mode (`loopback` or `lan`). A browser that is already paired creates no new session row:
the token is minted but not redeemed, and its row is pruned by D7.

**D7 — expired pairing tokens are pruned when a new one is minted.** Today
`mobile_pairing_tokens` only ever grows; the prune makes its size a function of the TTL rather than
of how many times `rings up` ran. Only rows that are already `consumed_at IS NOT NULL` or
`expires_at <= now` are removed — never a live, unconsumed code.

**D8 — credentials come from a user-owned `secrets.env`.** Path
`~/.config/growing-assistant/secrets.env`, mode `0600` or the file is refused with an explanation.
Format and validation:

```ini
# comments and blank lines are allowed
DEEPSEEK_API_KEY=sk-…
GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD=…
GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD=…
```

- only `DEEPSEEK_API_KEY` and `GROWING_ASSISTANT_MAIL_<ID>_PASSWORD` / `_SMTP_PASSWORD` are
  accepted; any other key (including `password=`, `secret=`, `api_key=`) makes the loader refuse
  the whole file, so it cannot quietly become a general secret store;
- the process environment wins over the file, so a one-off `export` still overrides;
- the loader is called by `assistantd` and by the `pw`/`rings` entry points; it reports only *names*
  and a count, never values, and the project never writes the file;
- **the loader never writes to stdout** — diagnostics go through the logger to stderr, because
  `growing-assistant-mcp` speaks a stdio protocol on stdout and a stray line would corrupt it. It
  also never prints a value in any branch, including errors;
- the file is outside the repository, is not referenced by the config, and is covered by the
  existing backup exclusion rules (backups carry runtime state, not config or secrets).

Tests are naturally isolated from a developer's real file: every test points `XDG_CONFIG_HOME` at a
temporary directory, so nothing in the suite can read the host's secrets.

**D9 — autostart is one Windows `.cmd` in the Startup folder.** `rings autostart install` writes
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\rings-assistantd.cmd` (reached through
`/mnt/c/...`) containing exactly:

```bat
@echo off
wsl.exe -d <distro> -u <user> -- bash -lc "cd <repo> && uv run assistantd >> <runtime>/assistantd.log 2>&1"
```

The values are resolved, not guessed: the distro name comes from `$WSL_DISTRO_NAME` (falling back to
`wsl.exe -l -q`), the user from `id -un`, the repository from the installed `rings` entry point's
location, and `<runtime>`/`<repo>` are the **WSL-side Linux paths** — the command inside the quotes
runs in WSL, not in Windows. `%APPDATA%` is read with `cmd.exe /c echo %APPDATA%` and converted with
`wslpath`, so a non-default profile directory still works.

`rings autostart status` prints the path and whether it is installed; `rings autostart remove`
deletes it. Nothing is installed without the explicit verb, no administrator rights are needed, and
the command prints the exact file it wrote.

**D10 — one honest status block.** `rings up` prints, and never invents:

```text
daemon        started (pid 931974, 1.3.1) / already running (pid …, since …)
web           http://127.0.0.1:8765/chat
pairing       automatic (this launch)
mail          smail · credentials configured (GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD) · 6 stored
eHall         enabled · profile present · only a live check knows whether it is still logged in
```

When the control plane is disabled it says so and prints the `[mobile]` snippet; when a credential
is missing it names the variable, never the value; when eHall has no profile it points at
`pw ehall login`.

**D11 — `rings down` stops the lock holder.** It sends `SIGTERM` to the pid in the lock metadata,
waits up to 15 s for the lock to become free, and reports either "stopped" or "still running
(pid …)". The shutdown is the existing supervisor shutdown; a turn that was mid-flight is recorded
as interrupted and is never replayed (ADR-0041 §30).

When nothing holds the lock, `rings down` says "没有在运行" and exits `0`: stopping something that is
already stopped is not an error, and a script that calls it should not have to special-case that.
`rings up` on an already-running daemon with a different version prints both versions and reuses it;
it never restarts a live daemon on its own.

## 5. Files

| Area | File |
| --- | --- |
| Verbs, status block, deps seam | `src/assistant/cli_chat.py`, `src/assistant/cli_up.py` (new) |
| Lock probe / detached start / log rotation / readiness / stop | `src/assistant/adapters/runtime/daemon_process.py` (new), reusing `inspect_lock` |
| Windows Startup file | `src/assistant/adapters/runtime/windows_autostart.py` (new) |
| Credential file | `src/assistant/adapters/config/secrets_env.py` (new) |
| Pairing token prune | `src/assistant/store/mobile_sessions.py` |
| Fragment redemption | `src/assistant/adapters/web/static/chat.js` |
| Docs | `docs/specs/0002-…` (this file), `docs/adr/0046-…`, `README.md`, `README_en.md`,
  `docs/guides/getting-started.md`, `docs/guides/mobile.md`, `AGENTS.md` |

No migration, no new web route, no new dependency.

## 6. Testing

- **Launcher**: probe semantics (free / held / stale metadata), detached start + readiness +
  `rings down` against a real daemon in a temporary XDG root (one process-level test, ~10 s), log
  rotation, and a bounded-timeout failure path with a stub that never answers. The process-level
  test binds a free port chosen by the test (an ephemeral probe, then written into the temporary
  `config.toml`), so it cannot collide with a developer's running instance.
- **Credential file**: `0600` enforcement, unknown-key refusal, environment precedence, quoted and
  whitespace values, and a leak check that no value reaches the log or the status block.
- **Pairing**: the fragment flow is proved twice — an HTTP-level test for the conditional rule
  (already-paired browser must not create a second session; 401 must redeem exactly one) and the
  Playwright browser test for the real page (skipped on hosts without Chromium, as today).
- **Autostart**: file content and path are asserted against a temporary "Windows" root; install is
  never run against the real Startup folder in tests.
- **Existing gates stay green**: the frozen route set, the `pw` command surface, the migration
  pins, and the architecture tests that forbid shell/browser/executor reachability from the
  conversation path.

## 7. Release

v1.4.0 (new feature). ADR-0046 records D1–D11; version pins, release notes, changelog and the
READMEs move together; the full quality gate (ruff, mypy, lock check, full pytest twice, build,
installed-artifact smoke) runs before the merge, and the tag is created only after remote `main` CI
is green.

## 8. Rejected alternatives

- **Task Scheduler instead of the Startup folder.** More machinery, needs elevation on some hosts,
  and no benefit over one reversible file.
- **Extending the session TTL (30 days → longer) instead of auto-pairing.** Keeps the manual paste
  and adds a long-lived credential; auto-pairing removes the step without weakening the code.
- **Unconditional re-pair on every launch.** Simple, but grows `mobile_sessions` by one row per
  launch and leaves stale sessions alive for 30 days.
- **Foreground-only `rings up`.** The daemon would die with the terminal, which is the problem
  being solved.
- **A systemd user unit.** Unavailable here (`systemctl --user` is offline) and a bigger change in
  a WSL host than a Startup-folder file.
- **Secrets in `config.toml` or in the runtime database.** Both are backups-visible and both are
  explicitly refused by the existing parsers; the user-owned env file keeps the only secret source
  outside every artifact the project produces.
