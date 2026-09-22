# Integrity, backup and recovery

## What it does

The runtime authority is one SQLite database plus the immutable objects it references — raw received
mail and normalized web snapshots. A backup takes a consistent copy of exactly those, and a restore
puts them back into a **new** directory that you then point a runtime at.

Since v1.3 that database also carries the derived-but-durable state of the release: the attention
inbox (including which items you acknowledged or dismissed), the planning capacity preferences,
superseded plan blocks and pending external reviews — a mail send or a certificate application
waiting for your confirmation. All of it is ordinary rows, so `pw backup create` carries it with no
extra step, and `pw backup inspect` reports the counts.

What a restore *does not* carry is authority: every outstanding approval is superseded, every
unconsumed challenge is invalidated, every pairing token is consumed and every session is revoked on
the restored copy. A restored review is still a pointer, not a permission: confirming it afterwards
means creating a fresh approval by hand. Credentials and the eHall browser profile live outside the
archive and are never included.

```bash
uv run pw integrity check

uv run pw backup create ~/assistant-backup.gab
uv run pw backup verify ~/assistant-backup.gab
uv run pw backup inspect ~/assistant-backup.gab

uv run pw backup restore \
  ~/assistant-backup.gab \
  --to ~/restored-assistant-data
```

## Common workflow

```bash
uv run pw integrity check                 # read-only, offline; creates, migrates and repairs nothing
uv run pw backup create ~/assistant-backup.gab
uv run pw backup verify ~/assistant-backup.gab
uv run pw backup inspect ~/assistant-backup.gab
```

An existing archive file is never overwritten, so a timestamped name per run is the simplest habit.

## Restore into staging

```bash
uv run pw backup restore ~/assistant-backup.gab --to ~/restored-assistant-data
XDG_DATA_HOME=~/restored-assistant-data uv run pw integrity check
```

The restored directory is a complete runtime data directory laid out exactly like the original
(`<runtime>/mail/…`, `<runtime>/web/snapshots/…`), so it is the equivalent of
`$XDG_DATA_HOME/growing-assistant/` and can be used by pointing `XDG_DATA_HOME` at its parent.

### Disaster recovery checklist

1. Stop `assistantd` and any running editor MCP session.
2. `uv run pw backup verify ~/assistant-backup.gab`.
3. `uv run pw backup restore ~/assistant-backup.gab --to ~/restored-assistant-data`.
4. `pw integrity check` against the restored data directory.
5. Re-provide credentials (`DEEPSEEK_API_KEY`, IMAP and SMTP passwords) — they are not in the
   archive.
6. `pw ehall login` again if you use eHall — the authenticated browser profile is not in the archive.
7. `pw mobile pair` again — every mobile session was revoked by the restore.
8. Review unresolved actions (`pw actions`, especially `RUNNING` and `UNKNOWN`) before doing
   anything else.
9. Rebuild knowledge indexes (`pw sync` / `pw reindex`) from the original roots.
10. Start the daemon against the restored data directory with `XDG_DATA_HOME` pointed at its parent.

**Never** copy files from an archive over a live database while the daemon is running. Rings does not
offer that path and does not need it.

## Commands

```text
pw integrity check
pw backup create FILE
pw backup verify FILE
pw backup inspect FILE
pw backup restore FILE --to DIR
```

## Safety behavior

- **Consistency.** Backups use the SQLite backup API, so they never copy a live database file or its
  `-wal`/`-shm` sidecars. Referenced objects are re-hashed: a missing or corrupt object fails the
  whole backup instead of producing a "successful" archive that is short of content.
- **Fixed archive shape.** A manifest carrying identities and hashes, the runtime database, raw mail
  and web snapshots. Nothing else is a member.
- **Verification is read-only.** `verify` and `inspect` write nothing. Verification checks member
  names (absolute paths, `..`, backslashes, drive letters, symlinks, duplicates, unlisted or missing
  members and size/compression-ratio limits), the manifest, per-member sizes and hashes, and the
  database's own integrity, foreign keys and migration compatibility.
- **Restore is staging-only.** `--to` must be an absent or empty directory that is not the live
  runtime (or a parent or child of it). There is no in-place restore and no `--force`. Content is
  written to a temporary sibling directory and moved into place only after verification passes; a
  failure removes only the temporary directory the command itself created.
- **A restore invalidates outstanding authorization.** Unconsumed challenges, valid approvals, unused
  pairing codes and all mobile sessions are invalidated — the challenge and approval rows are not
  marked as used, but they can no longer authorize anything. History is never deleted.
- **`RUNNING` / `UNKNOWN` executions stay unresolved.** A restore is not reconciliation: it does not
  claim that an external effect was rolled back, and those runs keep blocking a blind retry.
- **Exit codes.** `0` success or valid; `1` a problem with the archive or the runtime, or a policy
  refusal such as a non-empty target; `2` a command usage problem. "You used the wrong argument" and
  "the backup is broken" are worth distinguishing.

### What a backup excludes

```text
model API keys
IMAP passwords
SMTP passwords
eHall authenticated browser profile
derived knowledge indexes
external original knowledge files
```

The configuration file stays where it is too. Credentials and sessions belong to the environment, and
indexes are derived data that can always be rebuilt from the originals.

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| `BackupSourceMissing` / `BackupSourceCorrupt` | An object the database references is missing or changed. Backups fail rather than producing a partial archive. |
| Restore refuses the target | The directory exists and is not empty, or it is the live runtime (or a parent/child of it). |
| A restored runtime asks for approval again | Correct: approvals were invalidated by the restore. |
| Knowledge answers are empty after a restore | Indexes are derived: rebuild them with `pw sync` or `pw reindex`. |

- There is no automatic or scheduled backup, no cloud backup or sync, and no in-place restore.
- There is no database downgrade: a runtime written by a newer schema version fails closed.

## Implementation notes

- Operational integrity, safe backup and recovery: [ADR-0031](../adr/0031-operational-integrity-backup-and-recovery.md)
- System design §9: [docs/specs/0001-system-design.md](../specs/0001-system-design.md)
- v1 runtime and release contract: [ADR-0032](../adr/0032-v1-runtime-and-release-contract.md)
