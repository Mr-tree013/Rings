# ADR-0046 — One-Command Startup, Automatic Pairing and a User-Owned Secrets File

## Title

`rings up` starts or reuses the daemon, pairs the browser and opens `/chat` in one command;
`rings down` stops it; `rings autostart` optionally starts it at Windows logon; and the credentials
that used to be exported by hand come from one file the user owns.

## Status

Accepted

## Context

By v1.3.1 Rings was complete and pleasant to *use*, and tedious to *start*:

```text
terminal 1   uv run assistantd                 # dies with the terminal
terminal 2   rings --web                       # the browser must be paired first
browser      pw mobile pair → copy a code → paste it
shell        export GROWING_ASSISTANT_MAIL_<ID>_PASSWORD=…      # and the SMTP one
browser      pw ehall login …                  # a separate manual SSO session
```

Measured on the reference host, the cost of *staying* running is small (107 MB resident, ~0.5% of
one core at idle, 24 MB of runtime data, the control plane reachable in under a second), while the
four manual steps above are paid every time. Two of them also expire: the browser session lasts 30
days, and the eHall session lasts as long as the university says it does.

Four existing decisions shaped what a fix may do:

* the daemon already holds an `flock` on `<runtime>/assistantd.lock` with `pid`, `started_at` and
  `version` as metadata, and `inspect_lock()` already answers "is a live process holding it"
  without side effects (ADR-0032);
* `POST /api/pair` is the one route that takes a capability instead of a session, and the approval
  link already carries such a token in the URL *fragment*, which never reaches the server
  (ADR-0026, ADR-0041);
* credentials are environment-only on purpose — no secret store, no password in a config file
  (ADR-0043);
* the conversation prepares external effects and never performs them; any widening of that is a
  separate decision (ADR-0034, ADR-0045).

## Decision

1. **The verbs live in `rings`.** `rings` (terminal chat) and `rings --web` are unchanged;
   `rings up [--foreground] [--no-open]`, `rings down` and `rings autostart
   install|status|remove` are added. `pw` gains nothing: the startup path is one entry point.
2. **Lifecycle detection uses the lock, never `ps`.** `rings up` asks `inspect_lock`; a held lock
   means the daemon is running and its metadata names it, so "reuse it" is exact.
3. **`rings up` starts the daemon detached by default** (`start_new_session=True`, output appended
   to `<runtime>/assistantd.log`, rotated once at 5 MB per start, mode 0600). Closing the terminal
   must not stop Rings. `--foreground` runs the daemon in the terminal instead, and in that mode it
   neither pairs nor opens anything — that process *is* the daemon.
4. **Readiness is bounded and honest.** The command waits up to 20 s for the lock *and* for the
   control plane to answer any HTTP response (a 401 proves the server is listening). On timeout it
   prints the log path and its last lines and exits non-zero; it never claims a start that did not
   happen. When `[mobile]` is disabled there is nothing to wait for, so readiness is "the process
   is alive", no browser opens, and the command says how to enable the web surface.
5. **Pairing is automatic, and only when it is needed.** `rings up` mints one pairing token (the
   same one-time, 600 s, hash-only value `pw mobile pair` prints) and opens
   `<url>/chat#pair=<token>`. The page reads the fragment, removes it with `replaceState` before
   anything else, and redeems it through the existing `POST /api/pair` **only** when its own session
   is refused. An already-paired browser creates no second session.
6. **No new route, no new capability.** Auto-pairing reuses the route that already existed; the
   token is a capability in the fragment, never in a query string, and it is still single-use.
7. **Spent pairing tokens are pruned when a new one is minted.** A token is a 10-minute capability,
   not history; the table's size follows the TTL rather than the number of launches. A live,
   unconsumed code is never touched.
8. **Credentials come from a user-owned `secrets.env`.** `~/.config/growing-assistant/secrets.env`,
   mode 0600 or the file is refused with an explanation. Only the provider key (whose *name* the
   composition root supplies) and the derived `GROWING_ASSISTANT_MAIL_<ID>_PASSWORD|_SMTP_PASSWORD`
   names are accepted; one other key refuses the whole file, so it cannot become a general secret
   store. The process environment always wins, so a one-off `export` still overrides. The project
   never writes the file, never prints a value and never logs one — only names and counts, and never
   on stdout, because `growing-assistant-mcp` speaks a stdio protocol there.
9. **Autostart is one file.** `rings autostart install` writes a single `.cmd` into the per-user
   Startup folder whose one line re-enters WSL and runs `assistantd`; `status` shows it and `remove`
   deletes it. No administrator rights, no service, and its content is printed when it is written.
10. **One honest status block.** `rings up` reports the daemon (started or reused, with pid and
    version), the web URL or "not enabled", whether pairing happened, which credential *names* were
    loaded or why the file was refused, the mail accounts and how many have credentials, and the
    eHall state — including that only a live check knows whether a saved session is still valid.
11. **`rings down` is a graceful stop of the lock holder.** `SIGTERM` to the pid in the lock, then a
    bounded wait for the lock to free. `SIGTERM` is what the supervisor already handles; a turn that
    was mid-flight is recorded as interrupted and never replayed. Stopping a stopped daemon is not
    an error, and a daemon whose version differs is reused rather than restarting itself.

### Rejected alternatives

- **A Windows Task Scheduler entry instead of the Startup folder.** More machinery, sometimes needs
  elevation, and no benefit over one reversible file.
- **Extending the session TTL instead of pairing automatically.** Keeps the manual paste and adds a
  longer-lived credential; auto-pairing removes the step without weakening the code.
- **Re-pairing on every launch, unconditionally.** Grows `mobile_sessions` by one row per launch and
  leaves stale sessions alive for 30 days.
- **Foreground-only `rings up`.** The daemon would die with the terminal, which is the problem.
- **A systemd user unit.** Unavailable here (`systemctl --user` is offline) and a bigger change on a
  WSL host than one Startup file.
- **Secrets in `config.toml` or in the runtime database.** Both are backup-visible and both are
  refused by the existing parsers.
- **Letting the launcher read the provider key from anywhere it likes.** The architecture gate that
  keeps that variable name in exactly two modules stays as strict as it was; the loader takes the
  name as a parameter instead.

## Consequences

Starting Rings is one command, and it can be made to survive a reboot with one more. The pieces that
made the four steps necessary are unchanged underneath: the same lock, the same one-time pairing
code, the same environment-only credentials, and the same approval boundary for every external
effect. What changed is who performs the mechanical parts — the launcher instead of the person.

The costs, stated plainly: a secrets file exists on disk (0600, user-owned, outside the repository);
`rings up` mints a pairing token per launch (pruned, and never used by an already-paired browser);
and an always-on daemon means the mail pipeline analyses new mail while nobody is looking, which is
the one recurring cost of the always-on option.

## Implementation notes

- `src/assistant/cli_up.py` — verbs, status block, dependency seam, autostart verbs.
- `src/assistant/adapters/runtime/daemon_process.py` — lock state, detached spawn, log rotation,
  HTTP readiness, `SIGTERM` stop.
- `src/assistant/adapters/runtime/windows_autostart.py` — the Startup file, rendered and managed
  without touching the real folder in tests.
- `src/assistant/adapters/config/secrets_env.py` — the loader.
- `src/assistant/adapters/web/static/chat.js` — fragment redemption.
- `src/assistant/store/mobile_sessions.py` — pairing-token pruning.
- Spec: `docs/specs/0002-one-command-startup.md`; plan:
  `docs/dev/2026-09-22-one-command-startup-plan.md`.
