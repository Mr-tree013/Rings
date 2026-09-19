# ADR-0002 — Persist Every External Input as an InboundEvent

## Title

All external input is persisted as an `InboundEvent` before any processing.

## Status

Accepted

## Context

Inputs arrive from mail, the mobile web surface, the local filesystem and scheduled
jobs. Processing is retried, interrupted and replayed. If processing is driven directly
by an external side effect (reading an IMAP message inline, handling a web request
inline), then a crash mid-way loses the event, a retry duplicates work, and there is no
single place to answer "what did the system see?".

## Decision

Every external input becomes a persisted `InboundEvent` first. Classification, case
creation and downstream processing consume `InboundEvent` records; they never read the
external system as their source of truth. Event identity is derived from the producing
system (for mail: `UIDVALIDITY + UID`, plus `Message-ID` as a cross-check).

## Alternatives Considered

- **Process inline**: fewer tables, but no replay, no audit trail and unavoidable
  duplicate processing after crashes. Rejected.
- **In-memory queue**: loses work on restart and cannot explain past behaviour.
  Rejected.
- **Use an external broker (Redis/Kafka)**: operational weight with no benefit for a
  single-machine assistant. Rejected.

## Consequences

- The Event Inbox becomes the single audit entry point and the basis for deduplication.
- The store must be transactional; SQLite (ADR-0003) provides that.
- Consumers must be idempotent, because an event may be processed more than once.

