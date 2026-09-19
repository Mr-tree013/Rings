# ADR-0009 — Async Boundary for Blocking SQLite Access

## Title

Put an async boundary in front of blocking SQLite access, with connections owned by the
worker thread that uses them.

## Status

Accepted

## Context

The runtime store is `sqlite3` (ADR-0008), whose API is blocking. The daemon that will
host mail, index, web and scheduler services runs on an asyncio event loop
(`assistantd`). Calling a blocking database operation directly from that loop would stall
every other service — including the mobile surface a user is waiting on — for the
duration of a disk write.

Phase 1A left the repository synchronous and owned a live `sqlite3.Connection` in a
long-lived `Database` object. That shape cannot be handed to a worker thread safely,
because `sqlite3.Connection` objects are bound to the thread that created them unless the
guard is disabled — and disabling the guard (`check_same_thread=False`) would hide
exactly the bug we need to prevent, turning a loud error into silent data corruption.

## Decision

- The application-facing repository port is **async**: `add`, `get`,
  `get_by_external_identity`, `list_pending`, `transition`.
- The SQLite adapter implements each operation as a thin `async` method that delegates to
  a private blocking `_*_sync` method through `asyncio.to_thread()`.
- A connection is created, used and closed **inside the worker thread that runs the SQL**.
  `Database` is a connection factory plus pragma policy, not a connection owner; no
  connection is cached, and no connection crosses a thread boundary.
- `check_same_thread` is never passed to `connect()`; the sqlite3 thread guard stays on,
  and a static test enforces that.
- Migrations stay synchronous and run during process startup, before the long-running
  services exist. They are not wrapped in `to_thread()` for symmetry.
- No DB worker pool, actor model or `aiosqlite` dependency: the workload is one person's
  assistant with low write volume, and `to_thread()` is sufficient. If measurements ever
  show otherwise, the port is the seam where a different implementation goes.

## Alternatives Considered

- **`aiosqlite`**: removes blocking from the loop but adds a dependency and a second
  concurrency model (its own background thread per connection), while the code we would
  write still has to reason about threads and transactions. Rejected for now.
- **A dedicated DB worker thread with a request queue (actor model)**: strong control over
  ordering and backpressure, and the natural end state for high write concurrency — but
  unjustified complexity for a single-user assistant, and it would need its own
  cancellation and shutdown design. Rejected until evidence demands it.
- **A connection pool**: the workload has almost no concurrent readers and one writer;
  SQLite serialises writers anyway. Rejected as premature.
- **Keeping the synchronous API and letting callers remember to use `to_thread()`**:
  the boundary then lives in every call site, which is where it will be forgotten.
  Rejected: the port makes the async contract explicit and testable.
- **`check_same_thread=False` with one shared connection**: removes the error instead of
  the problem. Rejected outright.

## Consequences

- The event loop stays free; each database operation costs one thread hand-off, which is
  negligible next to the disk write it wraps.
- `Database` no longer holds state, so it is safe to construct per component and pass
  across threads; `Database.connect()` is the only way to reach a connection.
- Repository behaviour is proven by async integration tests against real SQLite files,
  including two concurrent ingests racing for one identity.
- Every future store module must follow the same shape: async public API, private
  blocking implementation, connection created and closed in the worker thread.

