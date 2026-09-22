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

## From the conversation

The same pipeline is reachable from Tree with two operations, and no way to submit:

```text
You > eHall 现在能用吗？
Tree > 现在可以准备一份证明申请（服务：nju-ehall/证明书申请）。页面上的字段：
       - 申请人姓名（applicant-name）：必填
       - 证明书类型（certificate-type）：必填，可选：在读证明、成绩证明
       需要准备的材料：身份证件、学号
       …在你亲口说「确认提交」之前，什么都不会提交。

You > 帮我申请在读证明，申请人姓名是张三，证明书类型是在读证明。
Tree > 将要提交的证明申请（以下内容就是实际提交的内容）：
       · 服务：nju-ehall/证明书申请
       · 申请人姓名：张三
       · 证明书类型：在读证明
       · 需要准备的材料：身份证件、学号
       · 页面契约指纹：<64 位十六进制>
       …确认提交吗？回复「确认提交」我就提交，或回复「取消」。
       回复「可以」不会提交。
```

What the conversation will and will not do:

- `ehall.status` reads the live form (`not_configured`, `auth_required` and "the page is not the one
  I know" are told apart, and it never asks for a password);
- `ehall.certificate.prepare` prepares one exact `ActionRequest` for an OPEN case and shows the
  preview **rendered from that payload** — there is no approval, no execution run and no keystroke
  at this point;
- every value must appear in your own message: a name, a student number or any other detail the
  model produced is refused before anything is created, and there is no autofill from facts,
  knowledge, mail or memory;
- a missing required parameter is a question ("还缺少必要的信息"), never a default;
- only your own `确认提交` (or the card's 确认提交 button) settles it — through the same
  `ApprovalService` and `ActionExecutionService` the CLI uses — and `可以` submits nothing;
- the browser card is settled by durable identity with no model in the path, and an ambiguous
  result stays `UNKNOWN` and is never retried automatically.

There is no `ehall.submit`, no `approval.create`, no `action.execute` and no `browser.*` operation.

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
- Conversational certificate review: [ADR-0045](../adr/0045-conversational-ehall-certificate-review.md)
- Approval and execution boundary: [ADR-0023](../adr/0023-action-approval-execution-boundary.md)
