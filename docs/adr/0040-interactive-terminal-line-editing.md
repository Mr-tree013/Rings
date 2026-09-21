# ADR-0040 — Interactive Terminal Line Editing and Pending Confirmation Groups

## Title

`rings` edits lines with a real terminal editor and treats each turn's pending proposal as one
confirmation group, so a revision retires the proposal it replaced.

## Status

Accepted

## Context

Two defects reached a real user in v1.1.0, and both were structural rather than cosmetic.

The first was visible in the transcript:

```text
You > ...下午连点到^[[D^[[D^[[D^[..
You > ics的地点是在仙一107^[[C
Tree > 刚才的输入包含无法识别的终端字符，我没有执行任何操作。请重新输入这一句。
```

`^[[D` and `^[[C` are cursor-left and cursor-right. v1.1.0 read the conversation from the terminal
with `ConsoleInput`, which reads a whole line off `sys.stdin.buffer` and decodes it strictly
(ADR-0035 §4-§6). That is a correct *decoder* and a terrible *line editor*: it leaves editing to
the terminal's canonical line discipline, which knows about ASCII erase characters and knows
nothing about the arrow keys, about Unicode, or about display width. The consequences follow
mechanically:

* arrow keys are not editing commands to a byte-oriented reader — they are bytes, so they became
  conversation text, and the model was asked to interpret `^[[D`;
* Backspace in the canonical discipline deletes bytes. Deleting one Chinese character out of
  `仙一107` can leave an incomplete UTF-8 sequence behind, which the strict decoder then refuses —
  the "unrecognised terminal characters" message the user saw was the Phase 10C fail-closed rule
  doing exactly what it was designed to do with input that no longer meant anything.

The Phase 10C fail-closed behaviour is still right for piped and scripted input. What was missing
was a separate interactive path.

The second defect was a correctness bug in the local confirmation machinery. The user gave one
message that produced a pending three-class proposal; then, before confirming, they said
`ics的地点是在仙一107`. The runtime produced a *revised* three-class proposal — and left the
original three operations `WAITING_CONFIRMATION` as well. When the user finally said `可以`, the
confirmation path treated every waiting operation in the thread as one batch and executed both
proposals, creating a bare `计算机系统基础（ICS）` rule as a side effect of a superseded question.

## Decision

### The terminal boundary has two paths

1. **`rings` is an interactive terminal application and requires a real terminal line editor.**
2. **Arrow keys are editing commands, never conversation content.**
3. **Backspace and Delete operate on Unicode user text, not on individual UTF-8 bytes.**
4. **CJK display width is handled by the terminal editing library**, not by this project.
5. **Interactive TTY input and non-interactive/piped input are separate paths**, chosen from what
   `sys.stdin` and `sys.stdout` actually are, never from an environment guess.
6. **Interactive TTY input uses a mature line-editing implementation** — `prompt_toolkit`, added
   as a line-editing frontend only. There is no TUI framework and no home-grown raw-byte editor on
   this path.
7. **Non-interactive input retains strict decoding and the Phase 10C fail-closed behaviour.**
   `ConsoleInput` and `decode_strictly` are unchanged and remain usable on their own; the strict
   path does not import the editor at all.
8. **Submitted conversation text is validated again after editing**: no NUL, no lone surrogate, no
   raw ANSI control sequence, no unintended C0 control character. Ordinary text — tabs, Chinese
   punctuation, quotes, angle brackets, `@`, emoji, CJK — passes untouched. A rejection is a value,
   not an exception: zero conversation turn, zero model call, zero mutation, and the existing
   friendly recovery message.
9. **Editing history is in-memory only.** It is never written to `~/.history`, `~/.rings_history`
   or any other file, because user prompts may contain private data. Conversation persistence
   remains the existing SQLite conversation system; line-edit history is convenience, not record.
10. **Cursor movement, Home/End, Backspace/Delete and session-local Up/Down history are normal
    product UX.**
11. **Ctrl-C cancels the current unfinished input** and mutates nothing; the session returns to
    `You >`.
12. **Ctrl-D on an empty prompt exits cleanly**; `/exit` keeps working.
13. **A terminal editing failure must not traceback through the user UI.** The editor and its
    event loop are isolated behind the input boundary, and every failure becomes a sentence.
14. **This layer does not interpret natural language and has no domain access**: no store, no
    adapter, no service, no model. It produces a string or a reason, and that is all.
15. **The model must never see terminal control sequences** — enforced by the editor consuming
    keys, by the post-edit validation, and by tests that assert the payload of the actual request.

### A pending local confirmation is a group, and a revision retires the group it replaces

16. **A pending local confirmation group is the set of operations one turn put in front of the
    user**, and `turn_id` is the group boundary. Three weekly classes proposed in one message are
    one question, and `可以` answers that question and no other. No new confirmation framework,
    table or status is introduced: the existing `WAITING_CONFIRMATION` operations and their
    `turn_id` already express this.
17. **Revising a proposal of the same kind makes the older group non-live.** Every operation in the
    older group becomes `REJECTED`, its turn stops claiming to be waiting for a confirmation, and
    it can never be applied afterwards. "Same kind" is the family of the proposal: a revised weekly
    schedule supersedes an earlier weekly schedule, and a newer offer to apply a plan supersedes
    the earlier offer.
18. **One human confirmation settles exactly one group.** A generic `可以`/`确认`/`好` never
    fans out into several historical proposals.
19. **When more than one unrelated group is waiting, Tree asks which one** and applies neither.
    Different kinds of proposal — a weekly schedule and a plan apply — stay live and stay separate;
    the runtime does not guess, and it does not consult the model to resolve it.
20. **`取消` withdraws every outstanding local question.** Cancelling applies nothing, so it needs
    no target: it can only destroy questions, never create effects. (This is the one place where
    an untargeted answer may touch more than one group, and it is safe by construction.)
21. **A superseded group is never applied**, and **an expired group is still `REJECTED`**, exactly
    as before. The TTL, the fingerprint and the crash fence are unchanged.
22. **The external mail confirmation is untouched.** Sending still requires the existing explicit
    vocabulary (`确认发送`, `发送`, …), a generic `可以` still never sends, and a pending local group
    never borrows a send. Fact confirmation keeps its own explicit vocabulary for the same reason.
23. **Confirming a revised group is one answer, not a pile of lines.** A confirmed group renders as
    one list with a single "已加入固定安排：" header; rules that already existed are counted in one
    sentence ("其中 2 条已存在，没有重复添加。"); a partial failure is still reported.
24. **The hotfix does not touch the user's data.** A duplicate rule that v1.1.0 already created
    stays exactly where it is: no migration, no startup cleanup, no automatic retirement. The
    release notes say so and the user retires it themselves.

## Rejected alternatives

* **Manually parsing every ANSI cursor sequence.** Rejected: a home-grown parser is a bug farm, it
  cannot handle IME composition, bracketed paste or CJK width, and it puts terminal vocabulary into
  the conversation runtime.
* **Stripping escape characters after they reach the conversation.** Rejected: the sequence would
  already be part of what the user "said", and the cursor edits it should have performed would be
  silently lost.
* **`errors="replace"`.** Rejected (already, in ADR-0035): a repaired byte sequence can mean
  something the user did not say, and the caller cannot tell afterwards.
* **Deleting UTF-8 bytes manually.** Rejected: byte deletion is what produced the broken Chinese in
  the first place. Deletion belongs to the editor, which knows where characters are.
* **Keeping the current raw-byte reader for interactive TTY use.** Rejected: it is the defect.
* **Persisting command history containing private conversation text.** Rejected: prompts may carry
  private data; the durable record of what was said is the conversation itself.
* **Applying every waiting operation on one `可以`.** Rejected: it is the second defect, and it
  makes a confirmation a bulk action over historical proposals the user cannot see any more.
* **Deleting the obsolete recurring rule automatically.** Rejected: the hotfix prevents future
  duplication; retiring the user's existing data is the user's decision.

## Consequences

* `rings` in a terminal behaves like a terminal: Home/End, arrows, Delete, bracketed paste, CJK
  width and a session-local history all come from the editor.
* A piped `pw chat` keeps today's deterministic decoding, including its fail-closed refusal of
  bytes that cannot be read, so scripts and tests are unaffected.
* Editing happens in a worker thread, because `prompt_toolkit` drives its own asyncio event loop
  and asyncio does not nest inside the running conversation loop.
* One new runtime dependency exists (`prompt-toolkit`), pinned by the architecture freeze test
  together with the reason it is allowed.
* A revised proposal leaves a small, honest trail: the older operations are `REJECTED`, and the
  older turn is finished rather than left pretending to wait forever.
* A user who already has the duplicate rule from v1.1.0 keeps it until they retire it themselves.
