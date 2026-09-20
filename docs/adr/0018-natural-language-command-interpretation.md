# ADR-0018 — Natural-Language Interpretation Produces Typed Non-Executing Command Drafts

## Title

A natural-language request is interpreted into one typed, validated `CommandDraft` that the user
reviews; the interpreter never executes anything, and the model only ever sees a bounded,
explicitly chosen slice of local state.

## Status

Accepted

## Context

Phase 4A built the safe way to ask a model for structured data. Phase 4B uses it for the first
feature where a model's *interpretation* influences what the assistant would do: turning "finish
the SE lab by Friday" into something executable.

The risk is not that the model is wrong — models are wrong. The risk is that being wrong has
consequences: a fabricated task id completing the wrong task, a guessed timezone scheduling a
reminder at the wrong instant, a title carrying instructions being followed, or a request for an
unsupported capability turning into a plausible-looking call to something that should not run.

Three decisions follow from that:

- **interpretation is a preview, not an action.** The user always sees what would happen and the
  exact structured command that would do it; the project executes nothing on the model's word.
- **identity is authorised by context, not discovered later.** A task id is only acceptable if it
  was in the list handed to the model, so a hallucinated or remembered id cannot become real by
  being looked up.
- **the context is chosen, bounded and minimal.** The model sees open task metadata and the
  current time; it does not see descriptions, work sessions, calendar state, notifications,
  scheduler payloads, knowledge content, files or mail.

## Decision

1. Natural-language input never directly invokes application mutations.
2. Interpreter output is a typed `CommandDraft`, not an executed command.
3. Model output remains untrusted after JSON Schema validation and receives semantic validation.
4. The interpreter exposes no tools to `ModelPort`.
5. V1 accepts exactly one logical command per invocation.
6. Ambiguous requests produce `NEEDS_CLARIFICATION`.
7. Unsupported requests produce `UNSUPPORTED`.
8. The model may only reference existing tasks whose ids were supplied in the bounded context.
9. Invented or out-of-context entity ids are rejected deterministically.
10. Task descriptions are not sent to the model.
11. Knowledge documents and content are not sent to the model in Phase 4B.
12. Notifications, work session content, scheduler payloads, file contents and personal facts are
    not sent.
13. Open task metadata is the only personal-state context supplied in V1.
14. Context size is bounded and deterministic.
15. Context values such as task titles are data, never instructions.
16. The interpreter does not persist user input, prompts, raw model output, or reasoning.
17. Phase 4B adds no model transcript database.
18. Commands containing interpreted dates or times require a configured planning timezone.
19. The interpreter never guesses the machine-local timezone.
20. All model-produced datetime values must be timezone-aware ISO 8601.
21. No model-produced shell command is executed automatically.
22. CLI rendering uses deterministic shell quoting.
23. The interpreter does not execute internal mutations even when they are reversible.
24. External actions such as mail, eHall and browser automation are unsupported in this phase.
25. Multiple-action requests are not decomposed automatically in V1.
26. The existing structured CLI remains authoritative for execution.
27. A future Agent Runtime may consume the same typed `CommandDraft`s through a separately
    designed execution boundary.
28. No tool loop, retrieval augmentation, conversation memory or autonomous planning is
    introduced.

Additional frozen details:

- **Supported commands are a closed set**: `CREATE_TASK`, `COMPLETE_TASK`, `CANCEL_TASK`,
  `SET_DEADLINE`, `CLEAR_DEADLINE`, `CREATE_CALENDAR_EVENT`, `REQUEST_WEEK_PLAN`. Editing a task
  is deliberately *not* interpreted in V1: `unchanged` / `set` / `clear` cannot be expressed
  without a second value rule for `description` and `estimate`, so an edit request is answered
  with `UNSUPPORTED` rather than being mapped onto a guess.
- **The schema is the boundary.** It is a closed object whose `status` selects exactly one of
  three mutually exclusive branches; every command is its own closed object with `kind` as a
  `const` and *all* of its fields required (nullable where "the user did not say"), so "absent"
  and "null" can never be confused. An unknown command fails schema validation — it does not
  degrade into "unsupported".
- **Semantic validation is separate and local.** Drafts reuse the entity rules
  (`validate_task_title`, `validate_estimated_minutes`, `validate_event_interval`), datetimes are
  parsed again as aware ISO 8601 instants, and a backwards interval is rejected rather than
  swapped.
- **Reference validation is exact.** A `task_id` must match one of the ids in the context that
  was sent; matching by title, resolving prefixes, or re-querying the database are all forbidden.
  Two tasks with the same title therefore force a clarification, not a coin flip.
- **Timezone policy is deterministic.** A time-bearing draft (a deadline, a calendar interval,
  or a weekly plan request) without a configured `[planning].timezone` becomes a clarification
  with a fixed question. The model cannot escape it by inventing an offset, and the local code
  never interprets natural-language time itself.
- **One request, one message.** The request is a single USER message holding canonical JSON
  (`sort_keys`, compact separators, `ensure_ascii=False`) with the user's text and the context;
  the fixed instructions carry the rules. Context values are never spliced into the
  instructions.
- **The context is capped at 50 tasks**, ordered deterministically (deadline tasks by due date,
  then the rest; priority, creation time, id), and `tasks_truncated` tells the model when the
  list was cut, so it must ask rather than invent.
- **Nothing is persisted.** No migration, no `command_proposals`, no `interpretations`, no
  conversation memory. The interpreter also does not log the request text, task titles or model
  output.
- **Previews are local artifacts.** The human summary and the equivalent `pw …` command are
  rendered by this project from the typed draft, with `shlex.quote` on every user-provided
  value. The model never produces a shell string.
- **Results carry no confidence and no rationale.** `InterpretationResult` holds a draft, a
  question or a reason — nothing that invites acting on a model's self-assessment.

## Alternatives Considered

- **Let the model call `TaskService` directly**: the shortest path to a demo, and it hands an
  untrusted generator the ability to mutate state. Rejected; there is no execution boundary yet.
- **`pw interpret --apply`**: convenient, and it makes the preview decorative. Rejected; the
  structured CLI stays the only place a mutation is authorised.
- **Persist raw model responses for audit**: useful eventually, and it would store prompts and
  personal context without a retention or review design. Rejected for this phase.
- **Send all personal knowledge into the prompt**: it multiplies prompt-injection surface and
  retrieval budget decisions that have not been designed. Rejected; knowledge-grounded answering
  is a later phase with its own ADR.
- **Let invented titles or ids resolve at execution time**: silently binds an interpretation to
  whatever row happens to match later. Rejected; identity must come from the supplied context.
- **Fall back to the machine timezone**: makes "tomorrow" mean whatever the machine thinks, and
  the machine is not the user. Rejected.
- **Allow several commands per response**: the model would be choosing an order of operations
  the project never designed. Rejected; one action per invocation.
- **Let the model emit the shell command itself**: an entire class of injection and quoting bugs
  with no upside. Rejected; the local renderer owns the command text.

## Consequences

- The user gets the value of natural language without giving up authority: interpret, read the
  preview, run the structured command.
- The interpreter is testable without a provider: every semantic, reference and timezone rule is
  a pure function over a schema-valid dict, and the whole feature runs on `FakeModelAdapter`.
- Privacy is structural rather than promised: the context type cannot carry a description, a
  work session, a notification or a knowledge document, so those cannot leak through a prompt.
- Costs: one action per invocation, no conversation memory, an unsupported answer instead of a
  clever guess, and two extra pieces of machinery (a schema and a renderer) that must be kept in
  step with the structured CLI. Those are deliberate: the next phase can add an execution
  boundary on top of drafts that are already validated and already reviewable.
