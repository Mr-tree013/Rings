# ADR-0013 — Configured Storage Roots and Periodic Reconciliation

## Title

Configure storage roots explicitly, and keep them in sync with periodic reconciliation.

## Status

Accepted

## Context

Phase 2A/2B can catalogue and index a storage root, but only when a human runs
`pw vault scan` and `pw reindex`. That is a manual pipeline, not a personal assistant.

Making it continuous raises two design questions that are easy to get wrong:

how does the assistant know which locations are user data (rather than guessing at every
mounted drive), and what makes "the index matches the files" true over time — a filesystem
notification library, or something more boring and more reliable?

## Decision

1. Storage roots are **explicitly configured** in a host-local config file.
2. The assistant never auto-discovers `/mnt/*`, Windows drives or the home directory.
3. Both local roots and vault roots must be authorised explicitly.
4. A configured physical path is a host-specific runtime location, not document identity.
5. `root_id` remains the persistent identity.
6. A vault's real identity is re-verified from `.pa/vault.toml` on every reconciliation.
7. Automatic synchronisation uses **periodic reconciliation**.
8. One reconciliation is: metadata scan → catalog update → knowledge index update.
9. A filesystem watcher may later trigger reconciliation earlier; it may not be required.
10. Correctness never depends on watcher events.
11. An incomplete metadata scan does not trigger knowledge reindexing.
12. A root that is offline keeps its catalog presence and its knowledge state untouched.
13. One root's offline / identity-mismatch / index-corruption problem does not stop other
    roots from being reconciled.
14. Host runtime database infrastructure failures still propagate (the daemon supervisor
    handles them); they are not disguised as root failures.
15. Configuration is read once at daemon startup; no hot reload in this phase.
16. The daemon performs one reconciliation at startup, then once per interval.
17. At most one reconciliation runs at a time in a process (an `asyncio.Lock`).
18. Reconciliation produces **no filesystem `InboundEvent`**: catalog and index maintenance
    are materialised-view maintenance, not business processing.
19. If a future flow needs "this file changed → do something", it creates a durable event of
    its own then.
20. No OS filesystem-watcher dependency is introduced.

Supporting rules:

- Vault labels come from the manifest; a label in the config is accepted but ignored.
- A configured path is `~`-expanded and required to be absolute, but never `resolve()`d:
  background indexing must not silently follow a root-level symlink to another tree.
- A missing config file is not an error: it means "no roots configured", and the daemon still
  runs (a disconnected drive and an unconfigured host are both normal states).
- The configuration is authorisation; the catalog is fact. A root that has never been seen
  successfully gets no `storage_roots` row.
- Local roots are synced with `scan_local`, vault roots with `scan_vault`, so vault identity
  is verified before any scan happens.
- Sync order follows the config file's declaration order, and roots are processed
  sequentially: removable-media I/O, SQLite contention and predictable cancellation all
  favour predictable behaviour over parallel scans.

## Alternatives Considered

- **Auto-discover every disk**: convenient until a colleague's drive or a system volume is
  indexed as personal knowledge. Rejected: the user authorises each root.
- **Only a filesystem watcher**: instant detection, but events are dropped while the daemon is
  down, watchers are platform-specific, and "we missed an event" becomes a silent correctness
  bug. Rejected as the sole mechanism.
- **Watcher plus periodic reconciliation**: the natural end state — and exactly what this ADR
  allows, with reconciliation as the guarantee. Deferred: no watcher dependency in this phase.
- **Force a full reindex at every daemon startup**: guarantees freshness by re-reading every
  file, which is precisely the cost the incremental metadata comparison exists to avoid.
  Rejected.
- **Route every file change through the Event Inbox**: durable, uniform, and wrong-headed here:
  it would make infrastructure maintenance look like business work, flood the inbox with
  thousands of events, and couple the index to a worker that does not exist yet. Rejected.
- **Hot-reload the config file**: nicer for editing, but adds file watching, partial-apply
  semantics and "which config is live?" ambiguity for a single-user tool. Rejected: restart
  the daemon.

## Consequences

- A logged-in machine keeps knowledge current with no commands: the first reconciliation runs
  at startup, and each later cycle picks up changes with a bounded latency (default 300s).
- Detection latency is explicit and configurable (10s–86400s), and the cost of a cycle is
  metadata-only for unchanged files: no re-hash, no re-parse, no re-read.
- A temporary permission problem cannot erase searchable content: incomplete scans update the
  catalog metadata they did see and leave the index alone.
- Unplugging a drive is a normal state: the root reports `offline`, the catalog and index stay
  as they were, and the next cycle after re-plugging resumes work automatically.
- Mount-path changes require a config edit (no drive-letter guessing), but document identity is
  unaffected when the path is updated.
- The daemon has a supervisor with deterministic backoff, so a service that crashes is
  restarted and logged instead of silently disappearing; fatal startup problems (invalid
  config, migration failure) exit non-zero for the user to fix.
- Reconciliation is invisible to the Event Core: Phase 1 semantics are untouched, and the
  EventWorker still has no business handler to run.

