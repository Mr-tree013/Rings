# ADR-0007 — FTS5 Before Vector Search

## Title

Use SQLite FTS5 for knowledge retrieval first; consider embeddings only with evidence.

## Status

Accepted

## Context

The assistant must answer from personal documents and cite the original text. The
corpus is small (personal files, not a web corpus) and mixed Chinese/English. The
requirement is traceability: an answer that cannot be traced to a line in a file is not
acceptable. Embeddings add a model dependency, an API cost per document and per query,
and a retrieval step whose failures are hard to explain.

## Decision

Phase 1 retrieval uses SQLite FTS5 with a tokenizer suited to Chinese substring matching
(`trigram`), combined with deterministic metadata filters. Queries return file path and
line/section anchors. Embeddings or hybrid search are introduced only if evaluation
shows a concrete class of queries that FTS5 cannot serve, and only with that evidence
recorded in a new ADR.

## Alternatives Considered

- **Vector search from the start**: better recall on paraphrase, but opaque, costlier,
  and unnecessary for a corpus where the user knows the words they wrote. Rejected for
  Phase 1.
- **An external search engine (Elasticsearch/Meilisearch)**: strong full-text features,
  but another service to run and secure. Rejected.
- **Grepping files per query**: fine at tiny scale, but no ranking, no metadata filters
  and no index freshness story. Rejected as the long-term answer.

## Consequences

- Retrieval is reproducible and explainable; a miss can be debugged by looking at the
  index and the query.
- No embedding pipeline, no vector store, no per-query model cost for search.
- Chinese recall depends on the tokenizer choice; this must be validated with real
  queries before the feature is considered done.

