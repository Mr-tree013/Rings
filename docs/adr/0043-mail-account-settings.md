# ADR-0043 — Mail Account Settings Without A Secret Store

## Title

Mail account metadata becomes editable from the product: a typed application service over a
managed, atomically written overlay file, exposed through authenticated and CSRF-protected HTTP
routes and a small settings page. Credentials stay exactly where they already were — the process
environment — so the settings surface stores a *reference*, never a secret, and says so.

## Status

Accepted

## Context

By v1.2 the mail pipeline is real: IMAP ingestion with UIDVALIDITY-correct identity, threading,
analysis, reply drafts, contacts, exact-reviewed SMTP delivery and Sent-folder reconciliation. None
of it is reachable by a new user, because every account lives in `[[mail.accounts]]` inside
`config.toml` — a file the product only reads, validates strictly, and rejects credentials in.

```text
features exist ──► no way to add an account ──► the whole area stays invisible
```

The obvious fix is a form that writes a password into a table. That fix is wrong in a way this
project has been refusing since Phase 6A: it would create a plaintext credential store, it would
put a secret on the wire back to a browser, and it would make "who can read my mail password" a
question about a database file that gets copied into backups. The existing design — the variable
name is *derived* from the account id, so a user cannot point the host at an arbitrary variable —
is deliberate and is worth keeping.

There is a second, quieter problem: `config.toml` is the user's file. It has comments, an order and
keys this build does not know. A web form that round-trips it will eventually delete something.

## Decision

1. **The mail settings UI manages typed account configuration.** Fields are typed values, not TOML
   text; the browser never composes configuration syntax.
2. **Browser code never reads config files directly.** Every read goes through the application
   service.
3. **The browser never receives stored secret values back.** There is no route that returns one.
4. **Mail credentials never enter conversation history.**
5. **Mail credentials never enter model context.** The interpreter's bounded context does not
   mention accounts at all.
6. **Mail credentials never enter logs.** No test diagnostic, no error string and no request log
   contains one.
7. **Mail account configuration and Contact data are distinct.** An account is a mailbox this host
   speaks to; a contact is an address the user may write to. Neither implies the other.
8. **Account configuration and mailbox messages are distinct.** Editing an account never touches
   stored messages, threads, analyses or drafts.
9. **The IMAP test is read-only.** Connect, TLS, authenticate, minimally list, close. No mutation,
   no delete, no mark-read.
10. **The SMTP test performs connect and authentication only and never sends DATA.** No MAIL FROM,
    no RCPT TO, no DATA.
11. **A connection test does not create an ActionRequest.** It is a diagnostic, not an effect.
12. **Real mail sending still requires the existing conversation ActionRequest / Approval /
    Execution path.** Nothing here shortens it.
13. **Secret persistence reuses an existing safe abstraction if one exists.**
14. **No unsafe plaintext credential database is introduced.**
15. **If there is no approved writable secret store, v1.3 stores a secret reference only and reports
    credentials as NOT CONFIGURED** until the value is provided through the existing supported path
    — the environment.
16. **Unknown provider settings remain blank.** No guessed host, port or security mode.
17. **Config writes are atomic.** Temp file in the same directory, then `os.replace`.
18. **Unrelated configuration is not destroyed.** The overlay is a separate managed file; the
    user's `config.toml` is never rewritten.
19. **If safe runtime hot-reload is narrow and feasible, implement it.**
20. **Otherwise return `restart_required = true` truthfully.**

### Rejected alternatives

- **Storing SMTP/IMAP passwords in ordinary SQLite business tables.** A plaintext credential store
  in the file that is copied into every backup, for no benefit over the environment.
- **Returning passwords through settings GET.** A secret that crosses the wire is a secret that
  ends up in a browser cache, a proxy log or a screenshot.
- **Logging test credentials.** The diagnostics would become the leak.
- **Sending a real test email.** "Test send" would be an external effect with no approval, which is
  exactly the boundary this project does not cross.
- **Guessing provider hosts.** A wrong host produces a credential failure the user cannot diagnose,
  and a right-looking guess is worse than a blank field.
- **Rewriting unrelated configuration destructively.** The user's file is not ours to reformat.
- **Making the model configure credentials.** A model that can write credentials is a model that can
  leak them.

## Consequences

### Where the settings live

Account metadata is written to a **managed overlay**, `mail-accounts.toml`, in the same XDG config
directory as `config.toml`. The precedence is deterministic and documented:

```text
effective accounts = (accounts in config.toml)  then  (accounts in mail-accounts.toml, by id)
```

The overlay entry wins for a given id; an id that exists only in `config.toml` stays. The settings
service always writes the **complete effective list** into the overlay, so the first edit through
the UI imports the legacy accounts instead of silently dropping them, and a host that never uses the
UI is completely unaffected — a missing overlay is not an error and changes nothing.

The file is created `0600` inside a directory created `0700`, both through the existing
`adapters/runtime/permissions.py` helpers, and written atomically. Its contents are account
metadata only: an id, a label, host, port, username, mailbox, an optional outbound block, and a
secret *reference name*. There is no password field, and the strict parser keeps refusing one.

### What the browser may see

The safe DTO carries the account id, a display label, the username, the from address, the enabled
flag, the IMAP and SMTP `host`/`port`/TLS mode, a `configured` boolean for each side, and a
credential block of `{configured, source_kind, reference}`. It never carries a value: `configured`
is derived from whether the environment currently holds the derived variable, so the answer is
truthful without ever reading the secret into a response body.

### Runtime reload

Reloading accounts would mean rebuilding `MailSyncService`, the recipient resolver's account list
and the registered executors, all of which the daemon composes once at startup and holds for the
process lifetime. v1.3 does not build a dynamic reconfiguration system for that: the settings
surface returns `restart_required = true` and says "配置已保存。重启 assistantd 后生效。" That is
honest, and it is what item 20 of the phase brief allows.

### Capability self-knowledge

`system.capabilities` reports receive/send/credential/enabled state from **configuration**, never
from how many messages happen to be stored. An account with no credential is `NOT_CONFIGURED` for
the operations that need one, even if a thousand messages were received before the password was
rotated away.

## Implementation notes

- Configuration reading stays in `adapters/config/toml_config.py`; the overlay reader and writer are
  a separate module so the strict primary parser keeps its current meaning.
- The secret reference and the `configured` flag come from `adapters/mail/credentials.py`, which
  already derives `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD` and `..._SMTP_PASSWORD`.
- Connection tests are diagnostics on the existing mail adapters. They reuse the same TLS policy
  (`starttls` or `ssl`, never plaintext, never "skip verification") and the same environment
  credential lookup as delivery, and they are pinned by an architecture test that refuses any
  `DATA`, `sendmail` or executor reference in their bodies.
- Routes live beside the existing chat and control-plane routes, behind the existing session plus
  CSRF pair. There is no route that returns a secret and no route named `send-test-message`.
- `pw integrity check` and the backup manifest already exclude credentials; account metadata follows
  the existing configuration backup policy, because it is configuration.
