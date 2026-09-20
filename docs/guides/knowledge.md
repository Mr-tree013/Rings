# Knowledge and grounded answers

## What it does

Rings indexes the folders and archive vaults you configure, then answers questions **only** from that
material. Retrieval is local and term-based; the model never decides which files matter and never
supplies a citation.

Two kinds of roots are supported:

| Kind | Identity | Where the index lives |
| --- | --- | --- |
| `local` | `local://<root-id>/<relative-path>` | `$XDG_CACHE_HOME/growing-assistant/knowledge/<root-id>/index.sqlite3` |
| `vault` | `vault://<vault-id>/<relative-path>` | `<vault>/.pa/index.sqlite3`, so it travels with the drive |

A document's identity is `root_id` + relative path. The physical path is only this machine's current
location for that root and may change.

## Enable / configure

```toml
[indexing]
interval_seconds = 300
run_on_startup = true

[[storage.roots]]
kind = "local"
id = "university"
label = "University documents"
path = "/home/user/Documents/University"
enabled = true
```

An archive vault carries its own manifest, so its identity is verified rather than configured:

```bash
uv run pw vault init /mnt/e/archive --id archive-main --label "Personal Archive"
uv run pw vault status /mnt/e/archive
uv run pw vault scan /mnt/e/archive
```

## Common workflow

```bash
uv run pw roots list                 # configuration only; scans nothing
uv run pw sync                       # one reconciliation: scan → catalog → index
uv run pw sync --root university --force-index
uv run pw reindex --root university
```

Search returns hits with their location, and nothing more:

```bash
uv run pw search "important deadline"
uv run pw search "deadline" --root university --limit 5
```

Ask a grounded question:

```bash
uv run pw ask "What is the deadline for my SE lab?"
uv run pw ask "When is my SE lab due?" --root university --limit 6
```

```text
# Answer
# The submission deadline is October 23 at 23:59. [S1]
#
# Sources
# [S1] local://university/notice.md - lines 18-31
```

## Safety behavior

- **Answers never fall back to unsourced world knowledge.** With no evidence the model is not called
  at all; with insufficient evidence the answer is `insufficient_evidence` instead of a
  general-knowledge guess.
- Every answer segment must cite evidence supplied in that exact request. A citation to an unknown
  identifier is rejected deterministically rather than repaired.
- Source URIs, page numbers and line ranges come from local evidence metadata, never from model
  output.
- Every content hit carries a source span (`page N` or `lines A-B`). A metadata-only hit — a file
  name or path — is not evidence for a content claim.
- An offline vault is listed as offline; its content cannot be cited, and it is never silently
  ignored or copied into host storage.
- `pw ask` is read-only: it writes no index, creates no durable state and executes nothing.

## Commands

```text
pw vault init /mnt/e/archive --id archive-main --label "Personal Archive"
pw vault status /mnt/e/archive
pw vault scan /mnt/e/archive
pw roots list
pw sync
pw sync --root university --force-index
pw reindex --root university
pw search "important deadline"
pw search "deadline" --root university --limit 5
pw ask "What is the deadline for my SE lab?"
pw ask "When is my SE lab due?" --root university --limit 6
```

## Troubleshooting / limitations

- **v1 uses local FTS5 indexes, not a vector database.** Retrieval is term-based, so paraphrases and
  synonyms may miss.
- Indexes are derived data. `pw reindex` (or `pw sync`) rebuilds them from the originals, and losing
  an index costs time rather than documents.
- Scanning metadata never reads file contents; content is read only when indexing, after the
  file's size and mtime are re-checked.
- Symlinks are skipped, and a partial scan never marks unseen entries as missing.
- There is no OCR, no Office/archive expansion, and no filesystem watcher in v1: freshness comes
  from periodic reconciliation, so a change is noticed within one interval.

## Implementation notes

- Stable storage identity and the metadata catalog: [ADR-0011](../adr/0011-stable-storage-catalog.md)
- Rebuildable per-root index: [ADR-0012](../adr/0012-rebuildable-per-root-knowledge-index.md)
- Periodic reconciliation: [ADR-0013](../adr/0013-configured-root-reconciliation.md)
- FTS before vectors: [ADR-0007](../adr/0007-fts-before-vector-search.md)
- Grounded answers: [ADR-0019](../adr/0019-source-grounded-knowledge-answers.md)
