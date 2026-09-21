# ADR-0037 — Conversational Outbound Mail and Deterministic Recipient Resolution

## Title

A new letter is composed in a conversation, resolved deterministically, and approved exactly like
a reply: the model writes the words, the runtime decides who may be written to, and only an
explicit human phrase sends anything.

## Status

Accepted

## Context

Phase 10B made a conversation able to *reply*: it drafts, prepares an immutable `mail.send` action,
shows the exact payload, and sends only after the user's own explicit phrase. The first thing a
person then asks for is the other half — "发个打招呼的邮件给我自己", "给张老师发邮件" — and the
second thing they ask for is somewhere to keep a name and an address: "张老师邮箱是
zhang@example.edu，记成联系人".

Both are dangerous in a specific way that has nothing to do with SMTP. A model that can *name a
recipient* can invent one. A model that can *write to anyone* can be talked into writing to anyone
by anything it reads — an incoming message, a contact's display name, a page it was shown. And a
"contact" that is really a personal fact, or a memory the assistant decides to keep by itself,
silently accumulates authority nobody granted.

Five shortcuts were rejected before this phase started:

* **letting the model supply an address.** A hallucinated address is not a formatting problem; it
  is a letter to the wrong person, and it would be sent by the same machinery that sends correct
  ones;
* **a first-turn send.** "发个问候邮件给张老师" would become an external action with no human
  reading of the exact bytes — the property Phase 10B exists to protect;
* **a generic confirmation.** If `可以` could send, the difference between "apply the plan" and
  "send this letter" would be a shrug;
* **contacts as facts.** A `ConfirmedFact` is personal memory with a confirmation flow of its own.
  Contacts are structured identity used to resolve a name; mixing them would give an address book
  the semantics of remembered personal truth;
* **a second SMTP path.** New mail and replies differ in where the draft came from, not in what
  leaves the machine. Two executors, two link tables or two approval paths would mean two places
  where "what was approved is what is sent" could be false.

## Decision

1. New outbound mail uses the existing `mail.send` external-action boundary. There is no
   `mail.compose_send`, no `smtp.send` and no second executor.
2. The model may draft the subject and the body. It may not authorize sending.
3. The model may never call SMTP, open a connection or reach an executor.
4. Every new-mail send requires an immutable `ActionRequest`.
5. The exact `ActionRequest` payload is rendered before sending. The preview is re-rendered from
   the stored action, never from the draft, the model's summary or the conversation.
6. The first user request can prepare the message but can never send it.
7. A second explicit **human** message is mandatory before anything leaves the machine.
8. The action-specific send vocabulary of ADR-0034 remains authoritative. Generic `可以`, `好`,
   `嗯`, `继续` and `ok` do not send mail; only `确认发送`, `发送`, `发吧`, `确认发出`, `send` and
   `confirm send` may settle a pending external mail review.
9. The approval challenge plaintext never reaches `ModelPort`, the conversation history or any
   durable row.
10. Contacts are local structured identity records: a name, an address and a lifecycle.
11. Contacts are **not** `ConfirmedFact`s: creating one writes a `Contact` and nothing else.
12. Contacts grant no authority: a contact cannot approve, send, execute or schedule anything.
13. Recipient resolution is deterministic, and its allowed sources are exactly:
    1. an email address explicitly present in the current human message;
    2. one unambiguous ACTIVE `Contact`;
    3. one unambiguous configured own mail account, for "我自己".
14. The model may not invent email addresses. An explicit address a model proposes is accepted
    only when the same normalized address occurs in the raw human user message.
15. Unknown named recipient → clarification. Multiple matching contacts → clarification. Multiple
    possible own accounts → clarification. Each with zero draft, zero action, zero review.
16. No scraping arbitrary incoming mail bodies to create contacts, and no automatic address-book
    import.
17. New outbound compose supports one primary recipient in this phase. No `Cc`, no `Bcc`.
18. No attachments. No scheduled mail. No contact sync. No generic external-action model
    operation.
19. Existing reply-mail behavior remains compatible: reply drafts keep their source-message
    invariant and their own table, and historical `mail.send` payloads keep their meaning.
20. Existing SMTP `UNKNOWN` semantics remain unchanged, and `UNKNOWN` is never blindly retried.
21. Editing a prepared letter invalidates the review that showed the old text: the draft advances
    a version, a new immutable action is prepared, its fingerprint differs, and a new explicit
    confirmation is required.
22. Changing a contact after a letter was prepared does not retarget that letter. The action stays
    bound to the address it snapshotted; using the new address means preparing a new letter.
23. Cancelling a review cancels it: no approval, no execution, no SMTP, and a later `确认发送`
    cannot revive it.

### Draft strategy

`MailDraft` requires a source `MailMessage` in a way that cannot safely become nullable: the column
is `NOT NULL` with a foreign key, the domain requires `reply_to_message_id`, and reply semantics
(thread membership, `Re:` subject derivation, reply-header derivation, source-message knowledge)
hang off it. Relaxing that would have rewritten the meaning of every historical reply row, and
SQLite cannot drop a `NOT NULL` constraint without rebuilding a table that other tables reference.

This phase therefore adds a **narrow new-mail draft** (`new_mail_drafts`, `NewMailDraft`,
`NewMailDraftService`) with one recipient, a subject, a body, an `origin` and a monotone `version`.
Both draft kinds converge before external execution: they produce the same
`ActionRequest("mail.send")`, through the same preparation service, into the same link table, and
the same review, approval, execution and reconciliation path.

### Payload strategy

`MailSendPayload` gains one closed discriminator, `kind` (`reply` | `new`), and one invariant: a
`new` payload never carries `In-Reply-To` or `References`. Nothing else changes: every field SMTP
reads is the same, reply headers were already optional, and the fingerprint rules are untouched.
Historical payloads — written before the discriminator existed — remain valid and parse as
`reply`. The schema version is therefore *not* bumped: an executor has nothing new to notice.

`MailSendLink` becomes a closed two-target variant (`draft_id` **or** `new_draft_id`, exactly one),
which keeps the two database invariants that make the boundary real — one action per draft version,
and one Message-ID per action — for both kinds in one table.

## Alternatives considered

* **The model invents recipient addresses.** Rejected: a hallucinated address is a letter to the
  wrong person.
* **Contacts as facts.** Rejected: identity resolution is not personal memory, and facts carry a
  confirmation flow that an address book must not inherit.
* **Automatic contact extraction from mail.** Rejected: a stranger's message would decide who the
  user can write to.
* **First-turn send.** Rejected: the exact bytes would never be read by a human.
* **A generic "yes" sends mail.** Rejected: external effects need their own explicit vocabulary
  (ADR-0034 §12).
* **A separate SMTP executor for new mail.** Rejected: one capability, one pipeline.
* **A generic mail tool or address-book/network lookup.** Rejected: the vocabulary stays closed and
  local.
* **Attachments in this phase.** Rejected: new content types, new size limits and new scanning
  questions belong to their own phase.
* **Scheduled send.** Rejected: a delayed external effect with no human present is a different
  approval problem.
* **Making `MailDraft.reply_to_message_id` nullable.** Rejected for the reasons above.

## Consequences

* A person can say "发个打招呼的邮件给我自己" or "给张老师发邮件，说我周五之前交报告" and get the
  exact preview of a real letter — with the same review, approval and execution boundary a reply
  already had.
* A hallucinated address cannot produce a draft, an action or a review, and the refusal says so in
  the user's own terms.
* A name resolves to an address only through a stored contact or the user's own words, so "who can
  this assistant write to?" is answerable from local records.
* `pw integrity check` re-derives every contact fingerprint and every new-mail link/payload
  relationship read-only.

## Implementation notes

* Migration: `migrations/0019_contacts_and_outbound_mail.sql`.
* Domain: `domain/contact.py`, `domain/new_mail_draft.py`, `domain/mail_send.py` (`kind`,
  `MailSendLink` variants).
* Application: `application/contacts.py`, `application/new_mail_drafts.py`,
  `application/recipient_resolution.py`, `application/mail_send_actions.prepare_new_send`.
* Conversation: ADR-0033 (runtime), ADR-0034 (exact review), ADR-0035 (preflight, capability
  metadata). Operations: `contact.list|create|edit|retire`, `mail.compose_new`,
  `mail.prepare_new_send`.
* Integrity and backup: ADR-0031.
