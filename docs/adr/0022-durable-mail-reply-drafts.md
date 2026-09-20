# ADR-0022 — Durable Mail Reply Drafts with Explicit Knowledge Context

## Title

Reply drafts are local, user-triggered, durable content whose recipients and subject are derived
locally and whose personal-knowledge context is only ever fetched because the user asked for it.

## Status

Accepted

## Context

Phase 5A receives mail, Phase 5B threads and classifies it. Phase 5C is the first time the system
produces *outgoing words* — and the first time a personal question ("what are my office hours?")
could be answered with material from the user's own vault.

That combination creates two risks that a design cannot leave to prompt wording:

- **the mailbox is a remote control for the personal index.** If a draft were generated
  automatically for every incoming message, then anyone who can send an email could make the
  assistant search the user's files and put the result in a reply. The trigger for reading
  personal data must be the user, never a message;
- **a reply is a commitment made in the user's name.** A recipient, a subject and a body are
  three different decisions. A model may write prose; it must not decide who receives it, what it
  is called, or whether it leaves the machine at all.

The project already has the pieces this needs: a durable event pipeline (ADR-0010), a model port
without tools (ADR-0017), bounded evidence with request-local citation ids (ADR-0019), and
untrusted mail with deterministic threading (ADR-0020, ADR-0021). Phase 5C composes them.

## Decision

1. Reply drafting is explicit user-triggered work, never an `EventWorker` side effect.
2. Phase 5C never sends mail.
3. A `MailDraft` is durable local state.
4. Recipient and reply subject are derived deterministically, never chosen by the model.
5. `Reply-To` is preferred over `From`.
6. V1 implements Reply, not Reply-All.
7. The model generates body text only.
8. Incoming mail and thread content is untrusted data.
9. Personal knowledge is never retrieved solely because an untrusted email asks for it.
10. Knowledge retrieval requires an explicit user-supplied context query.
11. Without an explicit context query, no Knowledge index content is sent to the model.
12. Knowledge evidence uses the existing bounded `GroundedContext` boundary.
13. The model may identify used source IDs only from the exact supplied evidence.
14. Source IDs are validated locally.
15. Knowledge citations and provenance are stored separately; they are not inserted into the
    outgoing email body.
16. The model must not invent unsupported personal facts.
17. Missing information is surfaced as `needs_user_input`, not hallucinated.
18. Draft creation and editing has no SMTP, Approval or `ActionRequest` capability.
19. User edits use optimistic concurrency.
20. A future send operation must bind to the exact later draft payload, not blanket approval.
21. Draft generation does not alter Tasks, Cases, Planner, Scheduler or Knowledge state.
22. No automatic retry of paid model requests.

Additional frozen details:

- **Derived recipients.** `reply_to_addresses` is added to `mail_messages` (default `[]`), parsed
  with the stdlib address parser so a display name containing a comma cannot split an address.
  The resolver prefers `Reply-To`, then `From`, keeps header order, extracts strict `local@domain`
  mailboxes, and refuses anything containing whitespace, control characters or a second `@`.
  When nothing usable remains, `MailReplyRecipientUnavailable` is raised **before** any model call.
  `mail_content_fingerprint` is deliberately left unchanged: adding a field to it would give
  every already-stored message a new fingerprint, so a rebuilt mailbox would be reconciled as
  "same Message-ID, different content" and duplicate messages that are not duplicates.
- **Derived subject.** `reply_subject` is a pure function: blank or missing becomes `Re:`; an
  existing case-insensitive `Re:` prefix is preserved exactly, so a thread never accumulates
  `Re: Re:`; anything else becomes `Re: <subject>`. The model never emits a subject.
- **One editable part.** The model returns `body`, `used_source_ids` and `needs_user_input`, and
  nothing else — the schema is closed and its property set is pinned by a test. There is nowhere
  to put a recipient, a subject, a send instruction or an approval.
- **Explicit knowledge only.** The single call into the knowledge boundary sits behind
  `if query is None: return (), ()` in one small function; a structural test pins that shape and
  a behavioural test counts calls with a spy. A message asking to "search all my files" produces
  zero evidence and zero searches.
- **Supplied ids only.** `used_source_ids` is validated against the exact evidence of that
  request; an unsupplied `S999` raises `MailDraftInvalidKnowledgeReference` and no draft is
  stored. With no context query there is no evidence, so any citation is refused.
- **Provenance without content.** `mail_draft_sources` stores root, entry, chunk, logical URI and
  the source span per used source — identity and location, never the excerpt and never a
  filesystem path.
- **Bounded untrusted context.** The request carries the current message plus at most five
  earlier thread messages (3000 characters each, 12000 total) and the fixed reply subject. The
  resolved recipient addresses and `To`/`Cc` are not sent: recipients stay local, and the
  fingerprint records them for audit.
- **A body must be readable.** `body_status != AVAILABLE` (an oversize, header-only message) is
  refused with `MailDraftSourceUnavailable`; no provider call is made, because a reply cannot be
  invented from headers.
- **Read-only threading.** A draft reads thread membership if it exists and never creates it: a
  message the analysis pipeline has not threaded yet gets a single-message context and a `NULL`
  `thread_id`. Drafting therefore changes no mail state.
- **Audit fingerprint.** Canonical JSON and SHA-256 over the prompt and schema versions, the
  message id, recipients, subject, the ordered thread ids and content fingerprints, the context
  query, the root filter, and the identity of every supplied excerpt. It is for audit and
  debugging; it never authorises sending anything.
- **Optimistic edits.** `UPDATE … WHERE id = ? AND version = ?`; a mismatch raises
  `StaleMailDraftUpdate`, the version increments, `origin` becomes `user_edited`, and the
  generation provenance and recipients are preserved. An edit must change at least one field.
- **No background drafting.** Only `pw mail draft create` builds a service that holds a provider.
  Read and edit commands build a provider-free service, so they work on a host with no model.
- **No retry.** A provider failure leaves no draft row; the user may simply ask again. Nothing
  retries behind their back.

## Alternatives Considered

- **Draft automatically when a message arrives.** It would make an incoming email able to trigger
  a personal-index search and to spend money, and it would put words in the user's name without
  being asked. Rejected.
- **Let the model choose the recipient or the subject.** Both are header facts or trivial
  derivations; a model could address the wrong person or rename the thread. Rejected.
- **Reply-All in V1.** It multiplies the recipient set — including people the user never intended
  to answer — for a feature nobody asked for yet. Rejected.
- **Search the vault when the message asks a question.** The content of an untrusted message
  would then decide whether the user's private files are read. Rejected; the query is the user's.
- **Put the knowledge into the body as citations.** An outgoing email to a third party is the
  wrong place to disclose where the user's private notes live. Rejected; provenance is stored
  locally and never sent.
- **Store the retrieved excerpts with the draft.** Duplicating index content would create a
  second, stale copy of the user's files in a second place. Rejected; only identity and location.
- **Let the model fill in a missing personal fact "plausibly".** A confident wrong fact in an
  email is worse than a question. Rejected; unsupported facts become `needs_user_input`.
- **Blanket approval for later sending.** An approval that is not bound to exact bytes would
  authorise whatever the draft happens to say later. Rejected (ADR-0006); a future send must bind
  to the exact payload.
- **Last-write-wins edits.** Two editors (or two windows) would silently overwrite each other.
  Rejected; compare-and-set with an explicit version.
- **Retry the provider a few times.** A draft is a user-initiated action they can repeat in one
  keystroke, and silent retries spend money without a visible reason. Rejected.

## Consequences

- The user gains a reviewable, editable, local draft without ever granting the mailbox the
  ability to read the vault. The privacy boundary is testable: a spy proves zero searches without
  a query, and a structural test pins the single guarded call site.
- Drafts are ordinary durable rows: they survive restarts, keep their version history's current
  state, and can be inspected without a provider.
- Costs and limits: knowledge is used only when the user names a query, so a draft cannot
  opportunistically find a relevant note; `needs_user_input` stays attached to the draft until a
  later phase offers an acknowledgement flow; attachments and `Cc` are not considered; and the
  send path — with its own approval design — is deliberately still absent.
