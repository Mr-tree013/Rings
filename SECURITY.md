# Security Policy

This project runs on your machine and holds your mail, your documents and your approval history. It
is designed so that a mistake in one part cannot silently reach the outside world, and reports about
the boundaries below are genuinely useful.

## How to report a vulnerability

**Do not report secrets or personal data publicly.**

- Use GitHub's private vulnerability reporting on this repository if it is enabled.
- Otherwise, contact the repository owner privately rather than opening a public issue containing
  sensitive data.

Please do not include any of the following in an issue, a pull request, a screenshot or a log paste:

- credentials of any kind (model API keys, IMAP/SMTP passwords, session or approval tokens)
- personal mail content, document content or file paths that identify you
- backup archives, runtime databases or exported runtime state

If a report needs that material to be reproducible, describe the shape of the data instead, and use
obviously fictional values.

## What is in scope

Security-sensitive areas, in the order they matter:

1. **Action / Approval boundary** — `ActionRequest` immutability, fingerprint computation and
   re-validation, single-use challenge tokens, consumption before execution, and the rule that no
   model or background worker can create an approval.
2. **SMTP ambiguity** — classification of definite failure versus `UNKNOWN`, and the rule that an
   ambiguous result is never retried automatically.
3. **eHall** — page-contract validation, the single-submit rule, the failure handling after a submit
   click, and the absence of any high-risk errand capability.
4. **Mobile LAN authentication** — session and CSRF handling, pairing-token lifetime and storage,
   the socket-peer trust decision, and the absence of any execution route.
5. **MCP capability boundary** — the exposed surface, the disabled-by-default settings, and the
   absence of approval, execution, filesystem and shell abilities.
6. **Watcher SSRF surface** — URL validation, host resolution checks, redirect handling and response
   size limits.
7. **Backup / restore** — archive member validation, hash verification, staging-only restore, and the
   invalidation of outstanding authorization after a restore.

## What is probably not a vulnerability

- A capability that does not exist (course drop, withdrawal, dorm checkout, generic shell, generic
  browser or generic HTTP) — its absence is deliberate.
- A model producing a plausible but wrong answer. Model output is untrusted input and is validated
  locally; grounding quality is not a security boundary.
- A backup archive failing to include credentials, the eHall authenticated profile or derived
  indexes — excluding them is the intended design.
- Anything that requires an attacker to already have local access to the runtime data directory,
  which is owner-only (`0700`/`0600`) and holds the approved history.

## Supported versions

This project publishes versioned releases; the current release line is 1.1.0. A fix is issued on the
current line, and older tags stay as they were.
