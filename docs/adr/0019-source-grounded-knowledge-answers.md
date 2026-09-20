# ADR-0019 — Source-Grounded Answers from Bounded Untrusted Evidence

## Title

`pw ask` answers only from indexed personal sources: deterministic local retrieval supplies
bounded, untrusted evidence, the model may cite only the opaque source ids it was given, and every
path, page and line number the user sees is resolved locally.

## Status

Accepted

## Context

Phase 4B proved that a model can be asked for validated structured data without being given any
ability to act. Phase 4C uses that boundary for the first feature whose *input* is the user's own
documents: answering a question from the personal knowledge index.

Retrieval changes the risk picture in three ways:

- **document text is attacker-shaped input.** Anything the user has ever indexed — a course
  notice, a downloaded PDF, an email print-out — can contain instructions aimed at the model. It
  must be *quoted*, never followed, and it must not be able to smuggle a capability in;
- **a citation is a claim about reality.** If a model can name a path, a page or a line range,
  the user has no way to tell a real source from an invented one, and the answer stops being
  checkable;
- **"the model knows" is not an answer to this question.** A grounded answer exists precisely to
  say what the user's *own* material says. Falling back to world knowledge would make the feature
  confidently wrong about the user's life.

## Decision

1. Grounded answering is read-only.
2. Retrieval remains deterministic and local.
3. The model does not choose or call search tools.
4. V1 uses the user's question itself as the search query.
5. Only full-text content hits may serve as answer evidence.
6. Metadata-only matches are not factual evidence.
7. Offline roots are reported, but their unavailable content is never inferred.
8. Retrieved document text is untrusted data, not prompt instructions.
9. Evidence enters the model only as canonical structured data.
10. Evidence context is bounded by hit count and by a character budget.
11. Physical filesystem paths are never sent as source identity.
12. Evidence uses the stable logical URI plus a `SourceSpan`.
13. The model receives locally assigned opaque source ids (`S1`, `S2`, ...).
14. The model never outputs logical URI, page or line metadata itself.
15. The model may cite only the source ids supplied to it.
16. Citation ids are validated deterministically after the model answers.
17. Every answer segment must contain at least one valid citation.
18. An unknown or fabricated source id causes deterministic rejection.
19. When the evidence is insufficient, the result is `INSUFFICIENT_EVIDENCE`.
20. No general-knowledge fallback is allowed in grounded-answer mode.
21. No web search is used in this phase.
22. No task, calendar, planner or scheduler context is sent.
23. No `CommandDraft` or interpreter execution is involved.
24. No tool loop is added to `ModelPort`.
25. No source document is modified.
26. No raw model reasoning is persisted or surfaced.
27. Questions and model outputs are not persisted in Phase 4C.
28. Grounded answers are generated only through the explicit `pw ask` command.
29. The knowledge index remains derived data; original files remain the authority.
30. Future read-only tool composition must preserve the same citation boundary.

Additional frozen details:

- **Evidence comes from the index, never from the file.** The chunk text is the representation the
  search ranked; re-reading the original would fork that representation and would behave
  differently for a detached vault, a reparsed PDF or a file that changed since indexing.
- **Retrieval is two deterministic local steps.** The question is first searched verbatim (Phase
  2's phrase matching). If that matches nothing, the same question is split into its own keywords
  locally — lowercase, non-alphanumerics as separators, stopwords and sub-trigram tokens dropped,
  deduplicated, capped at eight, in the question's own order — and those tokens are searched
  individually, fused by reciprocal rank. No model writes, expands, rewrites or reranks a query,
  and `derived_queries` records exactly which queries were issued. Without this step a
  natural-language question would match nothing, because the index matches phrases.
- **Bounding is explicit**: `MAX_EVIDENCE_CHARS_PER_CHUNK = 4000` (deterministic first-N Unicode
  code points, never a tokenizer or a summariser) and `MAX_EVIDENCE_CHARS_TOTAL = 24000`, applied
  in retrieval-rank order, with `content_truncated` telling the model when it sees part of a
  chunk. Deduplication is by `(root_id, chunk_id)`, keeping the highest-ranked appearance, and ids
  are assigned after deduplication so the same question over the same index produces byte-identical
  context.
- **`EvidenceId` is a request-local capability token** (`^S[1-9][0-9]*$`), not durable identity.
  An answer citing an id it was not given is refused, never repaired.
- **The schema allows two shapes and nothing else**: an answered result with 1-12 segments, each
  citing 1-8 supplied ids, or an insufficient-evidence result with a short reason and no segments.
  There is no field for a path, page, line, confidence or rationale — the model cannot author
  source metadata because the format has nowhere to put it.
- **The renderer resolves sources locally**, listing only the evidence the answer actually cited,
  once each, in first-citation order, as `[S1] <logical URI> - <page N | lines X-Y>`.
- **No evidence means no provider call.** The context is built first; if nothing indexed matches,
  the answer is determined locally and no model client is constructed, so a host without `[model]`
  still gets an honest answer.
- **Side effects are structurally impossible in this phase**, because the model has no tool, no
  mutation service and no writable path. Answer *quality* still depends on the model following
  these instructions, so this ADR does not claim prompt injection is impossible — it claims that
  injection cannot cause an action.

## Alternatives Considered

- **Let the model choose its own search query or call search tools**: it converts a deterministic
  local step into another untrusted, unrepeatable one, and it is the first half of a tool loop.
  Rejected.
- **Send the whole personal vault to the model**: unbounded cost, unbounded injection surface, and
  no way for the user to know which document an answer came from. Rejected.
- **Use metadata-only hits as evidence**: a file named `deadline-notes.md` says nothing about what
  is inside it — and for an offline vault there is no content at all. Rejected.
- **Let the model generate source paths, page numbers or filenames**: the user could not
  distinguish a citation from a plausible invention. Rejected; locations are resolved locally.
- **Fall back to general knowledge when retrieval is weak**: it would answer questions about the
  user's own life from a model's priors, with no way to tell. Rejected; the answer is
  `INSUFFICIENT_EVIDENCE`.
- **Add a second autonomous agent to verify the first**: more model calls, more cost and another
  unreviewable decision, to check a claim that deterministic citation validation already covers.
  Rejected.
- **Store the conversation transcript now**: it would persist questions and personal document
  excerpts without a retention or review design. Rejected.
- **Mix task state and knowledge state into one prompt**: two different trust models in one
  context. Rejected; the interpreter and the grounded answer stay separate paths.

## Consequences

- Every sentence of an answer is traceable: if it is not attached to a source the user was shown,
  it is not displayed.
- The privacy boundary is structural: the evidence type cannot carry a description, a work
  session, a notification, a scheduler payload or a physical path, so those cannot leak through a
  prompt.
- The feature works offline for retrieval and only calls a provider when there is something to
  ground on, which also means a host without `[model]` gets a correct answer about having nothing
  to quote.
- Costs: answers are only as good as the index (a document that was never indexed cannot be
  cited); a question whose keywords do not appear in any chunk returns `INSUFFICIENT_EVIDENCE`
  rather than a best guess; and the keyword fallback is deliberately crude — no stemming,
  synonyms, embeddings or reranking, so recall depends on the user's words appearing in the
  material.
