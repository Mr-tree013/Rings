# eHall: approved certificate application

## What it does

Rings can complete **one** NJU eHall errand: the certificate application
(`ehall.submit-certificate`). It is a typed pipeline with a narrow scope, not browser automation:

- there is no generic browser tool, no `click`, `fill`, `goto` or URL parameter anywhere;
- course drop, withdrawal, application cancellation, deletion and dorm checkout **do not exist** in
  the capability set — they are absent from the code rather than forbidden by a prompt;
- the submission cannot happen without one exact human approval.

## Enable / configure

```toml
[ehall]
enabled = false
```

Turning it on enables the whitelisted certificate pipeline only. There is no URL, selector or
credential to configure, and no model or personal-fact autofill participates in filling the form.

Install the browser runtime once. This downloads Chromium; it does not download a model:

```bash
uv run playwright install chromium
```

Chromium is not required for any other part of Rings, and `pw doctor` reports whether it is ready.

## Common workflow

Log in by hand. The project never asks for, types or stores a university password:

```bash
uv run pw ehall status
uv run pw ehall login
```

Complete SSO and MFA in the window that opens. The session is kept in a private profile under the
runtime data directory (`ehall/nju-profile/`, owner-only, never in the repository or the cache).

Inspect, prepare, then use the same approval chain as everything else:

```bash
uv run pw ehall certificate inspect

uv run pw case add "Apply for enrollment certificate"
uv run pw ehall certificate prepare --case <CASE> \
  --field applicant-name=张三 \
  --field certificate-type=在读证明

uv run pw ehall certificate show <ACTION>
uv run pw action show <ACTION>
uv run pw action challenge <ACTION>
uv run pw action approve <ACTION> <TOKEN>
uv run pw action execute <ACTION>
```

## Commands

```text
pw ehall status
pw ehall login
pw ehall certificate inspect
pw ehall certificate prepare --case <CASE> --field KEY=VALUE
pw ehall certificate show <ACTION>
pw action challenge <ACTION>
pw action approve <ACTION> <TOKEN>
pw action execute <ACTION>
```

## Safety behavior

- `inspect` reads the live form and submits nothing. `prepare` validates locally and freezes the
  service identity, the ordered field definitions and their options, the required materials, the
  submit control and the values you supplied.
- **Every writable value comes from `--field KEY=VALUE`.** No knowledge base, mail analysis, model or
  confirmed fact fills a field in.
- **A page-contract change invalidates the prepared action.** Execution re-inspects first and
  requires an identical fingerprint; a mismatch raises `EHallPageChanged` and fails closed, typing
  nothing and submitting nothing.
- The submit click happens once. A failure before it is a clear `FAILED`; anything unclear after it
  is `UNKNOWN`, which blocks retries of that action and asks you to check eHall by hand.
- The navigation whitelist is limited to the configured eHall origins; an unexpected top-level
  origin fails closed.
- The browser only runs when you run a CLI command. No daemon service can open one, and no
  background worker has a browser capability.

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| `pw ehall login` cannot start a browser | Chromium is not installed: run `uv run playwright install chromium`. |
| Prepare fails with a page-contract error | The portal changed. Rings fails closed instead of adapting; `inspect` again and re-prepare. |
| Execution reports `UNKNOWN` | The submit click happened but the outcome could not be determined. Check eHall manually; do not retry. |
| Execution reports an expired session | Log in again with `pw ehall login`, then prepare a new action. |

- NJU may change the portal at any time. The selectors are not a promise of stability; the design
  chooses to stop before typing rather than to guess.
- Only one eHall workflow exists in v1, and other eHall errands are not planned.

## Implementation notes

- Approved eHall certificate pipeline: [ADR-0025](../adr/0025-approved-ehall-certificate-pipeline.md)
- Approval and execution boundary: [ADR-0023](../adr/0023-action-approval-execution-boundary.md)
