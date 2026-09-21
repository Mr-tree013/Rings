# ADR-0038 — Conversational Fact Confirmation

## Title

Long-term personal facts can be proposed in a conversation, but they become durable truth only
after a second, explicit human confirmation that bypasses the model.

## Status

Accepted

## Context

Phase 7A built the durable half of personal learning: a `Correction` (the user's own words), a
`FactCandidate` (a proposed key and value, justified by that correction) and a `ConfirmedFact`
(the value a person promoted by hand, with one current value per key and supersession instead of
deletion). It deliberately shipped no consumer.

The conversation is the natural place to ask for one. It is also the most dangerous place to
create one, for three reasons that are about meaning rather than mechanics:

* a conversation statement is not a memory request. "我的办公室在仙林" is something a person says
  while thinking out loud; turning every statement into durable truth about them would make the
  assistant quietly remember things nobody asked it to remember;
* a model that can write personal memory can be talked into writing it. An incoming mail body, a
  display name or a pasted paragraph can contain the words "记住". If the model could confirm, a
  stranger's text would become the user's facts;
* a generic "可以" already means "yes" to a plan. If it also confirmed personal facts, the
  difference between arranging a week and remembering a permanent truth would be a shrug.

Four shortcuts were rejected before this phase started:

* chat history as memory. Conversation history is durable text, not durable truth; it has no
  promotion step, no provenance and no supersession;
* the model confirming. Confirmation must be a deterministic function of the human's own turn,
  exactly like the external-send review;
* facts as contacts, or contacts as facts. Contacts answer "which address does this name mean?";
  facts answer "what is true about me?". Mixing them would give an address book the authority of
  remembered personal truth;
* a second memory model. A vector store or a provider-side memory would be a new truth authority
  beside `ConfirmedFact`, with its own invisible promotion path.

## Decision

1. Conversation history is not durable personal memory.
2. A normal chat statement never automatically becomes a `ConfirmedFact`.
3. Explicit remember or correction intent may create a reviewable fact proposal.
4. Long-term fact confirmation requires a second explicit human confirmation.
5. The model may propose fact content, but it can never confirm it.
6. Final confirmation is deterministic and bypasses `ModelPort`.
7. Generic `可以`, `好`, `嗯`, `继续`, `ok` and `yes` are not sufficient for long-term fact
   confirmation. Accepted phrases are action-specific: `确认记住`, `记住`, `确认保存`, `确认记录`.
8. Cancellation uses `不要记`, `别记` or `取消`, and resolves the pending candidate as rejected
   using the existing candidate status.
9. Fact confirmation is a local knowledge mutation: it creates no `ActionRequest`, needs no
   `Approval` and produces no `ExecutionRun`.
10. The existing `FactCandidate`, `ConfirmedFact`, provenance and supersession semantics stay
    authoritative. This phase adds no second fact model and no fact table.
11. `ConfirmedFact`s do not automatically feed mail recipients, mail bodies, forms, contacts or any
    external action. Nothing in this phase reads a fact into an outbound surface.
12. Fact values are not dumped into every model prompt. Only bounded metadata enters the context:
    the key of a confirmed fact, and the key plus status of a pending candidate.
13. A pending fact confirmation survives a conversation restart, because it is the pending
    candidate itself. The durable row in the existing fact store is the review state, so no
    conversation-side table is needed.
14. There is no hidden model memory. If a fact is not a row in `confirmed_facts`, the assistant
    does not know it.
15. A question about a personal fact is answered from `ConfirmedFact`s through a read operation
    whose text the runtime renders; the model never states a personal fact from memory.
16. A correction is a new proposal for the same key. The existing supersession happens at the
    moment of confirmation, as it already does for the CLI, and the previous value stays as history.

## Model-facing operations

Closed vocabulary: `fact.list` (read), `fact.show` (read, by key) and `fact.propose` (local write:
key, value, and the user's own sentence as the correction text).

There is deliberately no `fact.confirm`, `fact.delete`, `fact.execute`, `memory.write` or
`memory.store`: final confirmation is not something a model may name or perform.

## Why no migration

The existing durable state is sufficient. `conversation_operations` already records what a turn
proposed, `FactCandidate` already records a pending value with its provenance and status, and the
deterministic confirmation path resolves "which candidate" from the pending candidates themselves:
exactly one candidate confirms it, several ask which, none means nothing is waiting. The latest
shipped migration therefore remains 0019, and the fact store keeps one source of truth.

## Alternatives considered

* Automatically converting all chat into facts. Rejected: a statement is not a request to remember,
  and "what did I say" is not "what is true about me".
* Using contacts as facts. Rejected: identity resolution and personal truth are different questions
  with different lifecycles.
* Using conversation history as truth. Rejected: text with no promotion step is not authority.
* Model auto-confirmation. Rejected: untrusted content would decide what is true about the user.
* A generic yes confirms personal memory. Rejected: it would collapse two different consents into
  one word.
* Facts automatically filling mail or forms. Rejected for this phase: a consumer needs its own
  argument (ADR-0027).
* A new vector-memory or provider-side memory system. Rejected: a second truth authority beside
  `ConfirmedFact`.

## Consequences

* `记住我的办公室在仙林` produces a candidate and an exact preview, and nothing durable about the
  user changes until they say `确认记住`.
* `我的办公室在仙林` (no remember intent) produces no candidate at all; the assistant may offer to
  remember it, and the user decides.
* A restart can re-show the pending preview from the candidate itself, and can never confirm it.
* `pw facts`, `pw fact candidate confirm` and the conversation all confirm the same rows through the
  same service, so there is one fact lifecycle.

## Implementation notes

* `application/conversational_facts.py` is a thin orchestration over the existing
  `LearningService`: propose (rejecting an older pending candidate for the same key, so a correction
  never leaves two live proposals), pending candidates, confirm, list and show.
* `ConversationService` settles a fact confirmation before interpreting, from the raw human turn,
  exactly like the external-send review (ADR-0034).
* Domain and storage are unchanged: ADR-0027, `domain/fact.py`, `domain/correction.py`,
  `store/learning.py`.
* Today Brief surfaces a pending fact review as a waiting item (ADR-0039).
