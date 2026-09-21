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
uv run rings          # the terminal conversation: SSH, development and fallback
uv run pw chat        # the same runtime, from the existing CLI
uv run rings --web    # opens the browser chat page, when the control plane is running
```

The most comfortable everyday surface is the browser one. It needs the same-LAN control plane to be
enabled, which also means it needs a model:

```toml
[mobile]
enabled = true
bind = "loopback"     # this machine only; "lan" reaches the same trusted network
port = 8791
```

```bash
uv run assistantd     # serves /chat alongside everything else
```

Then open `http://127.0.0.1:8791/chat`. It is the same conversation runtime, the same database and
the same confirmation semantics as the terminal — not a second implementation.

Both build the same `ConversationService`; there is no second implementation to keep honest. What
you need first:

* a `[model]` section in `~/.config/growing-assistant/config.toml` and `DEEPSEEK_API_KEY` in the
  environment — the conversation cannot run without a model;
* `[planning].timezone` if you want to talk about time ("tomorrow at three", "next Friday").
  Without it, a time-bearing request gets a question instead of a guess.

On start-up Tree resumes the most recent ACTIVE conversation and says so. The whole session is one
process: the provider client is opened once, not per turn.

Two reliability promises are worth knowing before you rely on it:

* **A malformed model or provider response never executes anything.** Tree normalizes the benign
  variations providers actually emit (`operations: null`, a missing field), and if an answer is
  still unusable it may repair it **once** with the same closed schema. If that also fails, the turn
  ends with a sentence and nothing has happened.
* **A terminal decoding failure does not end the session.** If the bytes your terminal sent cannot
  be read with the declared encoding, Tree says so, executes nothing, and keeps the prompt open.

What Tree says it can do comes from the runtime's own capability metadata rather than from a script:
`你能做什么` reflects the accounts, roots and timezone this host actually has, and `/help` uses the
same source.

## Editing a line

When you run `rings` in a real terminal, the prompt is a real line editor:

| Key | What it does |
| --- | --- |
| Left / Right | Move the cursor through the line, Chinese and ASCII alike |
| Home / End | Jump to the start or the end of what you have typed |
| Backspace / Delete | Remove one character — a whole Chinese character, not a byte of it |
| Up / Down | Walk the prompts you already submitted **in this session** |
| Ctrl-C | Discard the unfinished line and return to `You >` |
| Ctrl-D (on an empty line) | Leave Rings cleanly (the same as `/exit`) |

Two details are deliberate:

* **Editing history is memory only.** Up/Down recall prompts from this session, and nothing is
  written to `~/.history`, `~/.rings_history` or any other file — a prompt may contain private
  data. What you actually said is kept in the conversation database; the line history is only there
  so you can retype a question.
* **Editing keys never become conversation text.** Left and Right move the cursor; they are never
  submitted as `^[[D`/`^[[C`, and nothing in the model's input carries terminal control sequences.
  After you press Enter the text is checked once more, and a line containing a NUL, an unpaired
  surrogate or a raw control sequence is refused with a sentence — no turn, no model call, no
  change to your data.

Chinese input, pasted text (`发信地址:251880385@smail.nju.edu.cn.收信地址"刘夏芸"<251880557@smail.nju.edu.cn>`
and the like), full-width punctuation, quotes, angle brackets and emoji are ordinary text: they are
neither rewritten nor rejected.

Piped input is a different path on purpose. `pw chat` with input from a pipe or a script keeps
strict, deterministic decoding and the fail-closed behaviour described above; it does not use the
interactive editor.

## What you can say

```text
加个任务，周五前交报告
我还有什么没做？
刚才那个任务是什么？
把报告优先级改高一点
报告改到周日截止
刚才那个任务做完了
周四下午两点到四点是课
每周一十点到十二点有课，记下来，再帮我安排下周
我有哪些固定安排？
把刚才那门课改成九点到十一点
以后周一没有这门课了
今天论文写了一个半小时
帮我安排一下这周
看看刚才的计划
这周还有什么空闲时间？
我的课程说明里有没有写补交规则？
最近有什么需要处理的邮件？
回复刚才那封，说我周五之前交。
确认发送
帮我查一下刚才那封邮件有没有发出去。
发个打招呼的邮件给我自己
张老师邮箱是 zhang@example.edu，记成联系人
给张老师发邮件，说我周五之前交报告
我有哪些联系人？
我今天有什么事？
记住我的办公室在仙林。
确认记住
你记得我的办公室在哪里吗？
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
asked to regenerate it. A confirmation is also bound to the *turn that proposed it*: one message
that proposes something is one **confirmation group**, and `可以` settles one group and no other.

That matters most when you change your mind before answering:

```text
You > 我周二十点到十二点有课，下午两点到四点是 ICS，晚上六点半到九点二十是中国近代史纲要
Tree > 要把这些加入固定安排吗？
       - 每周二 10:00–12:00 · 生成式软件工程（网课）
       - 每周二 14:00–16:00 · 计算机系统基础（ICS）
       - 每周二 18:30–21:20 · 中国近代史纲要（仙二-404）
       回复「可以」我就保存，或回复「取消」。
You > ics的地点是在仙一107
Tree > 要把这些加入固定安排吗？
       - 每周二 10:00–12:00 · 生成式软件工程（网课）
       - 每周二 14:00–16:00 · 计算机系统基础（ICS）（仙一107）
       - 每周二 18:30–21:20 · 中国近代史纲要（仙二-404）
       回复「可以」我就保存，或回复「取消」。
You > 可以
Tree > 已加入固定安排：
       - 每周二 10:00–12:00 · 生成式软件工程（网课）
       - 每周二 14:00–16:00 · 计算机系统基础（ICS）（仙一107）
       - 每周二 18:30–21:20 · 中国近代史纲要（仙二-404）
       从 2026-09-21 起持续到你删除。
```

The first proposal was **retired** the moment the second one replaced it: it can never be applied
afterwards, and the old `计算机系统基础（ICS）` rule without a room is never created as a side effect.
If some of the rules already existed, the answer says so in one sentence (`其中 2 条已存在，没有重复添加。`)
rather than repeating a paragraph per rule.

Two more properties of the same rule:

* if two **unrelated** groups are waiting — say a weekly schedule and a plan to apply — `可以` does
  not pick one: Tree asks which you mean and applies neither, and no model is consulted to guess;
* `取消` withdraws the outstanding questions without applying anything, so it needs no target.

Mail is never touched by any of this: only your own explicit `确认发送` sends a letter.

Among the local operations, `plan.apply_proposal` is the only one that requires a confirmation:
everything else is either a read (executes immediately) or an unambiguous local write (executes
immediately). Mail is different, and is described next.

## Fixed weekly arrangements

A class that happens every week is not an event and not a scheduled job: it is one *rule* you keep.
Tree stores it once, derives its occurrences when you look at a week, and the planner treats them as
busy time, so a plan block is never placed on top of a class.

```text
You > 每周一十点到十二点有课，记下来，再帮我安排下周
Tree > 已加入固定安排：
       每周一 10:00–12:00 · 计算机系统基础课
       从 2026-09-21 起持续到你删除。
       周计划提案 …（2 个时间块）… 要应用这个计划吗？回复「可以」我就写进计划，或回复「取消」。
You > 可以
Tree > 已应用周计划提案（…）：新增 2 个时间块，替换 0 个。
```

Four things about how this behaves are worth knowing:

* **A statement is not an instruction.** `我每周一十点到十二点有课。` only describes your week, so
  Tree asks `要把这些加入固定安排吗？` and writes nothing until you answer. `记下来`、`记录一下`、
  `加到日历`、`放进固定安排` are instructions, and are written directly.
* **One rule is one weekday.** `每周一和周三下午两点到四点都有软件工程课，记录下来。` creates two
  rules with the same title, time and timezone — not one rule with a weekday list.
* **The timezone is the runtime's.** A new rule is interpreted in `[planning].timezone`; without one,
  Tree asks which timezone you mean instead of using the machine's. Say `按东京时间` and the rule
  carries `Asia/Tokyo` itself. `TZ` and the host clock never decide what a rule means.
* **Weekly only.** Odd/even weeks, every-N-weeks, monthly or yearly recurrence and holiday
  exceptions cannot be expressed in this version, and Tree refuses them rather than approximating
  them. There is no recurrence string to write and no `rrule` to pass.

Follow-ups use the rule you last saw, and never guess between several:

```text
You > 我有哪些固定安排？
Tree > 现在有 1 条固定安排：
       - 每周一 10:00–12:00 · 计算机系统基础课
You > 把刚才那门课改成九点到十一点。
Tree > 已更新固定安排：
       每周一 09:00–11:00 · 计算机系统基础课
       从 2026-09-21 起持续到你删除。
You > 以后周一没有这门课了。
Tree > 以后周一不再有「计算机系统基础课」了。它原来的时间是 每周一 10:00–12:00；
       已经过去的工作记录和计划都没有改动。
```

Editing keeps the rule's identity and changes only its future meaning. Retiring stops future
occurrences and deletes nothing: there is no "delete a rule", and history is never rewritten.
`calendar.list` shows one-off events and the derived weekly occurrences together, so a week reads
the way you think about it.

## Mail: draft, exact preview, then one explicit send

Mail is the one capability in the conversation that can leave the machine, and it is deliberately
the slowest one to use:

### A new letter, and the three kinds of recipient

```text
You > 发个打招呼的邮件给我自己
Tree > 将要发送的邮件（以下内容就是实际发出的内容）：
       · 发件账号：smail（me@example.edu）
       · 收件人：me@example.edu
       · 主题：打个招呼
       · Message-ID：<…>
       正文：
       ---
       你好，这是一封来自 Rings 的测试邮件。
       ---
       确认发送吗？回复「确认发送」我就发送，或回复「取消」。
You > 可以
Tree > 要发送的话，请回复「确认发送」。
You > 确认发送
Tree > 已发送。
```

Who a new letter can go to is a closed list, and every other case is a question:

* **an address you wrote yourself** — `给 alice@example.com 发封邮件，主题“测试”，内容“你好”`.
  If a model proposes an address that is not in your own message, Tree refuses *before* any draft
  exists, says which address it meant, and asks you to write it yourself;
* **a contact you recorded** — `张老师邮箱是 zhang@example.edu，记成联系人`, and afterwards
  `给张老师发邮件，说我周五之前交报告`. Two contacts with one name means Tree asks which one;
  an unknown name means Tree tells you it does not know the address yet;
* **your own configured mailbox** — `发个打招呼的邮件给我自己`. With exactly one send-ready
  account that is unambiguous; with several, Tree asks which mailbox to use rather than picking the
  first.

Contacts are local records of *identity*: they are not personal facts, they never become a
`ConfirmedFact`, and they grant no permission. They exist so a name you wrote can become an address
deterministically. `我有哪些联系人？` lists them; `以后不要用这个联系人了。` retires one (nothing is
deleted, and past mail is untouched).

### A reply

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

What conversational mail still does not do is attachments, scheduled sending and automatic sending,
and it does not look a recipient up in an address book or over the network: the only sources of an
address are your own words, a stored contact and your configured mailbox.

### Which mailbox is configured is not how much mail is stored

```text
You > 你能查找哪个邮箱？QQ还是学校邮箱？
Tree > 当前配置了 1 个邮箱：
       school — student@example.edu（IMAP imap.example.edu；收信已配置、发信已配置）
       本地目前缓存了 0 封邮件。
```

The first line answers the question that was asked — which accounts exist — and the last line is a
separate fact about content. They are never merged.

## Long-term facts: proposed in a conversation, confirmed by you

A conversation is not memory. Saying something is not asking Tree to remember it forever, and Tree
never decides on its own that a passing remark is a permanent fact about you.

```text
You > 记住我的办公室在仙林
Tree > 我准备记录这条长期信息：
       - profile.office：仙林
       （来自你的话：「记住我的办公室在仙林」）
       如果要长期保存，请明确说「确认记住」；说「不要记」我就不保存。
       「可以」不会保存这条信息。
You > 可以
Tree > 要保存这条长期信息，请回复「确认记住」。
You > 确认记住
Tree > 已记住：
       - profile.office：仙林
       （来自你的话：「记住我的办公室在仙林」）
You > 你记得我的办公室在哪里吗？
Tree > profile.office：仙林
```

Four properties are worth knowing:

* **a statement is not a request.** `我的办公室在仙林` creates nothing; Tree may offer to remember
  it, and you decide;
* **the second confirmation is explicit.** `确认记住` / `记住` / `确认保存` / `确认记录` save it;
  `可以`, `好`, `嗯`, `继续` and `ok` do not. Cancelling is `不要记` / `别记` / `取消`;
* **the confirmation never touches the model.** It is parsed by code from your own message, exactly
  like `确认发送`, and it is a local write: no `ActionRequest`, no approval, no execution;
* **only confirmed facts are answers.** A question about a personal fact is answered from
  `confirmed_facts` (or with an honest "I have not confirmed that"), never from this conversation,
  from a contact, or from what a model happens to know.

A correction (`不是仙林，是鼓楼`) is a new proposal for the same key. The old value stays confirmed
until you confirm the new one, and then it is superseded rather than deleted — the history of what
you believed stays readable.

## Today, in one answer

```text
You > 我今天有什么事？
Tree > 今天（2026-09-21，Asia/Shanghai）：
       安排
       - 10:00–12:00 计算机系统基础课（每周）
       - 19:00–21:00 写 SE 实验报告
       任务
       - 交软件工程报告（10-09 截止）
       需要处理
       - 邮件：teacher@example.edu —「SE 实验三」需要回复
       等待确认
       - 1 条长期信息等待确认
```

The brief is deterministic and read-only: it is assembled from your own local state in
`[planning].timezone`, never from the conversation and never by the model. It shows at most ten
schedule entries, ten tasks, five mail items, five reminders and five waiting items, and says how
many it left out. Mail appears as sender/subject/reply-needed metadata only — never a body. A
prepared letter is shown as *waiting for your confirmation* and is not sent; a proposal is not
applied; a fact is not confirmed; and an external outcome nobody can prove is reported as
*uncertain* with an explicit "I will not retry automatically".

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
* **No recurrence this build cannot honour.** "单双周"、"每两周一次"、"每个月第一周" and "节假日除外"
  are refused with zero mutation rather than being quietly recorded as every week.
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
- A plan with several operations is fully preflighted first: if one step cannot run, none of them do,
  and Tree says which step it stopped on.
- Recurring rules ("每周一早上十点") are not a calendar feature in v1.1; Tree says so instead of
  quietly creating a single Monday.

## Implementation notes

- The conversation runtime and its boundaries: [ADR-0033](../adr/0033-tree-conversation-runtime.md)
- The typed-draft precedent it follows: [ADR-0018](../adr/0018-natural-language-command-interpretation.md)
- Grounded answers: [ADR-0019](../adr/0019-source-grounded-knowledge-answers.md)
