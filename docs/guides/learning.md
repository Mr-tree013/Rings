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

## Implementation notes

- Human-confirmed personal facts: [ADR-0027](../adr/0027-human-confirmed-personal-facts.md)
- Reviewed non-executing playbooks: [ADR-0028](../adr/0028-reviewed-playbooks.md)
