# ADR-0032 — Version 1 Runtime, Upgrade, and Release Contract

## Title

Version 1 is a frozen local-first Personal Operations System: one daemon per runtime root, private
runtime permissions, fail-closed migrations, an installable artifact that carries its own
migrations, and a release gate that proves all of it — without adding a single new capability.

## Status

Accepted

## Context

Phase 9A made the runtime inspectable, backup-able and recoverable. What it did not do is answer the
questions a version 1 has to answer, because until now the answer was "run it from a checkout":

- **two daemons on one runtime is silent corruption.** Every long-running service assumes it is the
  only writer of the segments it owns (the mail cursor, the scheduler lease table, the watcher
  state). Two `assistantd` processes started by hand, by a terminal that survived a crash, or by a
  Windows Task Scheduler entry that fired twice would each believe they hold the lease they claimed.
  A file that merely *exists* is not evidence: the only honest signal is an OS lock that the kernel
  drops when the process dies;
- **personal state is created world-readable by default.** The runtime database, raw mail objects,
  page snapshots and backups are created with whatever umask the shell happened to have. On a shared
  machine that is a privacy bug, and on WSL with a permissive umask it is the common case, not the
  exception. Fixing it means being precise about *which* files: the project's own runtime objects,
  never the user's own documents, vault files or configuration;
- **the archive format had one hole left open.** Phase 9A recorded that bytes appended after the ZIP
  end-of-central-directory record do not fail verification, and left it as a known limit. For a
  format whose entire job is "this file is exactly the state I backed up", "and also some junk" is
  not acceptable in a release;
- **a newer database must fail closed.** A binary that meets a `schema_migrations` row it does not
  know — a database written by a future build — must refuse rather than ignore it: ignoring it means
  running today's queries against tomorrow's schema, and the failure would surface as corrupted
  data rather than a refusal;
- **upgrades are only real if every historical starting point works.** The project has shipped
  fifteen forward migrations. A v1 user may restore an old runtime or upgrade a laptop that has not
  been opened since v0.2; the only defensible claim is that *every* prefix upgrades to the current
  schema, with integrity and foreign keys intact;
- **"installable" was never tested.** `uv run` works because the checkout is the working directory.
  The migrations live outside the package, so an installed wheel cannot even start the daemon. A
  release is not a release until the built artifact is installed into a clean environment and its
  console scripts run without the source tree;
- **release pressure is how capability creeps in.** The temptation at the end of a project is to
  wire "just one" convenience: execute an approved action on startup, let a Playbook run, let MCP
  write more, let the mobile client send. Every one of those would undo a boundary an earlier phase
  spent an ADR on.

Phase 9B therefore adds **no** capability. It adds a lock, permissions, a strict archive boundary, a
fail-closed migration check, upgrade-matrix and stress coverage, packaging that carries its own
data, an installed-wheel smoke, and documentation that says what v1 is and what it is not.

## Decision

1. v1 is a local-first Personal Operations System, not an autonomous general-purpose agent.
2. The supported reference runtime is Linux/WSL with Python 3.13.
3. Exactly one `assistantd` process may own daemon responsibilities for one runtime data root.
4. CLI processes may continue to perform the already-documented direct SQLite mutations.
5. Daemon single-instance enforcement uses an OS file lock, not PID existence alone.
6. A stale lock file without a held OS lock never prevents startup.
7. Graceful SIGINT/SIGTERM shutdown waits for supervised services to stop.
8. Runtime-created private directories use owner-only permissions where supported.
9. Runtime-created private files containing personal state use owner-only permissions where
   supported.
10. Backup archives containing personal state are created owner-readable/writable only.
11. Integrity/doctor may report unsafe existing permissions but must not silently rewrite them.
12. Runtime startup refuses a database whose applied migration history is newer or incompatible
    with this binary.
13. No downgrade migration exists.
14. Every historical migration remains immutable.
15. v1 upgrade testing covers representative historical migration prefixes through the current
    schema.
16. Release packaging must include every migration, ADR-required runtime package and static web
    asset.
17. Installed console entry points must work outside the source checkout.
18. v1 release tests must not require network access.
19. External provider credentials remain environment-only.
20. No release process embeds secrets into build artifacts.
21. v1 preserves all exact `ActionRequest` / `Approval` / unknown-result safety invariants.
22. Release hardening may not widen mobile, MCP, watcher, model, browser or executor capability
    surfaces.
23. The sample configuration must be safe by default.
24. `assistantd` must not automatically execute pending approved actions after restart.
25. `RUNNING`/`UNKNOWN` external actions remain unresolved across restart and upgrade.
26. Derived knowledge indexes remain rebuildable rather than authoritative.
27. A v1 release requires full Ruff, mypy, pytest, acceptance and package-build gates.
28. Release notes must state known limitations rather than implying capabilities that do not exist.
29. The v1 tag is created only from a clean verified `main` branch.
30. No migration is introduced solely for release metadata.

Additional frozen details:

- **The lock is an OS lock over a file, not a file.** `assistantd` opens
  `<XDG_DATA_HOME>/growing-assistant/assistantd.lock` (mode `0600`) and holds
  `fcntl.flock(fd, LOCK_EX | LOCK_NB)` for its whole lifetime; the kernel releases it when the
  process exits, including on `SIGKILL`. If the lock is held, the process exits non-zero with
  `Another assistantd instance is already running for this runtime data directory.` and starts no
  supervisor. A lock file whose lock is *not* held is simply an old file: startup proceeds. The
  PID/start-time/version metadata written inside the lock file is diagnostic only and never
  consulted for authorization; nothing secret is written there.
- **Shutdown is cooperative and awaited.** Signals set the shared stop event; services observe it;
  supervisors return; the daemon awaits every service task; the lock is released last. No
  `os._exit`, no abandoned background task. `assistantd` never executes an `ActionRequest`, so
  shutdown cannot start or retry an external action — that is a property of the composition, and an
  architecture test keeps it true.
- **Permissions are applied to project-created objects only.** Directories the project creates (the
  runtime root, mail raw directories, web snapshot directories, restore staging directories, the
  mobile runtime directory, the eHall profile directory) are requested at `0700`; files it creates
  (runtime database, raw mail objects, web snapshots, backup archives and their temporaries, the lock
  file, restored database and content) at `0600`. Mode bits are only *added* where the platform has
  them, and nowhere does the project `chmod` a file it did not create — not vault documents, not
  local knowledge roots, not the user's configuration. Existing unsafe modes are reported by
  `pw integrity check` (`WARN`, or `FAIL` for a world-writable database or content object) and never
  repaired automatically.
  The helper lives in `adapters/runtime/permissions.py`, and it is the one adapters import the store
  is allowed to make: applying an owner-only mode is an operating-system fact, not a persistence
  concern, and the alternative — a private copy of `chmod` logic in the store — is how two modules
  drift apart. An architecture test pins that exception to exactly this module.
- **The `.gab` end-of-central-directory boundary is strict.** The archive must contain no ZIP
  comment and no bytes after the final EOCD record, so `valid.gab + junk` and `valid.gab + another
  archive` are both invalid. Zip64 archives are supported; the parser is not reimplemented — only
  the trailing-boundary check is added around `zipfile`.
- **Migrations resolve from the package, and the future fails closed.** The SQL files ship inside the
  wheel as package data, and the runner prefers the packaged directory (falling back to the checkout
  when running from source). A database whose `schema_migrations` contains a version this binary does
  not ship is refused with `DatabaseMigrationIncompatible` on the mutable path (bootstrap,
  `assistantd`, mutating CLI commands); `pw integrity check` instead *reports* the incompatibility,
  because it opens the database read-only and must not change it.
- **Upgrade coverage is a matrix, not an anecdote.** Representative prefixes (`0001`, `0003`,
  `0006`, `0009`, `0012`, `0015`) are built, seeded with data of their era, and upgraded to the
  current schema; additionally every prefix `0001…N` for `N = 1..15` is created and upgraded, so no
  intermediate historical state is a dead end.
- **Stress coverage is bounded and deterministic.** A thousand inbound events, five hundred
  scheduled jobs, hundreds of tasks with calendar and work records exercise the same invariants as
  production with a `FakeClock` and no sleeps; the assertions are about dedup, leases, fencing,
  ordering and notification uniqueness, never about wall-clock latency.
- **The release artifact is the thing that is tested.** `uv build` produces the wheel and sdist;
  the wheel is installed into an isolated temporary environment, and the installed console scripts
  (`pw`, `assistantd`, `growing-assistant-mcp`) are run there against temporary XDG roots. Package
  contents, package secret sweep and example-configuration safety are checked on the artifact rather
  than on the checkout.

## Alternatives considered and rejected

- **Multiple `assistantd` instances sharing one runtime.** Every service would need distributed
  coordination to be correct, and two mail cursors or two scheduler claimers is data loss by
  construction. Rejected; one daemon per runtime root.
- **PID-file-only locking.** A PID in a file says nothing about whether that process is alive, is
  still the same process, or even exists on this machine after a reboot. Rejected; the authority is
  `flock`, the PID is a note.
- **Removing the lock file when it looks stale.** Deleting the file another process is *about* to
  lock is precisely the race the lock exists to prevent. Rejected; a stale file is harmless.
- **Automatic `chmod` of existing runtime files (or anything else) during startup.** Silently
  changing permissions on files the user may have configured deliberately, or that live outside the
  project's ownership, is not hardening. Rejected; report, do not rewrite.
- **`chmod` on the user's knowledge roots or vault.** They are external authorities owned by the
  user. Rejected.
- **Accepting trailing bytes after a ZIP archive.** "A backup plus some junk" is not the state that
  was backed up, and an appended archive is a plausible smuggling shape. Rejected; strict EOCD.
- **Downgrade migrations.** They would have to be written, tested and kept for every past schema,
  and they would let a user open a database with a binary that predates it. Rejected; forward only,
  and a newer history fails closed.
- **Ignoring an unknown applied migration.** It means running today's SQL against tomorrow's schema
  and discovering the mismatch as corrupted data. Rejected.
- **Shipping secrets in the sample configuration, or reading them from config.** Credentials are
  environment-only; the parser rejects secret keys by design. Rejected.
- **Network-dependent release tests.** The release gate must run on a machine with no route to any
  provider, or it tests that machine's network rather than the software. Rejected; the socket guard
  covers release tests too.
- **Turning Playbooks into executable workflows for v1.** A dry-run validator that reports "this
  build still understands the payload" is not an executor, and promoting it to one would remove the
  human approval that every external effect depends on. Rejected.
- **Enabling mobile or MCP writes by default.** Both are off by default for a reason, and a release
  is a bad moment to widen a surface. Rejected; defaults stay `mobile.enabled = false`,
  `mcp.enabled = false`, `mcp.write_scope = "none"`.
- **Executing pending approved actions after daemon restart.** Approval binds one exact fingerprint
  to one human decision at one moment; a restart is not a new decision, and "the daemon was down" is
  not consent. Rejected; executions stay a `pw action execute` act.
- **Bumping the version before the gates, or tagging from a dirty tree.** The tag has to name a
  commit whose tests actually passed. Rejected; version bump, gates, build, smoke and tag happen in
  that order, from a clean `main`.

## Consequences

- A user can install the wheel, run `pw doctor`, start exactly one daemon, upgrade a two-year-old
  runtime database and take a backup, without the source checkout present and without any of the
  safety invariants moving.
- The failure modes are explicit: a second daemon refuses to start, a newer database refuses to
  open, a backup with junk appended is invalid, and a world-writable runtime database is reported
  rather than silently fixed.
- Costs and limits: the runtime lock is POSIX-only (`fcntl`), so Windows-native execution is not a
  supported daemon platform in v1; permissions are only as meaningful as the filesystem they are
  applied to, and a filesystem without Unix modes (a Windows mount under `/mnt`) will report the
  modes it has rather than the ones intended; the strict EOCD check means an archive touched by a
  tool that appends a comment must be rebuilt; and the upgrade matrix proves schemas and
  representative rows, not every possible data shape a user could have created.
