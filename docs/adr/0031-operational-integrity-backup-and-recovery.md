# ADR-0031 — Operational Integrity, Safe Backup, and Recovery

## Title

The runtime can be inspected without changing it, backed up consistently without trusting a file
copy, and restored into a fresh staging directory that comes back *without* the authorization
capabilities that were live when the backup was taken.

## Status

Accepted

## Context

Everything the assistant knows lives in one SQLite database plus the files that database references.
By Phase 9A that database is no longer a convenience cache: it holds commitments, cases and their
approval history, mail metadata pointing at archived RFC822 objects, drafts, facts and playbooks,
web observations pointing at normalized snapshots, mobile sessions, and the durable intents the
daemon works from. Losing it is losing the assistant. So two ordinary operational questions finally
have to be answered — *is what I have still intact?* and *can I get it back?* — and both answers have
to be as careful as the rest of this project.

- **a copy is not a backup.** SQLite in WAL mode keeps committed data in `-wal`; copying
  `assistant.db` while the daemon runs produces an archive that may be torn, may miss recent
  commits, or may look fine until a `PRAGMA integrity_check` reads it. The database has an official
  consistent-backup API and this is exactly what it is for;
- **the database is not the whole state.** A mail row whose raw `.eml` is missing is a row that can
  never be shown or re-analysed; a web observation whose snapshot is missing can never be diffed
  again. Those objects are content-addressed and immutable, which makes them safe to copy — but only
  if the copy is checked against the hash the database recorded;
- **some of the runtime is deliberately not authority.** Knowledge indexes are rebuilt from original
  files; the eHall browser profile is session material; credentials belong to the environment. A
  backup that swept up `cache/`, the Chromium profile and `config.toml` would turn a recovery tool
  into a credential-transfer tool and would silently promote derived state to authority;
- **restoring is re-arming.** An approval is a capability: exact-fingerprint, single-use, bounded by
  a challenge. A backup taken with a live approval would, on restore, hand back an authorization
  that was granted to a person in a past session — possibly on a different machine, possibly long
  ago, possibly after the payload's meaning changed. The same is true of an unconsumed
  `ApprovalChallenge` and of every mobile session and pairing token;
- **restoring must not invent a clean slate.** `ExecutionRun` rows in `RUNNING` and `UNKNOWN` are
  evidence that an external system may have been touched. Recovery is not reconciliation: pretending
  those runs succeeded, or retrying them, is precisely the double-send this project has been
  avoiding since ADR-0023/0024/0025;
- **restoring over the live runtime is how disasters become permanent.** Any tool that writes into
  the active data directory while a daemon is using it can destroy the very state the operator was
  trying to save. Recovery therefore produces a *new* runtime tree that the operator inspects and
  then points a fresh process at;
- **an archive is hostile input.** A `.gab` file from a USB stick is not trusted: path traversal,
  absolute members, drive letters, symlink entries, duplicate members, unlisted members and
  decompression bombs all have to be refused by construction, on a format the assistant itself can
  write but is not required to trust.

Phase 9A introduces no new business capability. It adds a read-only integrity checker, a consistent
backup writer, an archive verifier, and a staging-only restore that invalidates authorization as a
final step, plus the cross-system acceptance suite that proves the whole lifecycle still holds
together.

## Decision

1. The SQLite runtime database is the primary durable runtime-state authority.
2. A live WAL database is backed up with SQLite's backup API, never by blindly copying the database
   file.
3. Mail raw RFC822 objects referenced by the database are durable source objects and belong in the
   operational backup.
4. Web snapshot objects referenced by the database belong in the operational backup.
5. Per-root knowledge indexes are derived state and are excluded from backup.
6. Original local/vault knowledge files are external authorities and are not copied into an
   operational backup.
7. The eHall browser profile is session/credential-like state and is excluded.
8. Environment credentials and secret files are excluded.
9. Application config is not silently copied into the backup.
10. Backup archives contain a versioned manifest and cryptographic hashes.
11. Backup verification is read-only.
12. Restore never overwrites the active runtime directory in place.
13. Restore targets a caller-selected empty staging directory.
14. Archive extraction rejects absolute paths, traversal, links, duplicate members and unexpected
    files.
15. Restore verifies every member hash before making the restored runtime usable.
16. A restored SQLite database must pass migrations compatibility, `integrity_check` and
    `foreign_key_check`.
17. Restoring authorization state must not silently re-authorize future side effects.
18. Unconsumed `ApprovalChallenge`s are invalidated during restore finalization.
19. Unconsumed live `Approval`s are invalidated/superseded during restore finalization.
20. Mobile pairing tokens and mobile sessions are invalidated/revoked during restore finalization.
21. Historical `ActionRequest`s, `Approval`s and `ExecutionRun`s remain preserved for audit.
22. `RUNNING`/`UNKNOWN` external `ExecutionRun`s remain unresolved and continue to block blind
    retries.
23. Restore does not claim that external effects were rolled back.
24. Lease-based internal work relies on existing durable lease/reclaim semantics; restore must not
    invent successful completion.
25. Integrity checks never contact SMTP, IMAP, eHall, web watchers, models or MCP clients.
26. Derived indexes can be rebuilt after restore.
27. Backup/restore operations never log mail bodies, page contents, credentials, tokens or action
    payloads.
28. Full-system acceptance tests use temporary XDG roots and fake external adapters only.
29. V1 recovery is explicit operator work; there is no automatic cloud backup or background backup
    daemon.
30. Phase 9A introduces no new external side-effect capability.

Additional frozen details:

- **The archive has a fixed shape.** `manifest.json`, `runtime.sqlite3`, `mail/raw/…`,
  `web/snapshots/…` — and nothing else at the top level. There is no extension point, no
  "extra payload" directory and no per-version member set; a second format is a second ADR. Object
  members are content-addressed (`<prefix>/<sha256>.eml`, `<prefix>/<sha256>.txt`) and the prefix
  must equal the first two hex digits of the name, so a member cannot claim an identity it does not
  have.
- **Every member name is checked before anything is opened.** Absolute paths, `..` in either
  separator, backslashes, Windows drive letters, NUL bytes and empty segments are refused; the
  verifier additionally refuses symlink entries, duplicate names, members that the manifest does not
  list and listed members that the archive does not contain.
- **The bounds are named constants, not an absence of bounds.** 100 000 members, a 4 MiB manifest,
  a 16 GiB database, 64 MiB per content object, 64 GiB declared uncompressed in total, and a
  declared-ratio sanity limit of 200:1. A personal mail archive is legitimately large, so the total
  is generous — but it is a number, and an archive that declares more is refused rather than
  expanded.
- **The manifest carries identity, never content.** Format version, application version, creation
  time, database member and hash, migration file names, and for each object a storage key, SHA-256
  and size, plus aggregate counts. No mail body, no page text, no action payload, no credential, no
  token, no physical path of an original document. It is canonical JSON (sorted keys, compact
  separators, `allow_nan=False`, lowercase hex), so two identical backups have identical manifests.
- **The backup reads from its own snapshot.** The object list comes from the *backed-up* database,
  not the live one, so a mail that arrives mid-backup is simply not part of this backup rather than
  an inconsistency. Every referenced object is then read, checked to stay inside the configured
  runtime raw/snapshot root, and hashed. A missing or mismatched object fails the whole backup with
  `BackupSourceMissing` / `BackupSourceCorrupt`; a "successful" archive that quietly omits a file is
  not a thing this format can express.
- **The output appears atomically.** The archive is written to a temporary file in the destination's
  directory, flushed, verified, and only then `os.replace`d into place, so a crash leaves either the
  previous state or nothing — never a half-written archive. An existing file is refused; there is no
  `--overwrite` in V1.
- **Restore is staged and then renamed.** The destination must not exist or must be empty, and it is
  refused if it *is* the active runtime directory (or a parent/child of it). Members are extracted
  one by one with their hash and size verified as they are written, into a sibling `.NAME.restore-*`
  directory on the same filesystem, which is renamed into place only after finalization and a second
  integrity pass succeed. A failure removes only the staging directory this command created.
- **Finalization invalidates capabilities, not history.** In one transaction on the restored
  database: unconsumed challenges get `consumed_at = restore_time` (a capability token is spent —
  this is not evidence of a person's approval); approvals get `superseded_at = restore_time`, never
  `consumed_at`, because claiming they were consumed would claim they were used; unredeemed pairing
  tokens get `consumed_at = restore_time`; and every non-revoked session gets
  `revoked_at = restore_time`. Nothing is deleted, and the mobile client must pair again. The
  restored database is then checkpointed and left in rollback-journal mode so the staged tree is one
  file, not a file plus a `-wal`.
- **`ExecutionRun` state is left exactly as it was**, including `RUNNING` and `UNKNOWN`, and the
  existing unresolved-run fence still refuses to execute that `ActionRequest` after recovery. A
  regression test drives `pw action execute` against a restored `UNKNOWN` run and asserts the fake
  SMTP transport sees zero `DATA` commands.
- **Integrity checking is read-only and offline.** It opens the runtime database in SQLite
  read-only mode (so it cannot create, migrate or repair it), reports `PRAGMA integrity_check` and
  `PRAGMA foreign_key_check` plus cross-domain consistency (capability fingerprints, fact
  provenance, mail threading and links, observation lineage and event links, migration state),
  re-hashes referenced mail and web objects, and never touches a network or a model. Pending
  migrations are reported as `PENDING`, not applied; problems are reported, never repaired.
- **Knowledge roots are checked for presence, not for content.** A configured root that is offline
  (an unplugged USB vault) is reported as offline — it is not runtime corruption, and it never
  triggers a reindex.

## Alternatives considered and rejected

- **Copying the live SQLite file, or `assistant.db` together with its `-wal` and `-shm`.** This is
  the classic way to produce a backup that fails only when it is needed. Rejected; the backup uses
  `sqlite3.Connection.backup`.
- **Including credentials in the backup.** It would make the archive a secret store, it would move
  secrets into whatever medium the operator chose, and it would make restore silently re-arm SMTP,
  IMAP and the model provider. Rejected; credentials are re-supplied by the operator.
- **Including the eHall browser profile.** It is authenticated session material for a university
  system. Rejected; a restored environment requires a fresh `pw ehall login`.
- **Backing up derived knowledge indexes as authority.** They are rebuildable, they are large, and
  treating them as authority would let a stale index outlive the files it describes. Rejected;
  rebuild from the original roots.
- **Copying original vault/local documents into the backup.** They are external authorities that may
  live on a drive the operator controls. Rejected; the archive references them only by storage key.
- **Restoring directly over the running runtime directory.** It destroys the state the operator was
  trying to recover from, and with a daemon running it is a race. Rejected; restore is staging-only
  and there is no `--in-place` or `--force`.
- **Preserving live approval/session authorization unchanged.** A restored approval is an
  authorization granted in another session and possibly on another machine. Rejected; challenges,
  approvals, pairing tokens and sessions are invalidated during finalization.
- **Automatically retrying `RUNNING`/`UNKNOWN` external actions after restore.** Recovery cannot know
  whether SMTP or eHall already acted. Rejected; unresolved runs stay unresolved and keep blocking
  blind retries.
- **Automatic or cloud backup.** It would ship personal mail and page content to a third party and
  would hide the trust decision inside a background job. Rejected; V1 recovery is explicit operator
  work with a local file.
- **Network-based integrity checking** (for example pinging a provider to see whether it is
  reachable). Rejected; integrity is a statement about local durable state and must work with the
  network down.
- **Auto-migrating during `pw integrity check`.** It would make a read-only diagnosis mutate the
  thing it is diagnosing. Rejected; a pending migration is a finding.

## Consequences

- An operator can ask whether the runtime is intact, produce one file that is a truthful snapshot of
  the durable runtime, verify that file later on another machine, and stage a recovery — with three
  commands whose output never contains a mail body, page text, credential or token.
- The restored runtime is *usable but not re-armed*: tasks, cases, mail metadata, drafts, actions and
  their history, facts, playbooks and observations all read back, while approvals, challenges,
  pairing tokens and sessions do not. The five-step recovery procedure in the README makes that
  explicit instead of surprising.
- Costs and limits: the archive is one database plus referenced objects, so it is not a
  disk-image-style snapshot of everything the assistant ever derived; knowledge must be reindexed
  after restore; a large mail archive makes a large file, and the ratio/size bounds mean a
  pathologically compressed archive is refused rather than accepted slowly; and because V1 has no
  overwrite, backing up twice means choosing two file names.
- The refusal paths are the point: a missing raw mail object, a mismatched web snapshot hash, a
  tampered action payload, a body that traverses out of the staging directory, or a manifest that
  names a member the archive does not contain all *stop* the operation rather than degrade it.
