# Mail: receive, understand, draft, approve, send

## The normal path is a conversation

Everything below this section is the mechanism. The way to use it day to day is
[Tree Conversation](conversation.md):

```text
You > 最近有什么需要处理的邮件？
Tree > …
You > 回复张老师，说我周五之前交
Tree > [the exact bytes that would be sent]
       确认发送吗？
You > 确认发送
Tree > 已发送。
```

The conversation prepares the same `ActionRequest` this guide documents, shows you its exact
payload, and settles it with one explicit phrase — "确认发送", not "可以". The `pw`
commands below remain the advanced surface: they are how you debug, inspect, reconcile and operate
the same durable state, and they are what the conversation calls underneath.

## What it does

Rings receives mail over IMAP, stores it durably, groups it into deterministic threads, asks the
model for bounded analysis, drafts replies locally, and — only after one exact human approval —
sends through SMTP.

```text
mail sync
  ↓
thread / analysis
  ↓
draft
  ↓
Case
  ↓
mail.send Action
  ↓
challenge
  ↓
Approval
  ↓
execute
```

Nothing in the background sends mail: `mail-sync`, the event worker, the scheduler and the daemon
startup can receive, analyze and notify, but the only send path is an approved action executed by
`pw action execute`.

## Enable / configure

```toml
[mail]
poll_interval_seconds = 60
max_messages_per_poll = 100
initial_fetch_limit = 500
reconciliation_window = 500
max_message_bytes = 26214400
timeout_seconds = 30

[[mail.accounts]]
id = "smail"
host = "imap.example.edu"
port = 993
username = "student@example.edu"
mailbox = "INBOX"
enabled = true

# sending needs the SMTP half of the same account
smtp_host = "smtp.example.edu"
smtp_port = 465
smtp_security = "ssl"          # starttls | ssl
smtp_from = "student@example.edu"
```

Credentials are environment-only:

```bash
export GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD='...'
export GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD='...'
```

`starttls` and `ssl` are the only transport options. There is no plaintext SMTP and no way to skip
certificate verification.

## Sync and read

```bash
uv run pw mail accounts
uv run pw mail sync
uv run pw mail sync --account smail
uv run pw mail status
uv run pw mail messages
uv run pw mail show <MESSAGE>
```

Each message then has a deterministic place in a thread and at most one stored analysis:

```bash
uv run pw mail threads
uv run pw mail thread show <THREAD>
uv run pw mail analysis <MESSAGE>
```

- A message's identity is `(account, mailbox, UIDVALIDITY, UID)`; a UID alone is never a durable
  identity. A cursor only advances to UIDs that are already durably stored, so a crash replays rather
  than skips.
- Threading is code, not a model: the parent is resolved from `In-Reply-To` and then `References`,
  only within the same account, and only when it matches exactly one stored message. A duplicate
  `Message-ID` is `AMBIGUOUS` and is never guessed.
- Sync uses a read-only mailbox select and `BODY.PEEK`, so your mail is never marked as read.
- Classification produces candidates only. `DEADLINE` (by when) and `EVENT_START` (when it happens)
  are different values and are never collapsed into one date field.
- A stored analysis is reused across worker retries when its input fingerprint is unchanged, so a
  retry does not pay for the same answer twice.

## Draft a reply

Drafting is explicit and local. Nothing in the background ever creates a draft:

```bash
uv run pw mail draft create <MESSAGE>
uv run pw mail draft create <MESSAGE> --context-query "my office hours" --root university
uv run pw mail drafts
uv run pw mail draft show <DRAFT>
uv run pw mail draft edit <DRAFT> --subject "Re: lab slot" --body "..."
uv run pw mail draft acknowledge <DRAFT>
```

- The recipient comes from `Reply-To` (falling back to `From`) and the subject is derived by a pure
  local function. The draft schema has no `to`, `cc`, `bcc` or `subject` field, so the model can only
  write the body. V1 drafts are replies, not reply-all.
- **Personal knowledge is read only when you pass `--context-query`.** Without it the knowledge
  search is never called, and an instruction inside a mail body cannot make it start.
- Personal facts the sources do not support must be listed as `needs_user_input` instead of being
  invented.
- Editing is optimistically concurrent (`UPDATE … WHERE id = ? AND version = ?`); a stale edit fails
  instead of overwriting, and editing never changes the recipient.
- `--context-query` is the only way mail content can reach your knowledge index scope, and it is
  your query, not the message, that is searched.

## Prepare, approve, execute

```bash
uv run pw case add "Reply to supervisor"
uv run pw cases
uv run pw case show <CASE>

uv run pw mail send prepare <DRAFT> --case <CASE>
uv run pw mail sends
uv run pw mail send show <ACTION>

uv run pw actions
uv run pw action show <ACTION>
uv run pw action challenge <ACTION>
uv run pw action approve <ACTION> <TOKEN>
uv run pw action execute <ACTION>
uv run pw action cancel <ACTION>

uv run pw case done <CASE>
uv run pw case cancel <CASE>
```

- `prepare` freezes that draft version — sender, recipient, subject, body, date, reply headers and
  Message-ID — into an immutable payload. The bytes sent come only from that payload; the draft is
  never re-read at execution time.
- One draft version produces at most one send action; to send again you advance the draft first.
- The Message-ID is created before approval and becomes part of the approved payload, so retries and
  reconciliation use the same value.
- Editing the draft afterwards affects only a future action, and `pw mail send show` warns you when
  the draft has moved on.
- The capability check happens **before** the approval is consumed: if SMTP is not configured or the
  credential is missing, `pw action execute` reports `CapabilityUnavailable` and the approval stays
  unused.

## `UNKNOWN` is not a retry

**SMTP cannot guarantee exactly-once delivery.** A connection that drops after the message data was
accepted leaves both sides unsure whether the message was delivered, and no local code can settle
that by trying again.

```text
ExecutionRun UNKNOWN  →  DO NOT blindly resend
```

```bash
uv run pw mail send show <ACTION>
uv run pw mail send reconcile <ACTION>
```

`reconcile` searches the Sent mailbox for the one exact Message-ID that was fixed before approval,
and reports one of:

| Result | Meaning |
| --- | --- |
| `FOUND` | Exactly one header-identical match; the run is promoted to `SUCCEEDED` in one transaction. |
| `NOT_FOUND` | **This does not prove the message was not sent.** Only an audit row is recorded; the execution status is unchanged. |
| `AMBIGUOUS` | Two or more exact matches. No UID is chosen. |
| `UNAVAILABLE` | The Sent mailbox could not be read. The status is unchanged. |

There is no resend command. The only way to send again is to cancel the action, move the draft
forward, prepare a new action, and approve it as a human.

Failures are classified by where they happened: authentication, sender rejection or recipient
rejection before the message data is accepted is a definite `FAILED` (and the approval is already
spent), while anything unclear afterwards is `UNKNOWN`.

## Commands

```text
pw mail accounts
pw mail sync
pw mail sync --account smail
pw mail status
pw mail messages
pw mail show <MESSAGE>
pw mail threads
pw mail thread show <THREAD>
pw mail analysis <MESSAGE>
pw mail drafts
pw mail draft create <MESSAGE>
pw mail draft create <MESSAGE> --context-query "my office hours" --root university
pw mail draft show <DRAFT>
pw mail draft edit <DRAFT> --body "..."
pw mail draft acknowledge <DRAFT>
pw mail sends
pw mail send prepare <DRAFT> --case <CASE>
pw mail send show <ACTION>
pw mail send reconcile <ACTION>
pw cases
pw case add "Reply to supervisor"
pw case show <CASE>
pw case done <CASE>
pw case cancel <CASE>
pw actions
pw action show <ACTION>
pw action challenge <ACTION>
pw action approve <ACTION> <TOKEN>
pw action execute <ACTION>
pw action cancel <ACTION>
```

## Safety behavior

- Raw received mail is content-addressed under the runtime data directory; attachments are stored as
  metadata only and never executed.
- Mail content, headers and attachment names are untrusted data. They are stored and quoted, never
  rendered or executed.
- Threading, recipients and subjects are decided by local code, never by the model.
- Analysis and drafting cannot create a `Task`, a `Case`, an `ActionRequest` or an `Approval`.
- Sending requires an exact `ActionRequest`, one human `Approval` bound to its fingerprint, and an
  `ExecutionRun`.

## Troubleshooting / limitations

- `pw mail accounts` says a credential is missing: the environment variable name is derived from the
  account id, so check the spelling (`GROWING_ASSISTANT_MAIL_<ID>_PASSWORD`).
- `pw mail draft create` refuses an oversize message: a message whose body is unavailable is never
  sent to a model; only an `unknown` analysis is recorded.
- Mail is not indexed into the knowledge base in v1, and mail-driven automatic task creation does
  not exist.
- There is no SMTP retry, no resend, and no server-side deletion synchronisation.

## Implementation notes

- Ingestion: [ADR-0020](../adr/0020-durable-imap-ingestion.md)
- Threading and analysis: [ADR-0021](../adr/0021-mail-threading-and-analysis.md)
- Reply drafts: [ADR-0022](../adr/0022-durable-mail-reply-drafts.md)
- Approval and execution boundary: [ADR-0023](../adr/0023-action-approval-execution-boundary.md)
- Approved SMTP delivery: [ADR-0024](../adr/0024-approved-smtp-delivery.md)
