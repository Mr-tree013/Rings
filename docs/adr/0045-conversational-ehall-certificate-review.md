# ADR-0045 — Conversational eHall Certificate Review

## Title

The conversation surface gains exactly two eHall-facing operations — `ehall.status` and
`ehall.certificate.prepare` — over the certificate pipeline that already exists, with the exact
prepared `ActionRequest` as the only thing a human can approve and the existing approval and
execution services as the only path to a submission.

## Status

Accepted

## Context

Phase 11E asks for a conversational eHall. What already exists is one bounded capability, built and
reviewed in Phase 7:

```text
pw ehall certificate prepare --case CASE --field KEY=VALUE …
        │
        ├── the case must be OPEN
        ├── live read-only inspection of the one whitelisted service
        ├── every key, required field and option validated locally
        ▼
immutable ActionRequest("ehall.submit-certificate")  ──►  pw action challenge/approve/execute
```

Three properties of that pipeline decide this ADR:

* `EHallCertificateService.prepare()` types nothing. A preparation that filled the remote form would
  trigger the portal's autosave before anyone approved anything.
* The page contract — service identity, page markers, ordered field definitions, required materials
  and the submit control — is fingerprinted, and a changed page makes the prepared action stale.
* The gateway port has exactly two verbs, `inspect_form()` and `submit_certificate()`. There is no
  URL, no selector, no `goto`, no generic submit.

What was missing is the middle of the errand: a person speaking to Tree cannot currently reach that
pipeline at all. `conversation_external_reviews` could only describe `mail.send`, the conversation
vocabulary has no eHall operation, and there is no case-facing orchestration that would satisfy
`prepare`'s OPEN-case requirement.

The dangerous version of this phase is easy to state: teach the model to drive a browser. Every
element of that — an arbitrary URL, a selector, an inferred identity value, an autofilled profile
field, an automatic retry after an ambiguous submit — is a way to send something to a university on
the user's behalf that nobody read.

## Decision

1. **Only the existing certificate capability is reachable.** The action type stays
   `ehall.submit-certificate`, produced by `EHallCertificateService.prepare()`. No second eHall
   pipeline is created.
2. **Two operations exist, and no more:** `ehall.status` (`READ`) and
   `ehall.certificate.prepare` (`LOCAL_WRITE`).
3. **There is no `ehall.submit`, no `approval.create`, no `action.execute`, no `browser.*`, no
   `http.*` and no `filesystem.*` operation.** A submission has exactly one entry point in this
   project: `pw action execute`.
4. **`ehall.status` is read-only and local-first.** It reports whether the pipeline is configured,
   whether a usable session exists, and — when the host can inspect it — the live form's service
   identity, ordered fields (key, label, kind, required, allowed options), required materials and
   page-contract fingerprint. It never types, never submits and never accepts a URL or a selector.
5. **Missing parameters are a question, not a guess.** A preparation that lacks a required field,
   or names a field the live page does not have, is refused locally and answered with the form's own
   requirements so the user can supply them.
6. **An unsupported certificate is refused.** Only `证明书申请` is whitelisted, and a value that is
   not one of the options the page offers is rejected by `build_field_values()`.
7. **No guessed form fields.** The only field keys a preparation may contain are the keys of the
   live snapshot; the handler validates against that snapshot before the action exists.
8. **No guessed identity or profile values.** Every submitted value must occur in the user's own
   message, checked deterministically before anything is prepared. There is no autofill: no
   `ConfirmedFact`, no knowledge search, no mail, no model memory, no default profile.
9. **The model may prepare; it may never approve.** Preparation produces the existing immutable
   `ActionRequest` and stops.
10. **Preparation requires an OPEN case.** `prepare()` already refuses a terminal case
    (`CaseNotOpen`). The conversation either *uses* an existing case id or opens one bounded case
    through the existing `CaseService`. There is no conversational case.create/complete/cancel:
    case lifecycle is not a model surface.
11. **The exact `ActionRequest` payload is the preview.** The preview is rendered from the stored
    payload, never from the snapshot, the plan or the model's summary, so the thing reviewed and the
    thing that would be submitted cannot drift.
12. **The preview carries every human-relevant submitted field**: the service, every key with its
    label and value, the required materials, the page-contract fingerprint and the fixed local
    consequence text.
13. **The preview carries no credential, cookie, session, selector or internal URL.** Those never
    exist in the payload.
14. **A first request never submits.** "帮我申请在读证明" prepares, shows, and stops.
15. **Generic acknowledgement never submits.** `可以`, `好`, `ok` and `继续` are not in the
    confirmation vocabulary and settle nothing external.
16. **Only action-specific phrasing settles a review**: `确认提交` (and its documented synonyms) for
    an eHall certificate, `确认发送` for a mail send. The two vocabularies are disjoint.
17. **One phrase settles at most one review.** Every review carries its own action type, and a
    confirmation is matched against that review's own vocabulary. A waiting mail send and a waiting
    certificate cannot both be settled by one sentence.
18. **The browser confirmation card bypasses the model.** A card click is settled by durable
    identity (`card id → review id`) through `ConversationCardService.settle`, which records the
    phrase a terminal user would have typed and reaches the same deterministic code. No `ModelPort`
    is constructed or called on that path.
19. **The card shows the same immutable payload** and offers `确认提交` / `取消`.
20. **Approval and execution remain authoritative.** Confirmation re-loads the action, re-validates
    the fingerprint, and then uses `ApprovalService` to issue a challenge and create one `Approval`,
    and `ActionExecutionService` to create and run one `ExecutionRun`. Nothing about this phase
    gives the conversation a second path.
21. **Editing the payload invalidates the review.** A changed content means a new `ActionRequest`
    with a new fingerprint and a new preview; the old review becomes `STALE` and its confirmation
    cannot touch the new action.
22. **Only one live review per conversation thread.** The existing partial unique index applies to
    the generalised table unchanged: preparing a second thing stales the first.
23. **An ambiguous external result is never retried.** `UNKNOWN` blocks the action exactly as it did
    for mail; the review records it and tells the user to check the portal by hand.
24. **The external-review table keeps a closed action-type vocabulary.** Migration 0023 widens
    `conversation_external_reviews.action_type` from the one reviewed mail type to the exact pair
    `('mail.send', 'ehall.submit-certificate')`. It stays a `CHECK`ed closed set; it is never
    arbitrary text, and every historical mail row is preserved byte for byte.
25. **`ALLOWED_EXTERNAL_ACTION_TYPES`, the persistence constraint, the confirmation vocabulary, the
    preview dispatch, the card builder and the integrity audit all name the same two values.** A
    third capability cannot be added by naming it in one place.
26. **Destructive eHall capabilities remain physically absent.** No drop-course, withdraw,
    cancel-application, delete, dorm-checkout or arbitrary-submit operation exists in this build,
    in the vocabulary, in the registry or in the gateway port.
27. **No credential is ever requested.** The project never asks for a university password, never
    types one and never stores one; the browser session is a private profile the user logs into by
    hand, and the conversation says so instead of asking.

### Rejected alternatives

- **A generic browser tool** (`browser.navigate`, `browser.click`). The whole point of ADR-0025 is
  that the model has no vocabulary in which to express an arbitrary page action.
- **Letting the model name a URL or a service.** The whitelisted service is a constant.
- **Autofilling from `ConfirmedFact` or the knowledge index.** A certificate application is a
  statement to the university; guessing a name or a student number is exactly the failure this
  phase must not have.
- **An implicit case per certificate, with no case id at all.** Preparing two certificates for one
  errand would then have no durable grouping, and the CLI's own audited path names a case.
- **A conversational `case.create`/`case.complete`/`case.cancel`.** Case lifecycle is not needed to
  use the pipeline, so it is not exposed; the minimum is "use an open case, or open one bounded
  case".
- **Model-generated confirmation phrases.** Confirmation is a closed vocabulary matched by
  deterministic code against the human's own words.
- **Automatic retry after an ambiguous submit.** The portal may have accepted it; retrying is how a
  person ends up with two applications.
- **Widening the review table to arbitrary action types.** A closed pair is auditable; free text is
  not.

## Consequences

### The closed loop

```text
user sentence
   │
   ├─ ehall.status            (READ)      → configured / session / live form contract
   │
   └─ ehall.certificate.prepare (LOCAL_WRITE)
            │  typed field map, local validation against the live snapshot
            │  every value must occur in the user's own message
            ▼
        OPEN case (existing, or one bounded case opened through CaseService)
            ▼
        EHallCertificateService.prepare()  ──►  immutable ActionRequest
            ▼
        ConversationExternalReview (WAITING)     ← zero Approval, zero ExecutionRun
            ▼
        deterministic preview from the ActionRequest payload   → STOP
            │
   ┌────────┴─────────────────────────┐
   │                                  │
text "确认提交"                 browser card "确认提交"
   │                                  │
   └────────────► ConversationExternalReviewService.confirm()  ◄──── no ModelPort
                          │  reload action, re-hash fingerprint
                          ▼
                  ApprovalService.create_challenge() + approve()
                          ▼
                  ActionExecutionService.execute()  ──► one ExecutionRun
                          ▼
                  EHallCertificateExecutor ──► gateway re-inspects, fills, clicks once
```

### What changed in storage

`migrations/0023_conversation_review_expansion.sql` rebuilds `conversation_external_reviews` with
the widened closed `CHECK` and copies every row across unchanged, so a v1.2 database keeps its mail
reviews, its fingerprint column, its one-live-review-per-thread index and its execution-consistency
rule. Nothing is added to the table: no credential, no cookie, no page content, no draft body.

### What could not be expressed

There is no operation that names a URL, a selector, a browser action, an arbitrary action type, an
approval or an execution. `EHallCertificateGateway` still has exactly `inspect_form()` and
`submit_certificate()`. The only eHall action type that can be constructed is
`ehall.submit-certificate`, and the integrity audit refuses a review row that names anything else.

## Implementation notes

- Vocabulary: `domain/conversation_review.py` (`ALLOWED_EXTERNAL_ACTION_TYPES`, per-kind
  confirmation phrases), `domain/conversation_plan.py` (`ehall.status`,
  `ehall.certificate.prepare`).
- Handler: `application/conversation_capabilities/handlers.py` — deterministic validation against
  the live snapshot, the OPEN case rule, and no approval, no execution and no gateway submission.
- Settlement: `application/conversation_external_review.py` (closed per-kind pre-check) and
  `application/conversation_service.py` (per-kind phrase matching, one review at most).
- Cards: `application/conversation_cards.py` (`ConfirmationCardKind.EHALL_CERTIFICATE`), rendered
  from the immutable payload.
- Domain preview: `domain/ehall.py` (`EHallCertificatePayload.from_payload`), rebuilt for display
  only.
- Migration: `migrations/0023_conversation_review_expansion.sql`.
- Integrity: the external-review section of `pw integrity check` audits the closed action-type set,
  the `ActionRequest` relationship, the payload fingerprint and the review lifecycle.
