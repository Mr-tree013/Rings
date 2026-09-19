# ADR-0011 — Stable Storage Identity and Rebuildable Metadata Catalog

## Title

Identify documents by stable logical URIs, keep the metadata catalog on the host, and treat
physical paths as runtime locations.

## Status

Accepted

## Context

Phase 2 starts on the personal knowledge side: the assistant must eventually answer "what do
I have, and where did it come from?" across a local folder tree and one or more archive
vaults on removable storage. Before any of that, two things have to be true.

A document must keep its identity when a drive is remounted at a different mount point, and
the metadata the assistant keeps must never lie about what still exists. The failure modes
that would poison every later phase:

- a document stored as `/mnt/e/Photos/2024/a.jpg` becomes a *different* document when the
  drive mounts at `/mnt/f` or `E:` is reassigned;
- a metadata scan that hits one permission error concludes that thousands of files were
  deleted;
- the host database becomes the only copy of something that cannot be regenerated.

## Decision

1. The agent runs on the host machine. It never runs from a USB drive.
2. Removable drives and portable SSDs are **archive vaults**: cold storage, not runtime.
3. Local folders and archive vaults both get a **stable logical identity**, never an
   absolute path.
4. Local roots are referenced as `local://<root-id>/<relative-path>`.
5. Archive vaults are referenced as `vault://<vault-id>/<relative-path>`.
6. `/mnt/e/...`, `E:\...` and any other mount path are *current physical location*, not
   document identity. They are stored only as mutable runtime metadata
   (`storage_roots.last_known_path`) and never appear in a logical URI.
7. The host SQLite database keeps a lightweight **metadata catalog**.
8. The original files remain the authority; the catalog is derived.
9. The catalog can be discarded and rebuilt by scanning again — that is the test of whether
   it is derived.
10. No file contents are stored in this phase: no text, no summary, no embedding, no hash.
11. Scans do not follow symbolic links (files or directories).
12. An incomplete scan must never mark unseen files as missing.
13. Content hashing is not part of the initial scan: a first pass over years of photos,
    videos and archives must be metadata-only I/O.
14. Incremental change detection in this phase uses
    `relative path + size + mtime_ns`.
15. A strong content hash is produced later, when text extraction needs one.

Supporting rules that make the above testable:

- A root id matches `^[a-z][a-z0-9-]{0,62}$`, and the same id can never change kind
  (`local` ↔ `vault`) once registered.
- A vault names itself in `.pa/vault.toml` (`format_version`, `vault_id`, `label`,
  `created_at`). Initialisation is an explicit command; a directory that merely looks like a
  drive is never turned into a vault automatically.
- `MISSING` means exactly one thing: a *complete* scan of that root did not see the path.
  An unplugged vault is not `MISSING` — availability is a runtime question, not a catalog
  state, so there is no `OFFLINE` presence.
- Renames are not detected in this phase. A rename is an old path going `MISSING` and a new
  path appearing; guessing identity from size, mtime or filename would be worse than
  admitting a new entry.

## Alternatives Considered

- **Store absolute paths as document identity**: simplest to write, and wrong the first time
  a drive letter changes. Every downstream reference (mail attachments, eHall material,
  archive links) would silently point at a different file. Rejected.
- **Keep the whole index on the vault itself**: the index travels with the data, but it is
  unavailable exactly when the drive is unplugged — which is when the user asks "do I still
  have that report?". Rejected: the host keeps the catalog, the vault keeps only its
  manifest.
- **SHA-256 every file on the first scan**: gives content identity and rename detection, at
  the cost of reading every byte of a multi-hundred-gigabyte archive before the assistant is
  useful. Rejected for the initial pass; hashing arrives with text extraction, per file and
  on demand.
- **Follow symlinks**: convenient for curated folder trees, but it lets a scan escape its
  authorised root, can loop forever, and can silently walk into an unrelated tree or a
  mounted network share. Rejected: symlinks are skipped and reported.
- **Delete catalog rows when a vault is offline**: keeps the catalog truthful about the
  present, but destroys the record of what the vault contains and forces a full rescan on
  every plug-in. Rejected: only a complete scan may change presence, and only to `MISSING`.
- **Mark missing from an incomplete scan**: one permission error would look like a mass
  deletion. Rejected outright — `marked_missing` stays 0 unless the snapshot is complete.

## Consequences

- Logical URIs survive remounting, drive-letter changes and moving a folder: identity is
  `(root-id, relative-path)` for the life of the record.
- An unplugged vault keeps answering "you have these files, they lived at these paths".
- The catalog is disposable: deleting the database loses scan history, not user data.
- An incomplete scan is a first-class, visible outcome (`complete=false`, bounded error
  summaries, non-zero CLI exit) rather than a silent partial truth.
- Scans of the same root serialise on SQLite's write transaction; the later complete
  snapshot becomes the current view. No distributed locking, no daemon coordination.
- Nothing in this phase reads file contents, so the first scan of a large archive stays
  fast and cheap; the price is that identical files are not recognised as identical until a
  later phase adds content fingerprints.

