# ADR-0035 — Conversation Reliability, Input Decoding and Capability Introspection

## Title

`rings` is a product surface, so nothing recoverable may crash it: terminal input is decoded
strictly and recoverably, structured model output is normalized and repaired exactly once before
anything runs, and what Tree says it can do comes from the runtime's own capability metadata rather
than from prose in a prompt.

## Status

Accepted

## Context

Phase 10A and 10B made the conversation the primary surface and kept every safety boundary intact.
Real human use then failed in five ways that no safety test had covered, because they are usability
and robustness failures rather than authority failures:

* an input byte sequence the terminal could not decode reached `input()` and ended the process;
* the wire schema asked the model for all four top-level fields, so a perfectly ordinary answer
  (`{"mode": "direct_reply", "reply": "…"}`) or a null `operations` was rejected as a schema
  violation — and the validator's own sentence was shown to the user;
* "What can you do?" was answered from prose in the prompt, which had drifted: it still said the
  product could not send mail after Phase 10B had shipped exact-review sending;
* "Which mailbox is configured?" had no answer at all, and the nearest thing (a message count) is a
  different question, so a mailbox question could look like "0 messages";
* a plan with several operations executed them one at a time, so an operation refused later left
  earlier mutations applied with no way for the user to know.

None of these is a reason to weaken a boundary. Each is a reason to make the boundary *legible*:
fail closed, say so in words a person can act on, and keep the session alive.

## Decision

1. `rings` is a primary product surface, so recoverable user-input, model-output or application
   errors must never produce an uncaught traceback.
2. No user mutation may occur from malformed or partially validated model output.
3. Conversation model output is untrusted input.
4. Benign structured-output variations may be normalized only when doing so cannot create or
   broaden an operation.
5. Unknown operations, malformed arguments and ambiguous mutations remain fail-closed.
6. One bounded structured-output repair attempt is allowed before presenting a friendly
   interpretation failure.
7. Structured-output repair is not a tool loop and performs no side effect.
8. Raw schema/JSON/provider errors are not shown in the normal user UI.
9. Debug detail remains available to developers through an explicit debug mode or stderr
   diagnostics.
10. Terminal decoding errors must not terminate the conversation loop.
11. Invalid input bytes must never be silently converted into a potentially different executable
    user intention.
12. Tree capability descriptions come from actual runtime capability metadata, not stale prose in
    the prompt.
13. Configured capability/account state and stored domain data are different concepts and must be
    reported separately.
14. Mail account introspection exposes safe metadata only and never credentials.
15. Multi-operation conversational writes are fully preflighted before the first mutation.
16. If one operation in a turn requires clarification, unsupported semantics or invalid references,
    the turn must not partially apply earlier mutations.
17. Unknown/crashed local mutation semantics from ADR-0033 remain unchanged.
18. `ActionRequest` / `Approval` / `ExecutionRun` semantics from ADR-0034 remain unchanged.
19. This phase does not add new product capabilities merely to satisfy a user phrase.
20. Conversation reliability is a release gate for v1.1.

Freezing details:

- **The terminal is a boundary, not a convenience.** `input()` is replaced by a `ConsoleInput`
  reader that reads raw bytes and decodes them strictly with the *declared* encoding
  (`sys.stdin.encoding`, then the locale's preferred encoding, then UTF-8 — each attempt strict and
  documented). A byte sequence no candidate can decode, or a decoded string carrying lone
  surrogates (which cannot be re-encoded and is therefore not what the user typed), becomes a
  `DECODE_FAILED` outcome: no turn, no model call, no mutation, no crash, and a short sentence
  asking for the input again.
- **Normalization is a whitelist.** Only the two shapes a provider legitimately emits are repaired
  (`operations: null`/absent → `[]`, `reply`/`clarification` absent → `None`), and only in the mode
  where the field is meaningless. An unknown operation, a malformed argument, a guessed id, an
  invented `mail.send` or a `null` where a value is required is still refused.
- **One repair, then words.** A failed response may be sent back once with the compact validation
  issue and the same closed schema. A second failure is `MODEL_REPAIR_FAILED`: nothing executes,
  and the user reads "我没能可靠地理解这句话，因此没有执行任何操作" rather than a JSON pointer.
- **Capabilities are data.** `system.capabilities` reads the registry plus the loaded
  configuration and reports each area as `AVAILABLE` / `DISABLED` / `NOT_CONFIGURED` /
  `UNAVAILABLE`. The same snapshot feeds `/help` and the model context, so the prompt no longer
  carries a second, aging description of the product.
- **Accounts are configuration; messages are content.** `mail.accounts` reports what is configured
  (id, address, host, enabled, receive/send readiness) and, separately, how much is cached
  locally. No credential, token or password can appear in it.
- **Preflight before mutation.** A turn validates every proposed operation — capability existence,
  arguments, references, time context, confirmation policy and basic dependencies — before the
  first mutation runs. A refusal anywhere in the plan applies nothing and says so.

## Rejected alternatives

- **Catch `Exception` and ignore it.** Silently continuing hides a broken boundary; the phase needs
  named failure modes with named user-facing sentences.
- **`errors="replace"` and then execute the corrupted sentence.** The replacement character can
  change an intention; an input we cannot decode is an input we must not act on.
- **Hardcoding GBK/GB18030/cp936.** Without environment evidence that is a guess about someone
  else's terminal; the declared encoding is the evidence, and it is checked in order.
- **Showing raw JSON-schema errors to users.** "required property" is not a sentence a person can
  act on, and it leaks internals into a product surface.
- **Weakening the operation schema to accept arbitrary dicts.** Friendly input handling must not
  become permissive execution; the schema stays closed and typed.
- **Turning repair into an unbounded retry loop.** One bounded attempt, no tool loop, no side
  effect, and the second failure is final for that turn.
- **Hardcoded "what I can do" prose.** It already drifted within one phase; capability answers come
  from the registry and the configuration or they are not answers.
- **Using message count as mailbox configuration.** "Which mailbox do you use?" and "how much mail
  is here?" are different questions, and conflating them produces confident nonsense.
- **Adding new outbound mail to make a demo phrase work.** Reply-only is the v1.1 baseline;
  arbitrary compose is future product work, and a conversation is not a reason to add it.

## Consequences

- The conversation loop has an explicit error boundary: recoverable failures become sentences and
  the session continues, while `APPLYING`, `UNKNOWN_LOCAL` and `ExecutionRun UNKNOWN` keep their
  existing meaning.
- The prompt shrinks: capability prose is replaced by a bounded, deterministic snapshot, so the
  model's "what I can do" answers can no longer contradict the build.
- `/help`, `system.capabilities`, `mail.accounts` and the prompt read the same source, which is what
  keeps them from drifting apart again.
- No migration is needed: the existing `conversation_turns` and `conversation_operations` statuses
  already express everything this phase needs.

## Implementation notes

- Conversation runtime and typed plans: ADR-0033. Exact external review: ADR-0034.
- Typed non-executing interpretation and its timezone policy: ADR-0018.
- Mail ingestion, threading, drafting and approved sending: ADR-0020 to ADR-0024.
