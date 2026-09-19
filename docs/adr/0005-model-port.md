# ADR-0005 — All Model Access Goes Through a ModelPort

## Title

Access language models only through `ports.ModelPort`; DeepSeek is one adapter.

## Status

Accepted

## Context

The assistant uses models for extraction, drafting and classification. Model providers
change, prices change, and some work (extraction, re-ranking) needs a cheaper model than
others (drafting). Model calls are also the least reproducible part of the system and
the most likely to leak data if used carelessly.

## Decision

Define `ModelPort` in `ports/` as the only interface the application uses for model
access. Provider SDKs live in `adapters/` and never leak their types inward. Model
outputs are treated as **candidates**, never as facts or as authorisation to act.
Every call is recorded for audit (prompt version, model identifier, token usage,
result hash).

## Alternatives Considered

- **Call the DeepSeek SDK directly from application code**: fewer layers, but couples
  the codebase to one vendor and one wire format. Rejected.
- **Adopt an agent framework (LangChain/LangGraph) for model orchestration**: adds a
  large dependency and indirection for a workflow whose steps are explicitly modelled
  as state machines. Rejected for now; the boundary allows adding one later behind the
  port if a real need appears.
- **Let the model decide when to act**: conflicts with ADR-0006. Rejected.

## Consequences

- Swapping or adding a provider is an adapter change plus a contract test, not a
  refactor.
- Cost and latency are measurable per task type, so model choice can be tuned per node.
- Domain code stays free of model concepts; the vocabulary is `FactCandidate`,
  `ActionRequest` and `PlaybookCandidate`.

