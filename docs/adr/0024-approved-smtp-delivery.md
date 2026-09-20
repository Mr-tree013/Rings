# ADR-0024 — Exact Approved SMTP Delivery with Ambiguous-Result Reconciliation

## Title

Approved mail leaves the machine as an immutable snapshot, exactly once, and an ambiguous send is
resolved by looking for its Message-ID — never by sending it again.

## Status

Accepted

## Context

ADR-0023 built the boundary: an immutable `ActionRequest`, a human approval bound to its exact
fingerprint, a single-use approval, and an `ExecutionRun` that records what happened. Phase 6A
shipped it with an **empty** executor set, so nothing could actually happen.

Phase 6B gives that boundary its first real capability, and SMTP is the hardest possible first
one, because email is the one protocol where "I don't know" is a normal outcome:

- **the body is handed to the server before the client learns the verdict.** A connection that
  drops after the terminating dot may have left a delivered message behind. Calling that a
  failure and retrying is how people send the same mail twice;
- **approval must cover bytes, and the bytes must not move.** A draft is editable; the action that
  was approved is not. If sending re-read the draft, the user's approval would cover text they
  never saw;
- **a Message-ID is the only handle on an already-sent message.** It has to exist before approval
  (so it can be part of the payload) and stay the same forever after (so a Sent-folder search can
  find *that* message);
- **a Sent folder is not a reliable witness.** It may not exist, may not be readable, may be
  written later, or may hold two copies. Absence is not evidence of absence.

## Decision

1. SMTP send is the first production `ActionExecutor` capability.
2. Preparing a send snapshots one exact `MailDraft` version into an immutable `ActionRequest`.
3. Recipient, From, Subject, body, Message-ID and reply-thread headers are deterministic local
   fields.
4. The model never chooses recipients, the SMTP account or the Message-ID.
5. Editing a `MailDraft` after preparation does not change the `ActionRequest`.
6. Changed draft content requires a new `ActionRequest` and a new `Approval`.
7. One `MailDraft` version may produce at most one mail-send `ActionRequest`.
8. A draft with unresolved `needs_user_input` cannot be prepared until the user acknowledges it.
9. SMTP credentials are environment-only.
10. Only TLS-protected SMTP is supported.
11. Approval is consumed immediately before SMTP execution, through the existing Phase 6A
    transaction boundary.
12. No automatic SMTP retry exists.
13. A definite pre-delivery rejection is `FAILED`.
14. A connection or protocol failure at or after `DATA` may have been accepted, and is `UNKNOWN`.
15. `UNKNOWN` is never blindly retried.
16. A stable RFC Message-ID is generated at preparation time and is part of the approved payload.
17. Sent-folder reconciliation uses that exact Message-ID.
18. Finding the exact Message-ID can resolve `UNKNOWN`/`RUNNING` to `SUCCEEDED`.
19. Not finding it does **not** prove the message was not sent.
20. Reconciliation is explicit and user-triggered in V1.
21. No automatic resend command exists.
22. SMTP sending does not create or modify Tasks or Cases beyond the existing Case/Action records.
23. `EventWorker`, `Scheduler` and `MailAnalysis` never send email.

Additional frozen details:

- **Acknowledgement is a version.** `pw mail draft acknowledge` records
  `needs_user_input_acknowledged_at` and advances the draft version; the questions themselves stay
  as an audit trail. Any later subject or body edit clears the acknowledgement, so the questions
  have to be read again for the text that would actually be sent. Preparation refuses an
  unacknowledged draft with `MailDraftNeedsUserInput` and creates nothing.
- **Configuration is all-or-nothing.** `smtp_host`, `smtp_port`, `smtp_security`, `smtp_username`,
  `from_address` and `sent_mailbox` are optional as a group; a partial block is a configuration
  error, because half a sender is a trap. `smtp_security` accepts only `starttls` or `ssl` — there
  is no plaintext mode and no switch to skip certificate verification. The credential is
  `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD`, derived from the account id, separate from
  the IMAP secret, read only at composition time, and never stored, logged or placed in a payload.
- **The payload is a typed value.** `MailSendPayload` carries the schema version, the draft
  identity and version, the account, the derived headers and the frozen body. `from_payload` is
  strict: an unknown field, a missing field or an unsupported schema version is an error, which is
  what keeps `smtp_host`, usernames and passwords structurally unable to appear in a payload.
- **One Message-ID, generated before approval.** `<128 random bits>@<sender domain>`, minted by an
  injected factory in `adapters/mail/message_ids.py` (the second and last module allowed to read
  system randomness). It appears in the approved payload, in the fingerprint, in the transmitted
  bytes and in the Sent-folder search, and is never regenerated on a retry or a reconciliation.
- **Reply headers are derived locally.** From the message the draft replies to: `In-Reply-To` is
  its normalized `Message-ID`, and `References` is its references plus that id, deduplicated and
  bounded. A message without a usable `Message-ID` produces no threading headers rather than an
  invented one.
- **An edit does not disturb the prepared action.** `pw mail send show` prints the approved
  version, the current draft version, and a warning when they differ; the action still sends
  exactly what it snapshotted, and a new message requires advancing the draft and preparing again.
  An approval can never transfer to different content.
- **The database enforces one-version-one-action.** `mail_send_links` has a unique
  `(draft_id, draft_version)` and a unique `rfc_message_id`, and the action and its link are
  inserted in one transaction, so re-preparing without advancing the draft is refused rather than
  producing a second letter from the same approval.
- **Capability is checked before the approval is spent.** `ActionExecutor.supports(action)` is a
  pure, offline question ("is this account configured? is there a credential? is the payload
  readable?"), and the execution service asks it *before* consuming anything. A missing SMTP
  credential therefore produces `CapabilityUnavailable` with the approval still valid and no run
  created. Preparation deliberately does not require the credential: prepare, review and approve
  are all possible on a host that cannot send yet.
- **The SMTP conversation is written out.** `EHLO → [STARTTLS → EHLO] → LOGIN → MAIL FROM →
  RCPT TO → DATA → QUIT` on `smtplib` with `ssl.create_default_context()`. `sendmail()` is not
  used, because a black box cannot say whether the body had already been handed over — and that is
  precisely the difference between `FAILED` and `UNKNOWN`.
- **Failure classification is by stage.** Authentication rejection, sender refusal, all
  recipients refused and a response-level `DATA` rejection are `FAILED` (the server looked and
  said no). A transport or protocol failure after the data was written is `UNKNOWN`. Both spend the
  approval: the difference is only whether it is honest to retry.
- **Reconciliation is read-only and three-valued.** One `select(readonly=True)` on the configured
  Sent mailbox, a `HEADER Message-ID` search, and an **exact normalized comparison** of every
  candidate's header (a server answering `<x@example>` for `<x@example.evil>` is not a match).
  Exactly one match is `FOUND`; none is `NOT_FOUND`; two are `AMBIGUOUS`. A `FOUND` result
  promotes the run to `SUCCEEDED`, marks the action `EXECUTED` and writes the audit row in one
  transaction. Every outcome is recorded forever; nothing is ever deleted.
- **A missing mailbox is not a failure.** No Sent mailbox, no IMAP credential, a transport error
  or a protocol error all record `UNAVAILABLE` and leave the execution state exactly as it was.
- **No resend path exists.** `NOT_FOUND` does not mean failed, and there is no command, scheduled
  job or automatic retry that would send again. A user who wants a different outcome must cancel
  the action, prepare a new one from an advanced draft and approve that. `pw mail send reconcile`
  reports "do not resend blindly" and stops.

## Alternatives Considered

- **Send the draft as it stands at execution time.** The approval would cover text the user never
  saw. Rejected; the action snapshots a version.
- **Regenerate the Message-ID per attempt.** A second id makes "did this exact message arrive?"
  unanswerable, which is the whole point of reconciliation. Rejected.
- **Call `sendmail()` and treat any exception as a failure.** It cannot distinguish "the sender was
  refused" from "the connection dropped after the body was written". Rejected; the sequence is
  explicit and the stage decides.
- **Retry a `FAILED` send automatically.** The server may have accepted the message and failed
  while answering. Rejected; a retry needs a fresh human approval.
- **Treat `NOT_FOUND` as a definite failure.** Sent folders are not reliable witnesses. Rejected.
- **Pick one of two matching UIDs.** Choosing between two copies of the same id is a guess about
  the user's mailbox. Rejected; `AMBIGUOUS` stays unresolved.
- **Auto-reconcile from the daemon.** It would put a mailbox read on a schedule the user did not
  ask for. Rejected in V1; reconciliation is explicit.
- **Require the SMTP credential at preparation time.** It would make "prepare and review now, fix
  the credential later" impossible. Rejected; `supports` handles it before consumption instead.
- **Send from the daemon when a draft exists.** The mailbox cannot be allowed to cause an outbound
  message. Rejected.
- **Store the SMTP password with the action so it is available at execution.** A secret in a
  durable row is a secret in every backup. Rejected; the environment is the only source.

## Consequences

- The project can now send a mail the user approved, and can prove afterwards what was approved:
  the payload, the fingerprint, the approval, the run and the reconciliation history are all
  durable and locally resolvable.
- The ambiguous case is honest rather than convenient. An unresolved send blocks further
  execution of that action until a lookup confirms it, and the user is told that
  non-confirmation is not proof.
- Costs and limits: drafting still costs a model call, but sending costs nothing extra and never
  calls a model; the Sent mailbox must be configured and readable for confirmation; there is no
  automatic retry, no scheduled resend and no "force send"; and a genuinely lost message has to be
  handled by a person preparing a new action.
