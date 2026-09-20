# Tree Conversation

## What it does

Tree Conversation is the primary way to use Rings. You say what you want in your own words, and a
local runtime decides what may happen: it performs the local work itself, asks before a
structurally significant change, and answers from your own indexed sources.

It is not a general assistant with tools. The model is given a closed list of typed local
operations — tasks, calendar, work sessions, weekly planning, the reminder inbox and grounded
knowledge questions — and it can only ever *propose* one of them. The runtime, not the model,
decides whether a proposal is allowed, whether it needs your confirmation, and who executes it.

## Start it

```bash
uv run rings          # the primary conversational entry point
uv run pw chat        # the same runtime, from the existing CLI
```

Both build the same `ConversationService`; there is no second implementation to keep honest. What
you need first:

* a `[model]` section in `~/.config/growing-assistant/config.toml` and `DEEPSEEK_API_KEY` in the
  environment — the conversation cannot run without a model;
* `[planning].timezone` if you want to talk about time ("tomorrow at three", "next Friday").
  Without it, a time-bearing request gets a question instead of a guess.

On start-up Tree resumes the most recent ACTIVE conversation and says so. The whole session is one
process: the provider client is opened once, not per turn.

## What you can say

```text
加个任务，周五前交报告
我还有什么没做？
刚才那个任务是什么？
把报告优先级改高一点
报告改到周日截止
刚才那个任务做完了
周四下午两点到四点是课
今天论文写了一个半小时
帮我安排一下这周
看看刚才的计划
这周还有什么空闲时间？
我的课程说明里有没有写补交规则？
最近有什么需要处理的邮件？
回复刚才那封，说我周五之前交。
确认发送
帮我查一下刚才那封邮件有没有发出去。
```

Each of those becomes one or more typed operations. A turn proposes at most five, in order, and
Tree performs them one after another — with one exception, below.

## Planning is proposed, then confirmed

Planning keeps its existing shape: the deterministic planner produces a `PlanProposal`, Tree
summarises it, and **nothing is written to your calendar until you say yes**.

```text
You > 帮我安排一下这周
Tree > 周计划提案 ae793e95（2026-09-21 起）：提出 3 个时间块，有 0 条提示。
       - 2026-09-21 09:00（+08:00） → 2026-09-21 11:00（+08:00）
       …
       要应用这个计划吗？回复「可以」我就写进计划，或回复「取消」。
You > 可以
Tree > 已应用周计划提案（ae793e95）：新增 3 个时间块，替换 0 个。
```

The confirmation is answered by a fixed, deterministic vocabulary — no model is consulted, so
"可以" cannot be creatively reinterpreted:

```text
yes: 可以 · 好 · 好的 · 行 · 确认 · 确定 · 应用 · 是 · 嗯 · ok · okay · yes · y
no:  取消 · 不要 · 不用 · 算了 · 不 · 否 · 别 · no · n · cancel
```

A confirmation is bound to the conversation, the operation, its fingerprint and a 30-minute
expiry. Saying yes twice applies the plan once; a confirmation that expired is refused and you are
asked to regenerate it. If more than one confirmation is somehow pending, Tree asks which one
rather than choosing.

Among the local operations, `plan.apply_proposal` is the only one that requires a confirmation:
everything else is either a read (executes immediately) or an unambiguous local write (executes
immediately). Mail is different, and is described next.

## Mail: draft, exact preview, then one explicit send

Mail is the one capability in the conversation that can leave the machine, and it is deliberately
the slowest one to use:

```text
You > 最近有什么需要处理的邮件？
Tree > 最近 3 封：
       - [4f2a1c08] teacher@example.edu · SE 实验三 · 2026-09-21 09:00（+08:00） · 需要回复

You > 回复张老师，说我周五之前交
Tree > 草稿已准备好（…）        ← 起草用的是既有的 reply-draft 服务

You > 确认发送，把要发的内容给我看
Tree > 将要发送的邮件（以下内容就是实际发出的内容）：
       · 发件账号：smail（student@example.edu）
       · 收件人：teacher@example.edu
       · 主题：Re: SE 实验三
       · Message-ID：<…>
       正文：
       ---
       好的，我周五之前交。
       ---
       确认发送吗？回复「确认发送」我就发送，或回复「取消」。

You > 确认发送
Tree > 已发送。
```

What makes this safe:

* **The preview is the payload.** It is rendered from the immutable `ActionRequest` the existing
  mail-send service produced — not from the draft, and not from anything a model wrote. What you
  read is what the executor sends.
* **A first turn can prepare, never send.** "直接发", "不用给我看" and "自动发送" all end in the same
  preview, because a first turn has shown you nothing to consent to.
* **Only your own explicit phrase settles it.** Sending accepts
  `确认发送 / 发送 / 发吧 / 确认发出 / send / confirm send`; withdrawing accepts
  `取消 / 不要发 / 不发送 / cancel`. "可以", "好", "嗯" and "ok" are **not** send confirmations —
  they confirm a local plan and nothing else.
* **The confirmation never reaches a model.** It is parsed by code, and the challenge token the
  approval needs is created and consumed inside that one deterministic call. You never see it.
* **Editing invalidates the review.** Change the draft after the preview and the old confirmation
  goes stale; Tree prepares a new action (a new fingerprint) and shows a new preview.
* **An interruption is never retried.** A send whose result is unknown stays unknown and is never
  resent automatically. You can ask "帮我查一下到底发出去没有", which uses the existing Sent-folder
  reconciliation and its "not found does not prove it was not delivered" rule.

Conversational mail is reply-only, exactly like the underlying v1 mail domain: no contact book, no
new-message compose, no attachments and no scheduling.

## What it will not do

* **No eHall and no generic actions.** Mail is the only external capability a conversation can
  prepare; submitting eHall forms, creating an approval and executing an arbitrary action are not
  in the capability set. Ask anyway and Tree says so — in words, not by pointing you at six
  commands.
* **No automatic facts.** "记住我的办公室在仙林" is a statement in a conversation, not a human
  confirmation of a durable fact. Long-term facts keep their own confirmation flow, and Phase 10A
  does not wire the conversation into it.
* **No guessing.** An ambiguous task, an id that was never in the context, a missing timezone and
  an expired confirmation all produce a question.
* **No shell, filesystem, browser or HTTP.** There is no generic tool loop; the plan vocabulary is
  closed, and an operation outside it is a refused answer rather than an adventurous one.

## If the process dies mid-write

A local write is recorded as `APPLYING` before the service is called and `APPLIED` only after it
returned. If the process disappears in between, the next session reports it and marks the operation
`UNKNOWN_LOCAL`: Tree will not replay it, because it cannot know whether the first attempt
succeeded. Inspect the current state, then ask again.

## Session controls

These are conversation-session controls, not domain operations:

```text
/help          show natural-language examples (tasks, planning, mail, knowledge)
/new           archive this conversation and start a new one
/threads       list recent conversations
/use <id>      switch to one of them (id or unique prefix)
/exit          leave
```

`/help` shows examples of what you can say. It deliberately does not list the 100+ `pw` commands:
the CLI remains the advanced surface and its own `--help` is the place for it.

## Privacy

* What you say is stored in the runtime database — `conversation_messages` — because a conversation
  is durable history. It is inside the same backup and restore as everything else.
* Normal logs contain thread ids, turn ids, operation types and counts. They never contain your
  message text, Tree's answer, knowledge excerpts or prompt contents.
* Each turn is given a bounded context: the last 20 messages and at most 16,000 characters of them,
  plus at most a dozen recent tasks, proposals, calendar events and notifications. Mail bodies,
  documents, facts and playbooks are not attached.
* Knowledge reaches a turn only through the grounded-answer boundary, which returns citations from
  your indexed sources — or says it found no evidence.
* A pending send stores a *pointer* to the prepared action (its id, type and fingerprint) — never
  the challenge token, never a copy of the body as conversation state. The preview is re-read from
  the action itself every time it is shown.

## Commands

```text
uv run rings
uv run pw chat
```

Everything else in the conversation is a sentence. The structured commands remain available and
unchanged: see [getting-started.md](getting-started.md), [planning.md](planning.md) and
[knowledge.md](knowledge.md).

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| `no credential available` | Set the model API key in the environment; the conversation needs a provider. |
| A time-bearing request is answered with a question about the timezone | Add `[planning].timezone` to the host configuration. |
| "这一步需要你确认" repeats | The confirmation is waiting for one of the yes words above; anything else is a new request. |
| "上一次的本地写入在完成前中断了" | A crash left an `UNKNOWN_LOCAL` operation. Check the current state, then ask again. |

- There are no MCP prompts, no sampling and no conversation-driven external actions in Phase 10A.
- Conversation content is deliberately *not* indexed as knowledge: your documents are the source
  of truth, not your chat log.

## Implementation notes

- The conversation runtime and its boundaries: [ADR-0033](../adr/0033-tree-conversation-runtime.md)
- The typed-draft precedent it follows: [ADR-0018](../adr/0018-natural-language-command-interpretation.md)
- Grounded answers: [ADR-0019](../adr/0019-source-grounded-knowledge-answers.md)
