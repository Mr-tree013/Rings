# growing-assistant

一个会成长的个人助手：把邮件、个人资料、办事大厅和手机端连成一条可审计的闭环，并把每次成功的
流程与你的纠正沉淀成可读、可改、可测试的规则。

## 当前状态：v0.5.0（完整的可审计邮件工作流：收信 → 线程 → 分类 → 草稿 → 人工审批 → SMTP 发送 → 不确定结果对账）

已完成：

- durable event core（v0.1.0）：领域模型与状态机、SQLite 迁移、数据库级幂等去重、
  `EventInbox`、带 lease + fencing 的原子 claim、retry/backoff、crash recovery、dead letter。
- 个人知识（v0.2.0）：稳定存储身份（`local://` / `vault://`）、metadata catalog、
  文本/PDF 正文抽取与 SHA-256、per-root FTS5（trigram）索引、带 `page`/`lines` 定位的检索。
- 持续索引（v0.2.0）：`~/.config/growing-assistant/config.toml` 配置 Local/Vault roots，
  `assistantd` 周期性 reconciliation（scan → catalog → index），root 失败隔离与 supervisor。

外部输入侧只实现了收信：没有 SMTP 发送、没有 eHall/Playwright、没有 Web UI、没有手机端推送。
`assistantd` 现在托管 `index-sync`（启动时执行一次存储 reconciliation 并按配置周期重复）、
`scheduler`（durable reminder / rolling replan）、配置了账号时的 `mail-sync`，以及
**同时**配置了 mail 与可用 model 时的 `event-worker`（唯一的真实业务 handler）。没有 model 时
不启动 `event-worker`：一个连不上 provider 的 worker 只会把每封邮件 dead-letter，而停在
`RECEIVED` 的积压可以在配置好 model 后继续处理。

**唯一真实的外部副作用是「经人工批准的 SMTP 发送」**：executor 注册表只包含 `mail.send`，
且只有在该账号配置了 SMTP 时才注册。daemon 自身不会发送任何邮件——发送只能由
`pw action execute` 经过一次已消费的人工 approval 完成（eHall / browser 不存在）。

**尚未实现的加速机制**：filesystem watcher 快速路径。当前正确性来自周期 reconciliation，
因此变更检测有一个有界延迟（默认 300 秒，可配置到 10 秒）。

存储侧（Phase 2A）：Archive Vault 的**元数据 catalog** 已可用——稳定逻辑 URI、vault
manifest、不跟随 symlink 的 metadata 扫描、增量更新与安全的 missing 判定。

知识侧（Phase 2B）：**已实现** metadata catalog、文本/PDF 正文抽取、per-root FTS5（trigram）
全文索引与带 source span 的检索。

持续索引（Phase 2C）：**已实现** host config、`pw roots list`、`pw sync`，以及 `assistantd`
的周期性 reconciliation（scan → catalog → index，单 root 失败隔离）。

承诺与时间（Phase 3A）：**已实现** durable commitment model —— Task / Deadline /
CalendarEvent / PlanBlock / WorkSession 五个独立概念的持久化与结构化 CLI。

确定性周计划（Phase 3B）：**已实现** proposal-based weekly planning —— `pw plan week` 用确定性
greedy planner 生成持久、可审阅的 `PlanProposal`（不做 LLM 调用），`pw plan show` 原样展示当时
的 proposal，`pw plan apply` 是唯一写入 plan block 的路径；planner block 与 manual block 来源可
区分且可追溯，stale proposal 永不应用。**尚未实现**：自动 apply（apply 永远需要用户显式执行）。

计划执行与提醒（Phase 3C）：**已实现** durable scheduling —— `ScheduledJob` 意图写在 runtime
SQLite（daemon 重启后从 DB 恢复），deadline 的 reminder job 与 deadline/task mutation 在同一
transaction 内 materialize；提醒只投递到 durable notification inbox（`pw notifications`、
`pw notification show|read`，`pw scheduled` 可查看 job 状态）。Rolling replanning 是 debounced 的
后台 job：**只产生新的 `PENDING` proposal 并发 `PLAN_READY` 通知，绝不自动 apply**
（apply 仍需 `pw plan apply`）。daemon 现在托管 `index-sync` 与 `scheduler` 两个 service。
**尚未实现**：OS/手机推送、个人估时学习（personal effort learning）、自然语言时间解析、
重复任务/事件、embedding/向量检索、OCR、Office 文档与压缩包、filesystem watcher、eHall。

模型基础（Phase 4A）：**已实现** provider-independent model boundary —— core/application 只依赖
`ModelPort`，DeepSeek 走 Responses API 的 adapter（`[model]` 配置 + `DEEPSEEK_API_KEY` 环境变量，
key 永不写入 config/repo/log），structured output 必须在本地 `json.loads` + JSON Schema 校验
（`jsonschema`）通过后才可用，provider 的 reasoning 在 adapter 内丢弃（不返回、不落库、不打日志），
`FakeModelAdapter` 供确定性测试，`pw model status`（只读）与 `pw model test`（唯一的真实 provider
连通性检查，可能产生费用）可用。

自然语言命令预览（Phase 4B）：**已实现** `pw interpret "..."` —— 把一句话解释成**一个** typed
command draft，并打印人类可读预览与本地渲染的等价结构化命令：

```bash
pw interpret "add a task to write the SE lab report, high priority, 5 hours, due Friday"
# Interpretation: ready
#   Action: Create task
#   Title: write the SE lab report
#   ...
# No changes were made.
# Equivalent structured command:
# pw task add 'write the SE lab report' --priority high --estimate 300 --deadline 2026-10-23T15:59:00+00:00
```

**The interpreter does not execute anything.** 它只产出经过本地校验的 command draft 和等价命令，
由用户复核后自己运行；没有 `pw interpret --apply`。它会向配置的 model provider 发送请求文本，以及
**有限且明确**的 context：open task metadata（id/title/priority/estimate/deadline/updated_at，
最多 50 条）、current time、planning timezone。**task description、knowledge 内容、文件、
notification、scheduler payload 都不会发送。** 模型给出的 task UUID 必须来自本次 context，否则
直接拒绝；没有 `[planning].timezone` 时任何涉及时间的命令一律转为 clarification。
**尚未实现**：agent tool loop、read-only multi-tool composition、conversation memory、
answer persistence / FactCandidate、web search、eHall/browser、mail 回复发送。

基于来源的个人知识问答（Phase 4C）：**已实现** `pw ask` —— 只根据你自己已索引的资料回答，
每句话都必须带 citation，来源路径/页码/行号由本地解析：

```bash
pw ask "What is the deadline for my SE lab?"
# Answer
# The submission deadline is October 23 at 23:59. [S1]
#
# Sources
# [S1] local://university/notice.md - lines 18-31
# No changes were made.
```

- Answers are based only on indexed personal sources；检索是本地确定性的（问题原样先查，
  phrase 不命中时按本地关键词回退；没有模型生成 query、没有 embedding/reranking）。
- Every answer segment must cite indexed evidence；模型只能引用本次请求里给它的 `S1..Sn`，
  引用不存在的 id 会被本地拒绝（不会展示成功答案）。
- Source paths/page/line references are resolved locally；模型永远不会输出 URI/页码/行号。
- Retrieved excerpts are sent to the configured model provider（会发送问题 + 有上限的正文片段，
  可能产生费用；每条 chunk 最多 4000 字符、总预算 24000 字符）。task description、WorkSession、
  Calendar、Notification、Scheduler payload、文件绝对路径都不会发送。
- Offline archive content cannot be used until connected；offline root 会在输出里提示，不会被
  当作证据。
- `pw ask` is read-only；它不修改文件、不写 index、不创建任何 durable state，也不执行命令。
- `pw search` 仍然是不调用模型的本地搜索。
**尚未实现**：agent tool loop、read-only multi-tool composition、conversation memory、
answer persistence / FactCandidate、web search、eHall/browser。

收信（Phase 5A）：**已实现** durable IMAP inbound sync —— 配置 `[[mail.accounts]]`（host/port/
username/mailbox，仅 TLS），凭据只从环境变量读取：

```bash
export GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD='...'
pw mail sync              # 显式连接 IMAP 服务器并同步（会联网）
pw mail accounts          # 只读：账号与凭据是否存在
pw mail status            # 只读：UIDVALIDITY / cursor / 已存邮件数
pw mail messages          # 只读：已存邮件列表（不打印正文）
pw mail show MESSAGE      # 只读：单封邮件详情
```

要点：

- 邮件位置身份是 `(account, mailbox, uidvalidity, uid)`，**UID 从不单独作为持久身份**；服务器
  重建邮箱（UIDVALIDITY 变化）时执行 bounded reconciliation，能确认是同一封时复用稳定 UUID，
  确认不了（例如同一 Message-ID 但内容不同）就新建并记录 conflict。
- cursor 只推进到**本批次已 durable 处理**的 UID；一批（messages + locations + attachments +
  cursor）在一个事务里完成，失败整体回滚，所以崩溃只会重放、不会跳过。
- 原始 `.eml` 按 SHA-256 内容寻址保存在 runtime data 目录（`~/.local/share/growing-assistant/
  mail/raw/...`），不进 repo、不进 vault、不进 cache；附件只保存 metadata（文件名/类型/大小/
  SHA-256），不落地、不执行。
- 每封邮件最终都会 bridge 成恰好一个 `InboundEvent`（`source=mail:<account>`、
  `event_type=mail.message.received`、`external_id=message:<UUID>`）；event 里只有 message UUID
  和 account id，**不含正文/标题/附件**。两个 crash 窗口都会在后续 sync 中修复。
- 只读邮箱：`select(readonly=True)` + `BODY.PEEK`，同步不会把邮件标记为已读。
邮件的确定性线程与结构化分类（Phase 5B）：**已实现** deterministic threading + structured
analysis。

```bash
pw mail threads              # 只读：确定性 thread 列表
pw mail thread show THREAD   # 只读：一个 thread 的邮件（对话顺序 + link 状态）
pw mail analysis MESSAGE     # 只读：已存储的分析结果（不调用模型）
```

要点：

- **thread 由代码决定，模型不参与**：parent 只按 `In-Reply-To`，然后 `References`（由近到远）
  解析，且必须**同 account** 且只匹配到**唯一一条**已存储邮件；重复 Message-ID 一律
  `AMBIGUOUS`（自成 thread，绝不任选），找不到 parent 是 `UNRESOLVED`，没有引用 header 是
  `ROOT`。membership 只决定一次且幂等，迟到的 parent 不会改写历史。
- **分类结果只是候选**：每个 message 最多一条 durable `MailAnalysis`（category / requires_reply /
  summary / action_candidates）。`DEADLINE`（by when）与 `EVENT_START`（when it happens）是
  **不同取值**，绝不合并；带时间的候选必须给出邮件原文时间或 instant，instant 必须带时区偏移
  （naive 一律被本地校验拒绝）。
- **模型只看到有上限的不可信数据**：当前邮件 + 同一 thread 最多 5 条历史，每条最多 3000 字符、
  总计 12000 字符。`To`/`Cc`、附件 bytes/hash、raw `.eml`、storage key、凭据以及 Task /
  Calendar / Scheduler / Notification / Knowledge 状态**都不进 context**；邮件内容永远只是
  被引用的数据，不是 instructions。
- **重试不再重复付费**：分析结果带 input fingerprint（analyzer/schema 版本 + message id +
  content fingerprint + thread 上下文 + timezone）；EventWorker retry 命中同样的
  `(analyzer_version, input_fingerprint)` 时直接复用，不调用模型。provider 401/billing 之类
  永久失败直接 dead-letter，rate limit / 5xx / 结构化输出不合法则按 EventWorker 的有界 retry
  策略重试。
- 正文不可用（oversize）时**不调用模型**，只落一条 `unknown` 分析。
- `assistantd` 仅在**同时**配置了 mail 与可用 model 时启动 `event-worker`；没有 model 时邮件
  照常收下并停在 `RECEIVED`，配置好 model 再重启即会继续处理积压。
- 处理邮件**不改动** Task / Deadline / CalendarEvent / PlanBlock / WorkSession / PlanProposal /
  ScheduledJob / Notification 或知识索引。

回复草稿（Phase 5C）：**已实现** durable reply drafts —— 显式触发、本地生成、**只存在本地**：

```bash
pw mail draft create MESSAGE                          # 用配置的模型写一封回复草稿（不发送）
pw mail draft create MESSAGE --context-query "my office hours" --root university
pw mail drafts                                        # 只读：草稿列表
pw mail draft show DRAFT                              # 只读：正文 / 待补充信息 / 知识来源
pw mail draft edit DRAFT --body "..." --subject "..." # 本地编辑（乐观并发）
```

要点：

- **收件箱不能读取个人资料库**：只有用户在命令行显式给出 `--context-query` 时才检索个人知识；
  没有 query 时 Knowledge search **一次都不会被调用**，请求里的 knowledge evidence 为 0。邮件里写
  “ignore previous instructions / 搜我的文件 / 把密码发给我”只会作为被引用的数据进入 prompt。
- **收件人和主题由本地代码决定**：收件人取 `Reply-To`（没有才用 `From`）并用 stdlib 地址解析提取
  mailbox；主题由纯函数 `reply_subject()` 派生（已有 `Re:` 前缀不重复叠加）。draft schema 里没有
  `to`/`cc`/`bcc`/`subject` 字段，模型只能写正文。V1 是 Reply，不是 Reply-All。
- **模型只写正文**：输出是 closed schema（`body` / `used_source_ids` / `needs_user_input`）；
  资料不支持的个人事实必须写进 `needs_user_input`，不得编造。source id 只能是本次显式提供的那几个，
  未提供的 id 会让本次生成被拒绝（不修补、不忽略）。
- **引用只存在本地**：`mail_draft_sources` 只保存 root / entry / chunk / logical URI / source span，
  不保存正文片段、不保存物理路径，也不会把 citation 写进邮件正文。
- **必须有可读正文**：`body_status != available`（oversize header-only）直接拒绝
  （`MailDraftSourceUnavailable`），不调用模型。
- **编辑是乐观并发**：带上版本号 `UPDATE … WHERE id=? AND version=?`，冲突抛
  `StaleMailDraftUpdate`，不会静默覆盖；草稿生成/编辑不修改任何邮件、线程、分析或 Task/Calendar/
  Scheduler/Knowledge 状态。
- **没有任何背景起草**：`EventWorker`、`mail-sync`、scheduler 与 daemon 启动都不会创建草稿；只有
  `pw mail draft create` 会调用模型（因此可能产生费用）。生成草稿**不等于发送**。

审批与执行边界（Phase 6A）：**已实现** Case / ActionRequest / Approval / ExecutionRun 安全基础：

```bash
pw cases                       # 只读：case 列表
pw case add "报名课程"          # 建一个多步骤事务容器
pw case show CASE              # 只读：容器 + 里面准备好的 action
pw case done CASE / cancel CASE

pw actions                     # 只读：action 列表（含 approval / execution 状态）
pw action show ACTION          # 只读：exact payload + fingerprint + 当前状态
pw action challenge ACTION     # 生成一次性 approval token（只显示一次）
pw action approve ACTION TOKEN # 人工 approve 这个 exact action
pw action execute ACTION       # 执行（Phase 6A production capability set 为空）
pw action cancel ACTION        # 取消，使其永远不能被执行
```

要点：

- **每个外部副作用都必须由人 approve，且绑定到 exact fingerprint**：`Approval` 绑定
  `action_id + action_fingerprint`（= SHA256(canonical JSON payload)）。payload 创建后不可修改，
  内容一变就是新的 `ActionRequest`，旧 approval 自动失效。执行前会重新 hash payload 校验，
  绝不只信数据库里的 fingerprint 字段。
- **token 只用一次、寿命 10 分钟、只存 hash**：challenge token 是 256-bit 随机串，DB 只保存
  `sha256(token)`；明文只在 `pw action challenge` 输出一次，不会写日志、不会在其它命令再显示、
  错误信息也不会回显。approve 是 single-use，expired/已消费/错误 token 一律拒绝。
- **同一 action 同时只能有一个有效 approval**：DB partial unique index +
  service 双重保证；approve 后再 approve 会被拒绝（先等待过期，过期后被 supersede 并保留历史）。
- **执行先消费 approval，再调用 executor，二者同一 transaction**：任何时刻都只有一个调用者能
  消费成功，并发执行只会真正执行一次。`FAILED` 也会花掉 approval（想重试必须重新人工 approve）。
- **不确定的结果绝不自动重试**：`UNKNOWN`（外部结果不可判定）与崩溃留下的 `RUNNING` 会阻塞同一
  action 的再次执行，直到未来的 executor-specific reconciliation；`CancelledError` 直接传播，
  不会被悄悄标成失败。
- **capability 集合默认为空**：Phase 6A 不注册任何真实 executor，所以 `pw action execute` 对任何
  action 都返回 `CapabilityUnavailable`，并且**不消费 approval、不创建 execution run**。没有
  generic shell/browser/HTTP executor，高风险能力是「实现不存在」而不是「prompt 禁止」。
- **模型与自动化无法 approve**：Interpreter / GroundedAnswer / MailAnalysis / MailEventHandler /
  MailDraftService / EventWorker / Scheduler 都没有创建 Approval 的代码路径；`pw action` 也没有
  `--force`/`--approve-all`，并且没有 `pw action create`（ActionRequest 只能由 typed factory 或
  应用 API 准备）。

已审批的 SMTP 发送（Phase 6B）：**已实现** 从草稿到一个被批准、可审计的 outbound message：

```bash
# 1. 先有一封草稿（可能带未解决的疑问）
pw mail draft create MESSAGE
pw mail draft show DRAFT
pw mail draft edit DRAFT --body "..."
pw mail draft acknowledge DRAFT        # 有 open questions 时必须先 acknowledge（编辑后需重新 acknowledge）

# 2. 把某个 draft version 冻结成一个 exact action
pw case add "回复导师"
pw mail send prepare DRAFT --case CASE

# 3. 人工审批链（与 Phase 6A 完全相同，没有第二套 approval 命令）
pw action show ACTION
pw action challenge ACTION
pw action approve ACTION TOKEN
pw action execute ACTION

# 4. 只在结果不确定时，用 Sent 邮箱确认
pw mail send show ACTION
pw mail send reconcile ACTION
```

要点：

- **批准的就是发出的**：`prepare` 把该 draft version 的 From / To / Subject / body / Date /
  Message-ID / reply headers 冻结成 immutable `ActionRequest` payload，指纹覆盖全部字段；
  执行时发送的字节只能来自这个 payload（绝不重新读取 draft）。之后编辑草稿只影响新 action，
  旧 approval 永不转移；`pw mail send show` 会明确提示
  “Prepared from draft version: N / Current draft version: M / WARNING: draft changed”。
- **一个 draft version 只能有一个 send action**：`mail_send_links` 的
  `UNIQUE(draft_id, draft_version)` 与 `UNIQUE(rfc_message_id)` 在 DB 层保证；想重新 prepare
  必须先 edit/acknowledge 推进版本。
- **Message-ID 在 approve 之前就已确定**：`<128-bit random>@<sender domain>`，同时出现在
  payload、fingerprint、真正的 SMTP 字节和 Sent 搜索里，重试/对账时绝不重新生成。
- **TLS-only、凭据只在环境变量**：`smtp_security` 只接受 `starttls` / `ssl`（没有 plain、
  没有 skip verification）；SMTP 密码是 `GROWING_ASSISTANT_MAIL_<ID>_SMTP_PASSWORD`
  （与 IMAP 密码分开），不写库、不写日志、不进 payload。
- **能力检查发生在消费 approval 之前**：executor 的 `supports()` 是纯 offline 检查
  （账号配置 + 凭据 + payload 可读）。缺凭据时 `pw action execute` 返回
  `CapabilityUnavailable`，**approval 不被消费、不创建 execution run**。
  prepare 本身不需要凭据，所以可以先准备、审阅、批准，再补凭据。
- **明确的失败 vs 不确定**：SMTP 阶段被显式跟踪。认证失败 / 发件人被拒 / 收件人全被拒 /
  DATA 被明确拒收 → `FAILED`（approval 已消费，重试需重新人工 approve）；DATA 之后连接断开、
  超时或协议不明 → `UNKNOWN`，且**永不自动重发**。
- **Sent 对账是只读的三值判断**：只搜配置的 Sent mailbox 里那个 exact Message-ID，并对候选项
  做精确 header 比较（服务端返回 `<x@example>` 而我们要 `<x@example.evil>` 不算命中）。
  `FOUND` 可以把 `RUNNING`/`UNKNOWN` 提升为 `SUCCEEDED`（同一 transaction 内 run / action /
  审计行）；`NOT_FOUND` **不代表没发出去**；`AMBIGUOUS` 不任选 UID；`UNAVAILABLE` 保持原状。
  历史对账记录永不删除，而且没有任何 resend 命令。
- **没有任何后台发送路径**：daemon / EventWorker / Scheduler / MailSync / MailAnalysis /
  MailEventHandler / MailDraftService / Interpreter / GroundedAnswer 都没有 SMTP 能力，
  `smtplib` 只出现在 `adapters/mail/smtp.py`；发送只能经 `ActionExecutionService` +
  一次已消费的人工 approval。

**尚未实现**：eHall executor、browser executor、mobile approval UI、正文索引/问答（把邮件正文
送进 knowledge index）、thread 回溯修复、分类结果自动转 Task/Case、事件删除同步
（server-side deletion）、attachment materialization、QQ 与站点 watcher。
明确边界：**model 不能直接修改 task、文件、scheduler 状态或任何外部服务**；它只能产出文本，
是否可用由本地 deterministic validation 决定。

## Architecture summary

Architecture v1 是 **Modular Monolith + asyncio daemon + explicit state machines + SQLite**：

```text
smail
  ↓  IMAP incremental fetch (UIDVALIDITY + UID)
MailMessage → deterministic thread → InboundEvent → MailAnalysis (candidates only)
  ↓  pw mail draft create（显式；可选 --context-query 才读个人知识）
MailDraft（本地草稿；不发送）
  ↓  (后续 Phase：Case → knowledge search → material checklist)
  → pw mail send prepare → ActionRequest（immutable + Message-ID + fingerprint）
  → Approval（人类、exact fingerprint、single-use）→ SMTP over TLS（FAILED / SUCCEEDED / UNKNOWN）
  → pw mail send reconcile（只读 Sent 对账，永不重发）
  → result → archive → PlaybookCandidate → review/test → Playbook
```

进程模型：一个长期运行的 `assistantd`（Python 3.13 + asyncio），未来内部承载四类长期服务，
每类都有独立异常边界，单个服务失败不得拖垮整个 daemon。另有 CLI `pw`。Windows Task Scheduler
未来只在登录时负责拉起 WSL 内的 `assistantd`。

四条不可破坏的安全约束（详见 spec 与 ADR）：

1. 幂等由代码与持久状态保证，模型不参与幂等判断。
2. 任何外部副作用必须绑定具体 `ActionRequest` 的人工 `Approval`（approval 绑定 action
   fingerprint，内容变化即失效；runtime 没有自行批准的代码路径）。Phase 6A 已实现该边界：
   approval 只能由 `pw action approve` 用一次性 token 创建，执行前重新校验 fingerprint，
   且 production executor 能力集为空（ADR-0023）。
3. 事实区分 `FactCandidate` 与 `ConfirmedFact`；自动填表只允许使用已确认、未过期、来源可追溯、
   且被目标字段许可的事实。
4. 高风险能力（退课、撤销申请、退宿等）在代码能力集合中物理不存在，不靠 prompt 禁止。

运行时存储：标准库 `sqlite3` 直连，SQL 全部收在 `store/` 包内，领域与应用层不执行 SQL、
不 import 数据库驱动（ADR-0003、ADR-0008）。Repository 端口是 async 的，阻塞的 sqlite3
调用只在 worker thread 内执行，connection 不跨线程（ADR-0009）。

外部输入统一经 `EventInbox` 摄取为持久化的 `RECEIVED` 事件：重复的 `(source, external_id)`
是幂等成功（返回 `DUPLICATE`）而不是错误。`EventWorker` 负责 claim / retry / crash recovery /
dead letter；它现在注册了唯一的真实 handler（`mail.message.received` → `MailInboundEventHandler`），
并且只在 mail 与 model 同时可用时由 daemon 启动。未知 event type 永久拒绝，不会静默处理。

## Installation

需要 Python 3.13 与 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
```

## Running the CLI

```bash
uv run pw --help
uv run pw status
uv run pw doctor
```

## Running the daemon

```bash
uv run assistantd      # 启动后等待信号；Ctrl+C 优雅退出
```

## Development commands

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

## 数据目录边界

仓库只存代码、测试、文档、规则、playbook、evals、迁移、prompt 与示例配置。运行态与个人数据
一律放在仓库之外：

```text
~/.local/share/growing-assistant/   运行状态（SQLite、游标、作业、审计）
~/.config/growing-assistant/        配置
~/.cache/growing-assistant/         缓存
~/personal-vault/                   个人 Vault（facts/ inbox/ attachments/ archive/）
```

个人 Vault 与代码仓库**物理分离**；U 盘等移动存储属于 archive 存储，不是 Agent runtime。
文档身份是稳定逻辑 URI，而不是挂载路径：

```text
local://<root-id>/<relative-path>     本机目录（root id 由调用方显式指定）
vault://<vault-id>/<relative-path>    Archive Vault（id 来自 .pa/vault.toml）
```

```bash
uv run pw vault init /mnt/e/archive --id archive-main --label "Personal Archive"
uv run pw vault status /mnt/e/archive
uv run pw vault scan /mnt/e/archive
uv run pw reindex --root archive-main
uv run pw search "important deadline"
```

搜索结果是检索结果，不是 AI 答案：content hit 一定带 `page N` 或 `lines A-B`，
文件名/路径命中单独标为 `[metadata]`，离线 root 会被明确列出而不是静默忽略。

### 配置与持续同步

`~/.config/growing-assistant/config.toml`（示例见 `docs/examples/config.toml`）：

```toml
format_version = 1

[indexing]
interval_seconds = 300
run_on_startup = true

[[storage.roots]]
kind = "local"
id = "university"
label = "University documents"
path = "/home/user/Documents/University"

[[storage.roots]]
kind = "vault"
id = "archive-main"
path = "/mnt/e/archive-vault"
```

```bash
uv run pw roots list          # 只读配置，不扫描任何目录
uv run pw sync               # 手动跑一次 reconciliation（daemon 每周期做同样的事）
uv run pw sync --root archive-main --force-index
uv run assistantd            # 启动后立即同步一次，然后按 interval 周期重复
```

同一 root 的同步在单进程内串行；一个 root 离线、身份不符或索引损坏都不会阻止其他 root 同步。
扫描不完整时只更新已看到的 metadata，**不会**重跑知识索引，因此临时权限问题不会抹掉可搜索内容。

### 任务、日历、计划与实际工作

五个概念严格分开：`Task`（要做的事）、`Deadline`（最晚完成时间）、`CalendarEvent`
（已被占用的时间）、`PlanBlock`（计划用来做某个 Task 的时间）、`WorkSession`（实际做了多久）。
**Deadline 不占用时间，PlanBlock 不等于实际耗时。**

```bash
uv run pw task add "Write SE lab report" --estimate 300 --priority high \
    --deadline 2026-10-20T23:59:00+08:00
uv run pw tasks                     # 默认只列 OPEN；--all 包含已完成/已取消
uv run pw task show <id-or-prefix>  # 含 deadline、plan blocks、work sessions
uv run pw task done <id>            # 完成时会原子取消未结束的 plan block
uv run pw task deadline <id> --clear

uv run pw calendar add "SE lecture" --start 2026-09-21T10:00:00+08:00 \
    --end 2026-09-21T12:00:00+08:00
uv run pw calendar --days 30        # 未来 30 天的繁忙时间（区分 event / plan）

uv run pw plan add <task-id> --start 2026-09-23T19:00:00+08:00 \
    --end 2026-09-23T21:00:00+08:00
uv run pw work add <task-id> --start 2026-09-22T20:00:00+08:00 \
    --end 2026-09-22T21:00:00+08:00
uv run pw work list <task-id>       # 实际投入时间（精确秒数）
```

时间参数只接受带时区偏移的 ISO 8601；`tomorrow`、`next Friday` 这类输入会被拒绝
（那属于未来的自然语言解释器，不属于 CLI）。ID 参数接受完整 UUID 或唯一前缀，匹配多个时拒绝猜测。

## Repository layout

```text
src/assistant/{domain,application,ports,store,adapters}/   分层包（domain/ports/store 已实现，adapters 仍为空）
docs/specs/                                                架构设计文档
docs/adr/                                                  架构决策记录
rules/ playbooks/ memory/ evals/ prompts/ migrations/      规则、流程、记忆、评测、提示词、迁移
tests/{unit,integration,contract,regression,e2e}/          测试分层
```
