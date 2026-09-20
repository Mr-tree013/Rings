# ADR-0020 — Durable UIDVALIDITY-Aware IMAP Ingestion

## Title

Inbound mail is received over TLS, identified by `(account, mailbox, UIDVALIDITY, UID)`, archived
as content-addressed raw RFC822, and bridged to exactly one `InboundEvent` by durable
reconciliation.

## Status

Accepted

## Context

Phase 5 is the first time this project *receives* something from the outside world. The Event
Inbox, the daemon supervisors and the "untrusted external input" rules already exist; what is new
is IMAP, a protocol whose identity model is genuinely subtle.

Three facts shaped this design:

- **a UID is only meaningful inside a UIDVALIDITY.** Servers may rebuild a mailbox and renumber
  everything; continuing an old cursor across that boundary silently skips mail. The identity is
  `(account, mailbox, uidvalidity, uid)`, and nothing may treat a UID as durable on its own.
- **a cursor is an optimisation, not a proof.** No polling design can promise exactly-once
  delivery across arbitrary server rebuilds. What it can do is advance the cursor only over mail
  it has durably handled, make reprocessing idempotent, and reconcile a bounded window when the
  mailbox identity changes.
- **mail content is hostile input by default.** Subject lines, bodies, HTML and attachment names
  arrive from strangers. Phase 5A stores them; it never interprets them, never renders them and
  never executes anything it finds inside them.

## Decision

1. Mail ingress uses IMAP over TLS.
2. Phase 5A is receive-only; SMTP is not implemented.
3. Mail accounts are explicitly configured; there is no account discovery.
4. Passwords and app passwords are environment-only secrets.
5. IMAP polling is daemon infrastructure, not an LLM capability.
6. IMAP operations use stdlib `imaplib` behind an async port.
7. Blocking IMAP work runs off the asyncio event loop.
8. Mailbox incremental identity is `(account_id, mailbox_name, uidvalidity, uid)`.
9. A UID is never treated as globally persistent by itself.
10. A UIDVALIDITY change triggers bounded reconciliation instead of continuing the old cursor.
11. Reconciliation uses message identity and fingerprint evidence, and preserves existing stable
    `MailMessage` ids when the match is confident.
12. Bounded reconciliation does not claim perfect exactly-once or no-miss semantics across
    arbitrary server rebuilds.
13. Raw RFC822 source is stored locally outside the Git repository.
14. Raw messages are content-addressed by SHA-256.
15. Physical raw-mail paths are storage implementation details, not message identity.
16. `MailMessage` has a stable internal UUID independent of any IMAP UID.
17. The Message-ID header is useful evidence but is not trusted as a globally unique database key.
18. Threading headers (`Message-ID`, `In-Reply-To`, `References`) are preserved for later thread
    linking.
19. Attachment metadata and hashes are preserved; attachment files are not separately materialised
    in Phase 5A.
20. Mail parsing never executes HTML or scripts, and never opens an attachment.
21. `BODY.PEEK` and read-only mailbox access mean a sync never marks a message as read.
22. New logical messages are bridged to the Event Inbox using the stable `MailMessage` UUID.
23. The bridge is crash-recoverable: a persisted message without an `InboundEvent` record is
    repaired on a later sync.
24. An `InboundEvent` carries stable message identity and minimal metadata, never the body.
25. Message bodies, subjects and headers are untrusted external input.
26. Phase 5A performs no LLM classification.
27. Phase 5A performs no automatic Task or Case creation.
28. Phase 5A performs no reply drafting.
29. Phase 5A performs no sending.
30. One account's failure does not stop unrelated accounts or other daemon services.
31. Missing mail credentials do not stop `index-sync` or `scheduler`.
32. Daemon mail polling is periodically reconciled; process memory is not authoritative.

Additional frozen details:

- **TLS only, verified.** `IMAP4_SSL` with `ssl.create_default_context()`. There is no plaintext
  mode, no STARTTLS option and no "skip verification" switch in configuration.
- **One conversation per fetch, in one worker thread.** `connect → login → select(readonly=True) →
  UID SEARCH → UID FETCH → logout` runs inside a single `asyncio.to_thread`, with a socket timeout
  (default 30s, 10–300s configurable). Oversize messages are fetched with `BODY.PEEK[HEADER]`, and
  bodies with `BODY.PEEK[]`.
- **Bounded initial history.** The first sync imports the newest `initial_fetch_limit` messages
  (default 500), not the whole mailbox; deeper backfill is a later phase. The cursor is the highest
  UID *this batch stored*, so nothing is skipped by a large server-side gap.
- **Batch atomicity.** Messages, locations, attachments and the cursor advance in one SQLite
  transaction. A failure rolls the whole batch back and leaves the cursor untouched, so a crash
  replays work rather than losing it.
- **Raw objects are written before the database transaction.** The content-addressed write is
  atomic (temp file in the same directory, flush, `fsync`, rename) and an existing object is
  verified and reused rather than overwritten. A failed transaction can leave an unreferenced
  object; that is the safe direction, and a future maintenance pass can collect those orphans.
- **Fingerprints are distinct from raw bytes.** `raw_sha256` addresses the exact fetched bytes;
  `content_fingerprint` is a canonical SHA-256 of the parsed logical message (Message-ID, From,
  Date, Subject, body when available, ordered attachment hashes, server size). A message with no
  body (oversize) still gets a fingerprint and a durable row.
- **Reconciliation matches only strong evidence**: identical raw bytes, or the same non-blank
  Message-ID *and* the same content fingerprint, or the same fingerprint with matching size,
  sender and date header. The same Message-ID with different content is a **conflict**: a new
  message is created and the conflict is counted. Historical messages and locations are never
  deleted when a rebuild no longer shows them.
- **The bridge is repaired every round**, bounded to 100 messages, so recovery does not depend on
  the server producing new mail. Both crash windows are covered: a message without its event
  creates one, and an event without its link is recognised as an idempotent duplicate and linked.
- **Credential name derivation.** `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD`, upper-cased with
  hyphens as underscores. Configuration cannot name a variable, and a `password`-style key is
  rejected by the strict parser. The secret is read only at composition time and never stored on a
  domain object, logged or included in an error.
- **No EventWorker.** There is still no real mail handler; Phase 5A writes `InboundEvent` records
  and stops. A no-op handler would only pretend that mail is being processed.

## Alternatives Considered

- **Use the UID alone as durable identity**: the failure mode that destroys trust — a rebuilt
  mailbox silently maps old UIDs to new messages. Rejected.
- **Make Message-ID a UNIQUE key**: real mailboxes contain duplicates, missing headers and forged
  ids, so this rejects legitimate mail and cannot express two messages sharing a header. Rejected;
  Message-ID is indexed evidence.
- **Poll only unread messages**: it couples synchronization to the user's reading behaviour, and it
  requires a flag a read-only client must not set. Rejected.
- **Mark messages as seen**: a sync must not change the user's mailbox. Rejected; read-only select
  plus `BODY.PEEK`.
- **Store passwords in `config.toml`**: a configuration file gets copied between machines and
  pasted into issues. Rejected; environment-only, with a derived name.
- **Put complete email bodies into `InboundEvent`**: the event layer would become a second,
  unbounded, untyped copy of the mailbox. Rejected; the event carries identity only.
- **Run `imaplib` on the event loop**: it is blocking I/O and would stall every other service.
  Rejected; the adapter owns the worker-thread boundary.
- **Implement SMTP now**: sending is an external side effect needing its own approval design
  (ADR-0006). Rejected for this phase.
- **Let a model deduplicate messages**: identity decisions must be deterministic and auditable.
  Rejected.
- **Delete old message records after a UIDVALIDITY change**: the project would lose its own history
  of what it received. Rejected; historical rows are preserved and only the cursor moves.

## Consequences

- Mail becomes durable, local state that later phases can classify, thread and answer from, without
  re-reading the mailbox.
- The identity rules are testable: `(account, mailbox, uidvalidity, uid)` uniqueness, the
  reconciliation match table, batch atomicity and both bridge crash windows are covered by real
  SQLite tests, and the IMAP command sequence is covered by contract tests against a strict fake.
- Mail content never reaches a model in this phase, so the untrusted-input rules of ADR-0017/0019
  apply to something that is not yet sent anywhere.
- Costs and limits: the initial history is bounded; deletion on the server is not mirrored;
  attachments are metadata only; and cancellation has a Python-level limit — stopping the await
  stops application progress, but a blocking stdlib IMAP call already running in its worker thread
  may finish later. That is why every socket has a finite timeout.
