# Upgrading to growing-assistant v1.0

v1.0 does not change the schema. The runtime database in a 0.8.0 installation is the runtime database
of a 1.0 installation — migrations still end at `0015_inbound_observations.sql`. What changes is the
software around it: a single-instance daemon, owner-only file permissions, a strict backup format, and
a fail-closed refusal to open a database written by a newer build.

## From 0.8.0 (the direct path)

1. **Stop the daemon.** `Ctrl-C` in the terminal running `assistantd`, or send it `SIGTERM`. Confirm
   nothing is holding the runtime lock:

   ```bash
   uv run pw daemon status
   ```

2. **Take a backup with what you have** (do this before touching anything):

   ```bash
   uv run pw integrity check
   uv run pw backup create ~/assistant-backup-0.8.0.gab
   uv run pw backup verify ~/assistant-backup-0.8.0.gab
   ```

   If the audit reports a problem, fix or record it *before* upgrading: a backup of a damaged runtime
   is a damaged backup.

3. **Update the software.** Either pull the repository and reinstall, or install the v1.0 wheel:

   ```bash
   git pull            # if you run from a checkout
   uv sync --frozen    # installs exactly the locked dependency set
   ```

4. **Check the environment.** This is read-only; it does not migrate anything:

   ```bash
   uv run pw doctor
   ```

   It reports the runtime database, migration state, writability of the runtime directories, the
   content roots and whether the credentials your configuration needs are present in the
   environment.

5. **Let the software migrate forward.** Any normal command that opens the runtime does this
   synchronously, and so does `assistantd`:

   ```bash
   uv run pw task list          # or any other mutating/reading command
   ```

   If the database was written by a *newer* build than the one you just installed, this is where you
   find out: the command refuses with a clear message instead of running today's SQL against
   tomorrow's schema.

6. **Verify the result.**

   ```bash
   uv run pw integrity check
   ```

   `Result: PASS` is the green light. `permissions` may report `WARN` for directories that already
   existed with loose modes — that is a report, not a failure, and the assistant will not rewrite
   permissions you chose. Inspect anything reported as `FAIL` or `CRITICAL` before continuing.

7. **Start the daemon again.**

   ```bash
   uv run assistantd
   ```

   Exactly one daemon may run per runtime data directory; a second one refuses to start and says so.
   The lock it holds is an OS advisory lock, so a crash (or a machine that lost power) never leaves
   you with a file you have to delete.

8. **Inspect unresolved actions.**

   ```bash
   uv run pw actions
   uv run pw action show <id>
   ```

   Anything with a `RUNNING` or `UNKNOWN` execution stays unresolved across the upgrade on purpose.
   Confirm what happened at the provider (Sent-mail reconciliation for `mail.send`, a manual check in
   eHall) instead of retrying: the fence that blocks a blind retry is still in place.

## From 0.7.0 or earlier

The same steps, with one addition: your runtime will run through the migrations it is missing —
event leases, storage catalog, commitments, planning, scheduler, mail, intelligence, drafts,
cases and approvals, mail send, mobile, learning, playbooks and observations. That path is covered by
the release suite from every historical prefix, including representative data from the event-inbox,
storage, scheduler, mail-draft, mobile and watcher eras.

Take the backup with the **old** binary first (step 2). A backup written by an older build is a
backup: the archive format and its manifest are versioned, and `pw backup verify` will tell you
whether the file is intact.

## What does not survive an upgrade or a restore

- **Credentials.** SMTP and IMAP passwords and the model API key come from the environment; they are
  never written to configuration, an event, a log or a backup. Re-export them.
- **The eHall browser profile.** It is authenticated session material and is deliberately excluded.
  Run `pw ehall login` again.
- **Mobile pairings.** Sessions and pairing codes are invalidated by a *restore* (not by an upgrade
  in place). Run `pw mobile pair` and pair each phone again.
- **Knowledge indexes.** They are derived and rebuildable: `pw reindex --root <id>`. Your original
  files are untouched, as always.

## Downgrades

A database cannot be downgraded. If you need to go back to an earlier release, restore the backup you
took in step 2 into a staging directory and run the older binary against that:

```bash
uv run pw backup restore ~/assistant-backup-0.8.0.gab --to ~/recovery/growing-assistant
XDG_DATA_HOME=~/recovery uv run pw integrity check
```

There is no in-place restore, no `--force` and no downgrade migration: the old binary opens the
staged copy and the current runtime stays untouched.
