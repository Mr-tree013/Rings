# ADR-0012 — Rebuildable Per-Root Full-Text Knowledge Index

## Title

Keep extracted text in a per-root, rebuildable FTS5 index, and make every content hit carry
a source span.

## Status

Accepted

## Context

The catalog from ADR-0011 knows *what exists*. This phase makes it possible to search what
those files actually say, without turning personal documents into an unmanaged second copy
of the user's data.

Three constraints shape the design:

- the runtime database (`assistant.db`) is the authority for events and catalog metadata; it
  must not slowly fill with document text;
- an archive vault is cold storage — its content belongs on the drive, not on the host;
- a search result is only useful if the user can open the file and find the passage, so every
  hit has to point at a page or a line range.

## Decision

1. `assistant.db` keeps runtime authority and the lightweight catalog only.
2. Extracted text and the FTS index are **derived, rebuildable data**.
3. An archive vault's index lives at `<vault root>/.pa/index.sqlite3`.
4. A local root's index lives at
   `~/.cache/growing-assistant/knowledge/<root-id>/index.sqlite3`.
5. When the vault is not attached: catalog metadata stays queryable, vault full-text search
   is unavailable, and vault content is **not** copied to the host.
6. A damaged index, or one from an incompatible schema version, is rebuilt rather than
   repaired; it is never treated as irreplaceable user data.
7. Every index database is bound to `root_id`, `storage_kind` and `schema_version`, so one
   root can never read another root's index.
8. This phase uses SQLite FTS5.
9. The tokenizer is FTS5 `trigram`: Chinese substring search behaves predictably, and file
   names, code and chat text need no language-specific segmentation.
10. Queries shorter than three Unicode code points fall back to a literal `LIKE` search.
11. User input is plain text: it is never executed as FTS query syntax.
12. The index stores extracted text only; original files are never modified.
13. Strong SHA-256 fingerprints are computed only for files that actually get extracted;
    the metadata scan still never reads file contents.
14. After an indexing failure, the old text for that document stops being searchable: a
    stale index is worse than a missing one.
15. Every chunk carries a `SourceSpan`: an inclusive line range, or a page number.
16. No rename detection and no cross-document deduplication.
17. No embeddings or vector search in this phase.

Supporting rules:

- Content extraction is only attempted for a whitelist of text suffixes and `.pdf`; anything
  else is recorded as `UNSUPPORTED` without opening the file.
- Text is decoded as UTF-8 (with or without BOM). Other encodings are an ERROR, not a guess.
- PDFs are read with `pypdf`: no OCR, no password cracking, pages without a text layer are
  skipped, and a PDF with no text at all is `EMPTY` rather than `INDEXED`.
- A file is revalidated at read time: relative-path validation, no symlink anywhere in the
  path, regular file only, and size/mtime must match the catalog before *and* after reading.
- The unchanged-skip optimisation (matching size and mtime plus extractor name/version) is
  combined with a stat-only revalidation, because a file can change without the catalog
  knowing.
- Content hits are ranked by `bm25` inside one root; across roots, ranks are fused with
  Reciprocal Rank Fusion (`1 / (60 + position)`), because BM25 scores from different
  databases are not comparable.
- Search returns two clearly separated signals: content hits (with source spans) and
  metadata-only hits from the catalog.

## Alternatives Considered

- **Keep all text on the host**: simplest single-database design, and the worst privacy
  outcome — the host would hold a copy of every archived document, and the copy would go
  stale the moment the drive left. Rejected.
- **Put everything in the runtime `assistant.db`**: one file to back up, but it mixes
  authority with derived data, makes vault content host-resident, and turns a rebuildable
  cache into something that must never be lost. Rejected.
- **One global FTS database for all roots**: fewer files, but root identity and rebuild
  scope blur: deleting one root's index would touch every other root, and a mismatched vault
  could be searched under another root's identity. Rejected.
- **`unicode61` tokenizer**: good for space-separated languages, poor for Chinese substring
  queries, and it would need a segmentation policy the project does not have. Rejected.
- **Raw `MATCH` syntax from user input**: convenient for power users, but `a:b OR NEAR(x)`
  would become query semantics, and a typo becomes a syntax error. Rejected: user text is
  quoted as a phrase, with a literal `LIKE` fallback for short queries.
- **Hash every file during the metadata scan**: would give content identity early, at the
  cost of reading every byte of a large archive before anything is searchable. Rejected; the
  hash is computed when, and only when, a supported file is extracted.
- **Add OCR and embeddings in the same phase**: tempting scope, but both change the cost and
  the failure modes of indexing (models, languages, sizes). Rejected; they need their own
  decisions.
- **Serve stale text when re-indexing fails**: keeps results looking complete while quietly
  describing an older version of the file. Rejected.

## Consequences

- Vault content stays on the vault: the index travels with the drive, and unplugging it
  leaves the host with metadata and no document text.
- Search results are verifiable: every content hit names a file URI plus a page or line
  range, and the snippet is capped at ~300 characters.
- Rebuilding is always possible and cheap to reason about: `pw reindex --force` re-reads and
  rewrites derived data.
- Index databases use `journal_mode=DELETE` rather than WAL: on removable media the extra
  `-wal`/`-shm` files are a liability when a drive is unplugged mid-write, and losing an
  index transaction only means re-running `pw reindex`.
- Indexing is explicitly two-step: `pw vault scan` updates the catalog, `pw reindex` updates
  the index. A file that was never catalogued is never indexed, and a file that changed
  without a rescan produces an ERROR until the catalog is refreshed.
- A modified-but-not-rescanned file never silently keeps serving old text: the skip path
  revalidates size and mtime (metadata only, no read) before skipping.
- Per-root indexes mean cross-root ordering is a merge problem, solved with RRF rather than
  by comparing incomparable BM25 scores.

