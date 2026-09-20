# ADR-0027 — Human-Confirmed Personal Facts

## Title

Learning starts as a durable candidate a person proposed, and becomes a personal fact only when a
person promotes it by hand — with the correction it came from kept as its provenance forever.

## Status

Accepted

## Context

Six phases built a system that can receive mail, understand it, prepare an exact action, get it
approved and carry it out. The next thing the project wants is the ability to *remember*: the
user's student id, the office they sit in, the way they sign a reply. Today each of those facts is
typed again every time, and a personal assistant that cannot learn one of them is not much of an
assistant.

Memory is also the most dangerous feature in this repository so far, and the reason is not
storage — it is *authority*.

- **a fact is an authority on the user.** A wrong task title costs a minute; a wrong student id
  goes on a university form. Once something is "known", every later phase will want to use it, and
  the boundary that keeps a model from acting is the same boundary that has to keep a model from
  remembering;
- **the obvious design is the unsafe one.** "The model notices a pattern in the mail and writes it
  down" produces a store full of inferences nobody reviewed — and then some later phase autofills a
  form with them;
- **a knowledge answer is not a fact about a person.** Phase 4C answers questions from the user's
  *documents*; a document can be old, can be someone else's, and can be quoted. Turning a retrieved
  sentence into personal truth would launder a guess into a credential of correctness;
- **silent overwrites destroy the only thing that makes memory auditable.** If a new value replaces
  the old row, "what did I believe last term, and why" becomes unanswerable — and with a personal
  fact that question is often the point;
- **a personal store is an attractive place to put a password.** It has keys, values, provenance
  and a nice CLI. It must not become one.

Phase 6A already established the shape this phase reuses: prepare something exact, let a human
approve it, keep the history, keep the authority one-way. Phase 7A applies it to memory. It
deliberately stops there — yes, facts are stored, reviewed and queryable; no, nothing consumes them
yet.

## Decision

1. Learning starts as durable candidate state, never silent model memory.
2. A user correction is durable first-class data.
3. `FactCandidate` and `ConfirmedFact` are distinct domain concepts.
4. A `FactCandidate` is never trusted for autofill.
5. Only an unexpired `ConfirmedFact` may be eligible for future autofill.
6. Phase 7A performs no autofill.
7. Promotion from `FactCandidate` to `ConfirmedFact` requires explicit human action.
8. Models, `EventWorker`, `MailAnalysis`, `Interpreter`, `GroundedAnswer` and `Scheduler` cannot
   confirm facts.
9. No model may automatically create a `FactCandidate` in Phase 7A.
10. Every `FactCandidate` must have durable provenance.
11. V1 candidate provenance is an explicit user `Correction`.
12. Fact values are strings in V1.
13. Fact keys are stable namespaced identifiers.
14. Credential, password, token and secret facts are forbidden.
15. Confirming a new value for an already-active key supersedes the old `ConfirmedFact` atomically.
16. Historical facts are never overwritten or deleted by supersession.
17. `ConfirmedFact` may expire.
18. Expired facts remain in audit history but are not active.
19. Rejected candidates remain durable audit records.
20. Candidate confirmation and rejection are single-transition and concurrency-safe.
21. Phase 7A does not send corrections or facts to `ModelPort`.
22. Phase 7A does not infer preferences from behaviour.
23. Phase 7A does not create Playbooks.
24. Future consumers must request facts through a read-only port or service rather than querying
    SQLite directly.

Additional frozen details:

- **Three tables, and only three writers.** `corrections` holds what the user said, verbatim and
  trimmed; `fact_candidates` holds a proposal with the correction it came from; `confirmed_facts`
  holds what a person promoted. An architecture test walks every module in `src/` and fails if
  anything outside the learning path and the composition root even *names* `FactCandidate`,
  `ConfirmedFact`, `LearningService` or a confirmation call.
- **One CLI command is the whole human boundary.** `pw fact candidate confirm ID` promotes; nothing
  else in the project does. There is no `--force`, no `--edit-value` and no `--confirm-all`, and the
  service's `confirm_fact` takes only a candidate reference — there is no parameter through which a
  different key or value could be supplied.
- **Confirmation copies the snapshot.** `confirmed_facts.candidate_id` is `UNIQUE`, so one candidate
  yields at most one fact, and the fact carries the candidate's own `fact_key`, `value` and proposed
  expiry. Its domain-level invariant is checked at construction, so those three fields have to be
  built together; a prompt, a worker or a stray parameter cannot diverge them.
- **Provenance is durable and non-cascading.** `fact_candidates.correction_id REFERENCES
  corrections (id)` with no `ON DELETE` action, and no delete path exists at all: history is
  append-only for learning data in this phase.
- **One current fact per key, decided by the database.** A partial unique index on
  `(fact_key) WHERE superseded_at IS NULL` is the last line of defence; confirming a new value runs
  inside one `BEGIN IMMEDIATE` that re-reads the candidate, retires the key's current row with a
  compare-and-set, inserts the fact and records the candidate's single transition. Two callers
  racing on one key serialise, so at every committed state there is at most one current row.
- **Expiry is a read-time judgement.** `active` means `superseded_at IS NULL AND (valid_until IS
  NULL OR valid_until > now)`, computed from the row and the clock. There is no expiration job, no
  scheduled mutation and no state to fall out of sync; an expired row is still the current row for
  its key, which is exactly why confirmation has to retire it rather than insert beside it.
- **A failed confirmation changes nothing.** Because retirement, insertion and transition share one
  transaction, a failure after the retirement rolls it back: the old fact is still current and the
  candidate is still pending. A test injects precisely that failure through a duplicate fact id.
- **The value window cannot be faked.** A candidate whose proposed `valid_until` has already passed
  cannot be confirmed (`ExpiredFactCandidate`): the domain forbids `valid_until <= valid_from`, so
  the honest answer is "propose a new candidate", not a fact that is born expired.
- **The key namespace is open, with a credential ban.** Keys match
  `^[a-z][a-z0-9_.-]{0,127}$` — lowercase, namespaced, no whitelist of specific keys — and a
  dot-separated segment (or one of its `_`- or `-`-separated words) that names a password, secret,
  token, credential or private key is refused with `ForbiddenFactKey`. Segmentation is by word, so
  `mail.smtp_token` is refused while `profile.tokenizer` is an ordinary key. **The fact store is not
  a credential store**, and the ban is on the key, never on a guess about whether a value looks
  secret.
- **A correction is the only V1 provenance.** There is no "the model suggested it" candidate kind:
  every candidate carries the sentence the user wrote, and that sentence is what review displays.
- **Nothing consumes a fact yet.** No model context, no mail draft, no eHall field, no interpreter
  context and no mobile route reads a confirmed fact in this phase; `LearningService.get_active_fact`
  and `list_active_facts` exist as the single implementation of "current and unexpired" for whoever
  asks later. A test asserts the web route table is unchanged, and another asserts the whole mobile
  path cannot reach the learning service.
- **No Playbook machinery.** The word does not appear anywhere in `src/`, and an architecture test
  keeps it that way: a candidate, a promotion, a replay of a reviewed flow and a tested playbook is
  a design for Phase 7B, and half of it now would be a half-built promise.

## Alternatives Considered

- **Let the model write memory directly.** It is the shortest path to a system that seems to learn.
  Rejected; an unobserved model decision would become an authority on the user, with no review step
  and no provenance.
- **Derive facts automatically from behaviour (which folders are opened, which replies are edited).**
  Behaviour suggests preferences, it does not state them, and the user never sees the inference.
  Rejected; Phase 7A has no behavioural inference at all.
- **Treat a grounded knowledge answer as personal truth.** Retrieval answers "what does this
  document say", including documents that are outdated or about someone else. Rejected; a
  correction is the only provenance in V1.
- **Let `MailAnalysis` create candidates from the mail it reads.** It would fill the review queue
  with inferences from other people's sentences. Rejected; mail analysis produces analysis.
- **Overwrite the confirmed row in place.** One row per key is simpler to query and destroys the
  only evidence of what was believed before. Rejected; supersession retires and keeps.
- **Delete superseded facts to keep the store tidy.** The audit trail is the feature. Rejected; no
  delete path exists for facts, candidates or corrections.
- **Confirm a candidate with an edited value (`--value`).** The user would approve a value they were
  never shown, and the stored provenance would describe a different proposal. Rejected; a change
  means a new candidate, and therefore a new sentence.
- **Bulk confirm (`--all`) or `--force`.** Consent that does not name a specific value is not
  consent. Rejected; one candidate, one decision, each showing the text it came from.
- **Reject the candidate rather than deleting it.** A rejection is information — "I was told this
  and it is wrong" — and deleting it would let the same wrong candidate be proposed again with no
  memory of the decision. Kept, and tested.
- **Store credentials as facts "so the assistant can fill them in later".** A password in a
  fact table is a password in a database that gets copied, backed up and read by reports. Rejected;
  credential-like keys are refused, and keys are never scanned for secret-looking values.
- **Require the user to type the fact again at confirmation.** It would guarantee attention at the
  cost of a transcription error and of provenance that no longer matches. Rejected; the review
  command prints the exact proposal and the sentence, and confirmation copies it.
- **Auto-inject confirmed facts into mail drafts or eHall forms now.** It is the feature the facts
  exist for, and doing it in the same phase that introduces the store would make it impossible to
  tell which part was reviewed. Deferred, and asserted absent.

## Consequences

- The assistant can remember something without anyone trusting a model: a person writes what is
  true, a person promotes it, and the sentence that justified it is still on the record afterwards.
- The store is auditable by construction. Every fact points at a candidate, every candidate points
  at a correction, supersession keeps both versions, expiry needs no job, and a race or a crash
  cannot leave two current answers or an empty key.
- Costs and limits: answering "what is my office" requires reading two more tables, which is why
  the read is exposed as `get_active_fact`/`list_active_facts` rather than SQL; expired facts stay in
  the table forever; a fact with a mistaken value cannot be corrected in place — the user proposes a
  new candidate, which is more typing but keeps the audit honest; and because nothing consumes
  facts yet, the value of this phase is entirely in having a safe place to put them.
