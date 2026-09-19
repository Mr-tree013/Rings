# ADR-0006 — Side Effects Require a Fingerprint-Bound Approval

## Title

Every external side effect requires an `ActionRequest` approved by the human against an
action fingerprint.

## Status

Accepted

## Context

The assistant will send mail and fill institutional forms. A wrong send is
irreversible; a wrong form submission can be worse. The risk is not only a mistaken
model output, but also a stale decision: content the user approved may change before
execution, and an approval must not silently cover the changed content. The runtime must
also be structurally incapable of approving its own actions.

## Decision

- External effects are modelled as `ActionRequest` records with a deterministic
  fingerprint over the exact effect payload and target.
- Execution requires an `Approval` bound to that fingerprint, produced only by an
  explicit human action through a surface the runtime cannot forge.
- If the `ActionRequest` changes, the fingerprint changes and the existing `Approval`
  is void; a new approval is required.
- There is no code path by which the agent runtime approves an action.
- High-risk operations (course withdrawal, application cancellation, dormitory
  withdrawal) are absent from the capability set entirely.

## Alternatives Considered

- **Confirmation as a prompt instruction** ("always ask before sending"): depends on
  model compliance and is untestable as a guarantee. Rejected.
- **Session-level approval** ("the user trusts this workflow"): broadens authority
  silently when payloads change. Rejected.
- **Auto-approval after N seconds unless cancelled**: turns inattention into consent.
  Rejected.

## Consequences

- Irreversible actions always include an explicit user step; the design accepts the
  added friction.
- `SENDING_UNKNOWN` and similar uncertain states must be surfaced, never auto-resolved.
- Approval records are part of the audit trail and are testable in regression tests.

