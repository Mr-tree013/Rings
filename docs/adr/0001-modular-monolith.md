# ADR-0001 — Modular Monolith

## Title

Use a modular monolith for growing-assistant, not microservices.

## Status

Accepted

## Context

The assistant must run continuously on one person's machine (WSL2) with the smallest
possible operational burden. It has four long-running concerns (mail, index, web,
scheduler) that share one domain model, one runtime database and one user. The main
risk to the project is not scaling; it is complexity that makes each change expensive
and every behaviour hard to explain.

## Decision

Build a single deployable daemon (`assistantd`) with strict internal module boundaries:
`domain`, `application`, `ports`, `store`, `adapters`. Dependencies point inward only.
Each long-running service runs inside its own exception boundary inside the daemon.

## Alternatives Considered

- **Microservices / separate processes per concern**: independent failure domains, but
  requires IPC, duplicated configuration, distributed state and versioning for a
  single-user tool. Rejected as premature.
- **One flat script per feature**: cheapest initially, but guarantees duplicated
  deduplication and approval logic across features, which are exactly the parts that
  must not diverge. Rejected.
- **Agent-CLI-driven orchestration** (a daemon that shells out to an agent for every
  event): minimal code, unpredictable cost, latency and reproducibility. Rejected for
  the daily execution path; retained as an optional exploration path for unknown
  errand types.

## Consequences

- One process to run, log and restart; one SQLite file as the runtime authority.
- Boundaries are enforced by convention and review, not by the network. The
  `AGENTS.md` rules and mypy/ruff configuration are the guardrails.
- Any future split into services remains possible because adapters and ports already
  isolate the external edges.

