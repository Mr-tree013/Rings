# Learning: personal facts and reviewed playbooks

## What it does

Rings has two learning paths, and both of them end with a human decision rather than with a model
deciding what is true.

```text
Correction → FactCandidate → explicit human confirmation → ConfirmedFact

successful Action → explicit PlaybookCandidate → side-effect-free dry-run → explicit promotion → Playbook
```

Neither path changes any capability: facts do not fill forms in v1, and a playbook is a reference
blueprint that never executes.

## Personal facts

### From a conversation

The same lifecycle is reachable by talking, with one extra rule: the conversation may only
*propose*, and the confirmation is your own explicit phrase, parsed by code (ADR-0038).

```text
You > 记住我的办公室在仙林
Tree > 我准备记录这条长期信息：
       - profile.office：仙林
       （来自你的话：「记住我的办公室在仙林」）
       如果要长期保存，请明确说「确认记住」；说「不要记」我就不保存。
You > 确认记住
Tree > 已记住：
       - profile.office：仙林
```

* a plain statement (`我的办公室在仙林`) creates no candidate at all;
* `可以` / `好` / `ok` never confirm a long-term fact, and neither does a model: there is no
  `fact.confirm` operation in the vocabulary, and the confirmation path holds no provider;
* `不要记` / `别记` / `取消` resolves the pending candidate as rejected, keeping it as history;
* a pending proposal survives a conversation restart, and the session shows it again without ever
  confirming it;
* `你记得我的办公室在哪里吗？` and `我有哪些长期信息？` read *confirmed* rows only; a proposal is not
  knowledge, and a contact is not a fact.

### Common workflow

```bash
uv run pw correction add "My office moved to Room 302"
uv run pw corrections
uv run pw correction show <CORRECTION>

uv run pw fact candidate add profile.office "Room 302" \
  --note "My office is Room 302 in the CS building"
uv run pw fact candidates
uv run pw fact candidate show <CANDIDATE>
uv run pw fact candidate confirm <CANDIDATE>
uv run pw fact candidate reject <CANDIDATE>

uv run pw facts
uv run pw facts --all
uv run pw fact show <FACT>
```

### Safety behavior

- A candidate is not a fact. It cannot be used to fill anything in, it is not part of any model
  context, and no consumer reads it. Only a `ConfirmedFact` can be considered by future logic, and
  only while it is active and unexpired.
- Only you can confirm. The single entry point is `pw fact candidate confirm`, which the CLI reaches
  through the learning service. No model and no background worker has a path to it, and there is no
  `--force` or `--confirm-all`.
- Provenance is durable: every candidate cites the correction that produced it, stored in your own
  words, and history is append-only. Confirming a new value for the same key supersedes the old row
  in the same transaction rather than deleting it.
- Expiry is derived at read time. `--valid-until` takes an aware ISO 8601 timestamp; nothing is
  scheduled and no background job mutates the fact store.
- The fact store is not a credential store. Keys containing credential-like segments (`password`,
  `secret`, `token`, `credential`, `api_key`, `private_key`) are rejected, and values are never
  scanned in the hope of spotting something that looks like a password.
- **ConfirmedFacts are NOT auto-filled in v1.** They are stored, reviewed and queried; they are not
  injected into eHall forms, mail drafts, the interpreter or grounded answers.

## Playbooks

### Common workflow

```bash
uv run pw playbook candidate add <ACTION> \
  --name "Approved certificate workflow" \
  --note "Reviewed successful run"
uv run pw playbook candidates
uv run pw playbook candidate show <CANDIDATE>
uv run pw playbook candidate test <CANDIDATE>
uv run pw playbook candidate promote <CANDIDATE>
uv run pw playbook candidate reject <CANDIDATE>

uv run pw playbooks
uv run pw playbooks --all
uv run pw playbook show <PLAYBOOK>
uv run pw playbook retire <PLAYBOOK>
```

### Safety behavior

- A successful execution never becomes a playbook by itself. The candidate table only grows when you
  run `pw playbook candidate add`, at most once per source action.
- The source must be an unambiguous success: the action is `EXECUTED`, the run is `SUCCEEDED` and
  finished, and the payload fingerprint is re-verified. `FAILED`, `UNKNOWN`, `RUNNING` and
  never-executed actions are refused.
- The dry run is local parsing only. It re-reads the historical payload with the current strict
  parser and does not read credentials, open a socket, open a browser, check Sent, generate a new
  Message-ID or call an executor. It passes even with no password, no browser and eHall disabled.
- `PASS` means the current code still understands that payload. It does not mean the credential is
  still valid, the remote page is unchanged, or the action is worth doing again.
- **Playbooks DO NOT execute.** Promotion requires a passing dry run under the current replay
  contract version and an explicit human command; nothing may be parameterised, instantiated or
  replayed into a new action. A playbook contains no approval and does not bypass one.
- Retiring a playbook keeps it visible in history. Rejected candidates are kept as audit records.

## Commands

```text
pw correction add "My office moved to Room 302"
pw correction show <CORRECTION>
pw corrections
pw fact candidate add profile.office "Room 302" --note "Why this is correct"
pw fact candidate show <CANDIDATE>
pw fact candidate confirm <CANDIDATE>
pw fact candidate reject <CANDIDATE>
pw fact candidates
pw fact show <FACT>
pw facts
pw facts --all
pw playbook candidate add <ACTION> --name "Reviewed workflow" --note "Why this run matters"
pw playbook candidate show <CANDIDATE>
pw playbook candidate test <CANDIDATE>
pw playbook candidate promote <CANDIDATE>
pw playbook candidate reject <CANDIDATE>
pw playbook candidates
pw playbook show <PLAYBOOK>
pw playbook retire <PLAYBOOK>
pw playbooks
pw playbooks --all
```

## Troubleshooting / limitations

- `ForbiddenFactKey` means the key looks like a credential (`mail.smtp_token` is rejected by design).
- `PlaybookSourceNotEligible` means the referenced action is not a finished success.
- `PlaybookCandidateNotTested` means there is no passing dry run under the current contract version.
- Promotion never takes `--force` or `--skip-test`, and no automatic promotion exists.
- There is no fact editing in place: a new value is a new candidate that supersedes the old fact.
- A conversation can only propose a fact; saving one always needs your own `确认记住` (or the
  equivalent `pw fact candidate confirm`).
- Confirmed facts fill no form, no mail recipient and no mail body in this version: nothing reads a
  fact into an outbound surface.
- Only fact keys travel in a conversation's context, never values; the value is rendered from the
  confirmed row when you ask about it.

## Implementation notes

- Human-confirmed personal facts: [ADR-0027](../adr/0027-human-confirmed-personal-facts.md)
- Conversational fact confirmation: [ADR-0038](../adr/0038-conversational-fact-confirmation.md)
- Reviewed non-executing playbooks: [ADR-0028](../adr/0028-reviewed-playbooks.md)
