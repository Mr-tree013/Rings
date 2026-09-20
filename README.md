# growing-assistant

一个会成长的个人助手：把邮件、个人资料、办事大厅和手机端连成一条可审计的闭环，并把每次成功的
流程与你的纠正沉淀成可读、可改、可测试的规则。

## 当前状态：v0.8.0（邮件 + eHall + 手机控制面 + 人工确认的事实与 Playbook + 观察 + 受控 MCP）

已完成：

- durable event core（v0.1.0）：领域模型与状态机、SQLite 迁移、数据库级幂等去重、
  `EventInbox`、带 lease + fencing 的原子 claim、retry/backoff、crash recovery、dead letter。
- 个人知识（v0.2.0）：稳定存储身份（`local://` / `vault://`）、metadata catalog、
  文本/PDF 正文抽取与 SHA-256、per-root FTS5（trigram）索引、带 `page`/`lines` 定位的检索。
- 持续索引（v0.2.0）：`~/.config/growing-assistant/config.toml` 配置 Local/Vault roots，
  `assistantd` 周期性 reconciliation（scan → catalog → index），root 失败隔离与 supervisor。
- 运行态加固（仍是 0.8.0，Phase 9A）：`pw integrity check` 只读跨域体检、`pw backup create|verify|
  inspect` 一致备份归档（`.gab`）、`pw backup restore … --to DIR` 只做 staging 恢复并失效恢复前的
  授权能力。详见「完整性检查、备份与恢复」。

外部输入侧只实现了收信：没有公网服务、没有 cloud relay、没有手机推送。`assistantd` 现在托管
`index-sync`（启动时执行一次存储 reconciliation 并按配置周期重复）、`scheduler`（durable
reminder / rolling replan）、配置了账号时的 `mail-sync`、**同时**配置了 mail 与可用 model 时的
`event-worker`（唯一的真实业务 handler），以及 `[mobile] enabled = true` 时的 `mobile-web`
（同一局域网手机控制面）。没有 model 时不启动 `event-worker`：一个连不上 provider 的 worker
只会把每封邮件 dead-letter，而停在 `RECEIVED` 的积压可以在配置好 model 后继续处理。

**真实外部副作用只有两个，且都必须经过人工批准**：`mail.send`（配置了 SMTP 时注册）与
`ehall.submit-certificate`（`[ehall] enabled = true` 时注册）。daemon 自己既不发邮件也不开
浏览器：两者都只能由 `pw action execute` 在消费一次人工 approval 之后执行。

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

南京大学 eHall「证明书申请」（Phase 6C）：**已实现** 第一个白名单 eHall 事务 —— 只有一个专用
pipeline，不是通用浏览器 agent：

```bash
# 一次性：安装浏览器运行时（不联网下载模型，只装 Chromium）
uv run playwright install chromium

# 1. 手动登录（本项目永远不接触学校密码）
pw ehall login

# 2. 只读查看当前表单契约（不改动网页）
pw ehall certificate inspect

# 3. 用显式值准备一个 exact action（此时不在网页里填任何字段）
pw case add "申请在读证明"
pw ehall certificate prepare --case CASE \
  --field applicant-name=张同学 \
  --field certificate-type=在读证明

# 4. 复核 + 既有审批链（与 Phase 6A/6B 完全相同）
pw ehall certificate show ACTION
pw action show ACTION
pw action challenge ACTION
pw action approve ACTION TOKEN
pw action execute ACTION
```

要点：

- **只有一个能力**：action type 固定为 `ehall.submit-certificate`，service 固定为「证明书申请」。
  `pw ehall` 没有 `submit` / `click` / `open` / `fill`，也没有 service URL、selector 之类的参数；
  退课、撤销申请、删除、退宿等高风险能力在代码里**不存在**，不是靠 prompt 禁止。
- **登录是人的事**：`pw ehall login` 打开 headed Chromium 停在 eHall 首页，SSO/MFA 由用户自己完成；
  代码里没有填用户名/密码的路径，config 里也没有 `username`/`password` 字段，cookie 只保存在
  `$XDG_DATA_HOME/growing-assistant/ehall/nju-profile/`（0700，不进 repo/cache）。
- **顶层导航白名单**：只允许 `ehall.nju.edu.cn` / `ehallapp.nju.edu.cn` / `authserver.nju.edu.cn`
  （HTTPS）；其它顶层跳转一律 `EHallUnexpectedOrigin` 失败关闭。子资源/CDN 不受限。
- **prepare 不填表**：inspect 后只在本地校验（字段是否存在、必填是否齐全、select/radio 是否命中
  允许项），然后把 service identity、有序字段定义、materials、submit control 与用户填的值冻结成
  immutable `ActionRequest`。所有值只来自 `--field KEY=VALUE`，没有知识库/邮件/模型自动填充。
- **page contract 一变就失效**：fingerprint 覆盖 service、page markers、有序字段定义与选项、
  required materials、submit control。执行前重新 inspect，fingerprint 不一致 → `EHallPageChanged`，
  **一个字符都不会输入**，也不会提交。
- **提交只发生一次，且必须有审批**：执行流程是「重新校验 contract → 精确填写 → 逐字段 readback
  比对 → 点击唯一白名单 submit」。click 之前的任何失败（页面变了、session 过期、readback 不一致）
  都是明确 `FAILED`；click 之后无法判定则是 `UNKNOWN`，会阻塞同一 action 的再次执行，CLI 提示
  去 eHall 人工确认，**绝不自动重发**。
- **没有任何后台浏览器**：daemon 里没有 `ehall-worker`，EventWorker / Scheduler / MailSync /
  MailAnalysis / MailHandler / Interpreter / GroundedAnswer 都无法 import eHall adapter；
  浏览器只在用户显式运行 CLI 时启动，且 `pw doctor` 会检查 playwright 与 Chromium 是否就绪。
- **NJU 可能改版**：portal 的标题、字段与页面结构都可能变化。page contract 不匹配会在提交前
  停下（fail closed），而不是自动适配；本阶段的 selector 不是「永远稳定」的承诺。

同一局域网手机 Web 控制面（Phase 6D）：**已实现** 一个默认关闭的手机端界面 —— 手机可以查看进度、
建/完成任务、改草稿、审批一个 exact action，**但不能执行任何动作**：

```toml
# ~/.config/growing-assistant/config.toml
[mobile]
enabled = true
bind = "lan"      # loopback | lan
port = 8765
```

```bash
assistantd            # 托管 mobile-web 服务（与其它 service 相互隔离）
pw mobile pair        # 打印一次性配对码（只显示一次，只存 hash）
# 手机浏览器打开 http://<本机局域网 IP>:8765/pair，手工粘贴配对码
```

要点：

- **只在同一受信局域网**：`bind = "lan"` 监听 `0.0.0.0`，真正把关的是按 **socket peer** 判断的
  private-client middleware（loopback / private / link-local 放行，其它一律 403）。
  `X-Forwarded-For` / `Forwarded` / `X-Real-IP` **不被信任**：伪造 header 不能绕过。
  V1 没有 HTTPS、没有 cloud relay、没有 VPN、没有第三方登录；cookie 也**不**谎称 `Secure`。
- **秘密只以 hash 存在**：配对码一次性、10 分钟有效；session 30 天且可 `pw mobile revoke` 撤销。
  数据库里只有 SHA-256（带 `CHECK` 约束），明文只出现在创建它的那一次响应里。配对码由
  `pw mobile pair` 打印一次，**不会**放进 URL；`pw mobile approval-link ACTION` 的 token 放在
  URL **fragment**（`#token=...`，不发给服务器），页面加载后立刻从地址栏抹掉。
  服务器默认 `access_log=False`：token、cookie、邮件正文和 action payload 都不进日志。
- **每个写操作都要三件套**：session cookie（HttpOnly）+ `X-CSRF-Token` header + CSRF cookie
  三者一致才允许 mutation；所有响应带 CSP / `Referrer-Policy: no-referrer` / `nosniff` /
  `X-Frame-Options: DENY` / `Cache-Control: no-store`，`/docs`、`/redoc`、`/openapi.json` 关闭。
- **手机端没有执行入口**：审批复用 `ApprovalService`（仍是 exact fingerprint 绑定），approve
  成功即停止；`adapters/web/` 不能 import `ActionExecutionService`，路由表里不存在
  execute / send / submit / retry / resend，也没有 generic action-creation endpoint。
  实际执行仍只能在 host 上显式运行 `pw action execute ACTION`。
- **没有模型参与**：domain/application/web 这条路径不 import `ModelPort` / `StructuredModel` /
  Interpreter / GroundedAnswer；task 用结构化字段创建（没有自然语言解释），draft 编辑沿用
  Phase 5C 的 optimistic concurrency（版本过期 → `409` 并返回最新版本）。
- **界面是自包含的**：静态 HTML + CSS + 少量 vanilla JS，没有构建步骤、没有 CDN、没有任何
  第三方 JS/CSS/font；所有用户内容（task 标题、邮件主题/正文、action payload）一律经
  `textContent` 写入 DOM，`innerHTML` / `eval` / `new Function` 在静态资源里不存在。
- **daemon 里只是多一个被隔离的 service**：`mobile-web` 与 `index-sync` / `scheduler` 同级，
  crash 会被 supervisor 重启且不影响其它 service，stop event 会干净关闭 Uvicorn。

人工确认的个人事实（Phase 7A）：**已实现** 一个「先提案、后人工确认」的个人事实库 ——
助手可以记住事实，但没有任何模型或后台进程能替用户决定记住了什么：

```bash
# 1. 用一句自己的话提案（candidate = 提案，还不是事实）
pw fact candidate add profile.office "Room 302" \
  --note "我的办公室在计算机系楼 302"
# 可选：--valid-until 2026-12-31T00:00:00+08:00（必须是带时区的 ISO 8601）

# 2. 复核提案（会显示你写的那句话作为来源）
pw fact candidate show 2e5ae8e5
pw fact candidates            # 默认只看 pending；--all 看全部

# 3. 人工确认（唯一能让事实被信任的一步）或拒绝（保留为审计记录）
pw fact candidate confirm 2e5ae8e5
pw fact candidate reject 2e5ae8e5

# 4. 查看已确认事实（默认只看 active 且未过期；--all 含 expired / superseded 历史）
pw facts
pw fact show d37ee6eb
pw corrections                # 你说过的原话（audit）
```

要点：

- **candidate ≠ fact**：`FactCandidate` 只是提案，不能用于任何自动填写、不进 model context、
  不被任何消费者读取；`ConfirmedFact` 才可能在**未来**被消费，且必须满足
  `superseded_at IS NULL AND (valid_until IS NULL OR valid_until > now)`。
- **只有人能确认**：确认入口只有 `pw fact candidate confirm`（CLI → `LearningService`）。
  Interpreter / GroundedAnswer / MailAnalysis / EventWorker / Scheduler / MailDraftService /
  mobile web 都没有创建或确认事实的代码路径；确认**没有** `--force`、`--edit-value`、
  `--confirm-all`：被批准的值必须是你看到的值。
- **provenance 必须存在**：每个 candidate 都引用产生它的 `Correction`（你写的原话，只 trim 不改写），
  candidate + correction 在同一个 transaction 里写入；history 是 append-only，没有
  `correction delete` / `fact delete`。
- **带来源的历史，不覆盖**：同一个 key 确认新值时，旧行在同一 transaction 内被 `superseded`
  （不删除），数据库 partial unique index 保证任一提交状态下最多一个 current fact；
  确认中途失败会整体 rollback（旧事实仍 current、candidate 仍 pending）。
- **过期是读取期状态**：`--valid-until` 到点后该事实不再 active，但没有后台 job、没有状态需要同步；
  已过期但仍 current 的行会在下一次确认时被正确 retire。
- **这个库不是密码库**：key 中出现 `password` / `secret` / `token` / `credential` /
  `api_key` / `private_key` 等完整词段（含 `mail.smtp_token` 这类）一律 `ForbiddenFactKey`；
  项目**不会**去猜 value「像不像密码」。
- **Confirmed facts are not yet automatically injected into models, mail drafts, or eHall forms.**
  本阶段事实只被 store / review / query：eHall `--field` 仍然必须显式输入，mail draft 不会自动注入，
  Interpreter 与 GroundedAnswer 的 context 也没有变化（有 architecture test 锁定）。

已复核的 Playbook（Phase 7B）：**已实现** 「一次成功 → 人工命名候选 → 无副作用 dry-run → 人工晋升」
的引用蓝图 —— 它记录**什么曾经成功**，但**不能重放、不能执行、不能参数化**：

```bash
# 1. 指向一次已经明确成功的执行（action 必须是 EXECUTED，run 必须是 SUCCEEDED）
pw playbook candidate add <ACTION> \
  --name "Approved certificate workflow" \
  --note "Successful run reviewed by me"

# 2. 复核候选：来源 action / run / fingerprint 与历史 dry-run
pw playbook candidate show <CANDIDATE>

# 3. dry-run：只让当前本地 parser 重新读一遍那份被批准的 payload
pw playbook candidate test <CANDIDATE>

# 4. 人工晋升（要求当前 contract version 下有 PASS；没有 --force / --skip-test）
pw playbook candidate promote <CANDIDATE>

# 5. 查看 / 退休（退休只是不再引用，历史永存）
pw playbooks
pw playbook show <PLAYBOOK>
pw playbook retire <PLAYBOOK>
```

要点：

- **成功不会自动学习**：一次 `mail.send` / `ehall.submit-certificate` 成功执行之后，
  `playbook_candidates` 数量**不变**，直到用户显式 `pw playbook candidate add`；
  一个成功的 action 最多产生**一个**候选（DB `UNIQUE(source_action_id)`），拒绝也不是重建的理由。
- **来源必须是明确成功**：创建候选时会重新读取 action 与 run、重新 hash payload，要求
  `ActionRequest.status == EXECUTED`、`ExecutionRun.status == SUCCEEDED`、`finished_at` 存在；
  `FAILED` / `UNKNOWN` / `RUNNING` / 从未执行都会被拒绝（`PlaybookSourceNotEligible`）。
- **dry-run 不接触任何外部系统**：`mail.send` 只重新运行 `MailSendPayload` 的严格 parser，
  `ehall.submit-certificate` 只重新运行证书 payload parser。**不读 SMTP/IMAP 凭据、不发信、不开浏览器、
  不查 Sent、不生成新 Message-ID、不调用 `ActionExecutor`、不创建 `Approval` 或 `ExecutionRun`**；
  即使 SMTP 密码缺失、eHall 关闭、Playwright 未安装，dry-run 依然可以 PASS（有测试与 spy 锁定）。
- **PASS 的含义很窄**：它只证明「当前代码仍然理解这份 historical payload」。
  它**不**证明凭据仍然有效、远端页面没有变化、收件人仍然正确，也不证明这个动作值得再做一次。
- **晋升需要当前版本的 PASS**：promote 在同一个 transaction 内要求候选仍是 pending、且存在
  **当前 replay contract version + 精确 input fingerprint** 的 PASSED test（否则
  `PlaybookCandidateNotTested`）。parser 语义变化时递增 contract version，旧 PASS 自动失效，
  旧 Playbook 保留它当时晋升所用的版本，不自动重测、不自动退休。
- **Playbook 不携带任何能力**：它不含 Approval、不创建 `ActionRequest`、不绕过审批，
  CLI 里没有 `pw playbook run|execute|apply|instantiate`，代码里没有 `PlaybookExecutor`，
  也没有 `${...}` / `{{...}}` 参数化模板 —— 实例化与参数推断是后续 Phase 的事。
- **审计永远保留**：被拒绝的候选与被退休的 Playbook 都不删除，replay test 逐条追加
  （只保存 contract version、input fingerprint 与 bounded issue codes，不保存 payload 正文）。

网页观察与手工输入（Phase 8A）：**已实现** 「配置好的公开页面 + 你粘贴进来的文本」都会变成
durable、可版本化的观察，并进入同一套 bounded analysis —— 不引入通用 HTTP 客户端，也不创建任何任务：

```toml
# ~/.config/growing-assistant/config.toml
[watchers]
poll_interval_seconds = 300
timeout_seconds = 20
max_response_bytes = 2097152
full_fetch_every = 24

[[watchers.web]]
id = "course-notices"
url = "https://example.edu/notices"
enabled = true
```

```bash
# 观察配置好的页面（只有 sync 会联网）
pw watch targets
pw watch sync                 # 单个目标：pw watch sync --target course-notices
pw watch status
pw watch observations
pw watch observation show OBSERVATION

# 把一段文本交给助手（例如转发的 QQ 消息）
pw ingest text "Forwarded QQ notice..." --source qq-forward
pw ingest list
pw ingest show INPUT
```

要点：

- **URL 只来自配置**：只接受 `https://`，不允许 userinfo、IP-literal host 或显式端口；没有
  `pw watch fetch <url>` 这样的命令，模型也不能选 URL。fetch 前会解析 hostname 并要求**每个**
  解析结果都是公网地址（loopback / private / link-local / multicast / reserved / CGNAT 一律拒绝）。
- **不跟随跳转、不登录、不渲染 JS**：任何 3xx（`304` 除外）都是 `WebWatchRedirectNotAllowed`，
  用户需要配置最终 URL；没有 cookie、没有认证、没有浏览器（也不使用 eHall 的 Playwright）。
- **响应有上限**：流式读取，超过 `max_response_bytes` 立即放弃且**不写 snapshot**；只接受
  `text/html`、`text/plain`、`application/json`，PDF/图片/压缩包不会被下载。
- **首次抓取只是 baseline**：写入 snapshot + baseline observation + state，**不产生事件**，
  所以打开 watcher 不会把整站历史当成「新变化」；只有 normalized content hash 变化才产生
  `web.page.changed`。配置的 URL 改了 = 重新建立 baseline。
- **HTTP validator 只是优化**：只有 `checks_since_full < full_fetch_every` 时才带
  `If-None-Match`/`If-Modified-Since`；每满 `full_fetch_every` 次强制完整抓取，因此「永远 304」
  的服务器藏不住变化。
- **内容身份是 hash**：HTML 用 stdlib parser 抽取（丢弃 `script`/`style`/`noscript`，不下载任何子资源），
  CRLF→LF、逐行去尾空格、折叠多余空行后再 `sha256`；文本存在
  `~/.local/share/growing-assistant/web/snapshots/<prefix>/<sha>.txt`，数据库只存 relative key。
- **事件只带身份**：`web.page.changed` 的 payload 只有 `{observation_id, target_id}`，
  `manual.input.received` 只有 `{manual_input_id, source}` —— 页面正文和你粘贴的文本不会进事件表。
- **手工输入先落库再排队**：`pw ingest text` 先写 `manual_inputs`，再幂等 ingest 事件，然后告诉你
  「如果配置了 model 且 assistantd 在跑，这段文本可能被发送给 provider 做分类」——它自己**不调用模型**。
- **分析只做分类与候选**：web/manual 内容都是 untrusted quoted data（prompt 里明确写出），
  context 只包含 bounded diff 或 bounded 文本，**不附带** Knowledge / Facts / Tasks / mail / Playbooks，
  也**不含 URL**；输出 schema 只有 category / summary / action_candidates（deadline 与 event-start 分开）。
  watcher/manual analysis 可能把 bounded 内容发给配置的 model provider，但不会自动创建
  Task/Case/Action/Approval/Fact/Playbook/Notification。
- **crash 安全**：observation 先提交、事件后 ingest、link 最后写；两个 crash window 都由每轮 bounded
  repair 修复（即使这一轮没有任何页面变化）。一个目标失败只影响它自己，daemon 的其它 service 照常运行。
- **没有 model 时不丢数据**：没有可用 model 时 `event-worker` 不启动，观察与事件保持 durable 的
  `RECEIVED` 状态，配置好 provider 后会被处理；`web-watch` 只在有 enabled target 时才加入 daemon。

受控的 MCP / VS Code 集成（Phase 8B）：**已实现** 一个本地 stdio MCP server —— 让编辑器（和你
在编辑器里使用的 agent）看到开放的 task / case / 本周计划，而**不能**审批、执行或发送任何东西：

```toml
# ~/.config/growing-assistant/config.toml
[mcp]
enabled = true
write_scope = "none"        # none | tasks
expose_knowledge = false    # 只有你明确需要时才打开
```

```bash
# 1. 看看这个 host 实际会暴露什么（本地、只读、不启动 server）
pw mcp status

# 2. 打印 VS Code 配置片段（只打印，不写任何文件）
pw mcp vscode-config

# 3. 把 snippet 放进 VS Code 的 MCP 配置（workspace 或 user 的 mcp.json），
#    然后启动/信任这个 server，先检查 read-only 的 resources / tools。
```

要点：

- **只是一个本地适配器，不是 Agent Runtime**：它是 VS Code 启动的 stdio 子进程，
  不托管在 `assistantd`、不需要 daemon 在跑、不监听任何端口。stdout 只承载 MCP 协议消息，
  日志全部走 stderr。
- **默认只读、默认关闭**：`enabled = false` 时 server 会在 stderr 说明原因并以非零码退出
  （stdout 保持为空）；开启后默认 `write_scope = "none"`、`expose_knowledge = false`。
  默认 surface 恰好是 4 个 resource（`assistant://status`、`assistant://tasks/open`、
  `assistant://cases/open`、`assistant://plan/current`）与 2 个只读 tool
  （`assistant_get_task`、`assistant_get_case`）。case 只返回生命周期字段，不展开其中的 action。
- **写能力必须由你在服务器端显式打开**：只有 `write_scope = "tasks"` 才会注册
  `assistant_create_task` / `assistant_complete_task`，并且只调用既有 `TaskService`
  （deadline reminder、commitment revision、rolling replan 语义完全一致）。
  未开启时这两个 tool **不存在**（客户端按名字调用得到 unknown tool）——
  VS Code 的 tool-call 确认框只是 UX，不是授权边界。
- **MCP 永远不能做的事**：创建 Approval、执行 ActionRequest、发送邮件、提交 eHall、
  确认/拒绝 fact、创建/晋升 Playbook、读写文件、执行 shell、访问任意 HTTP/browser、
  使用 sampling/elicitation/prompts，或通过 MCP roots 读 workspace 文件。
  它也不 import `/proc` 或凭据：server 读的是和你其它命令同一份 host 配置。
- **知识库是显式 opt-in**：只有 `expose_knowledge = true` 才会出现
  `assistant_search_knowledge`，它是**本地 deterministic 全文检索**（不调用任何模型），
  只返回 logical URI + source span + bounded excerpt（单条 ≤1200 字符、总计 ≤6000），
  绝不返回物理路径或整篇文档。tool 描述明确写着：这些 excerpt 会被交给连接的 MCP client/model。
- **MCP 不产生新状态**：没有 migration、没有 session、没有 durable server state；
  它只是把既有 SQLite 状态读出来（以及在允许时写 task）。
- **VS Code 的信任提示是客户端的事**：VS Code 会要求你信任本地 MCP server，
  这个项目不去修改你的编辑器配置（`pw mcp vscode-config` 只打印），也不把那段信任当成自己的授权。

**尚未实现**：其它 eHall 事务（退课/撤销/删除等高风险能力永不实现）、generic browser agent、
公网部署 / cloud relay / VPN / 第三方登录、手机推送（APNs / FCM / Web Push）、
手机端执行动作（执行只在 host 上发生）、正文索引/问答（把邮件正文送进 knowledge index）、thread 回溯修复、
分类结果自动转 Task/Case、事件删除同步（server-side deletion）、attachment materialization、
QQ 协议/客户端自动接入、个人估时学习、把 Playbook 实例化为新 action（参数化 / 模板推断 / 自动晋升）、
用已确认事实自动填表或自动注入 model/mail context、把 web/manual 分析候选自动转成 Task/Case/Action、
需要登录或渲染 JavaScript 的站点（Phase 8A 只观察公开 HTTPS 页面）。
明确边界：**model 不能直接修改 task、文件、scheduler 状态或任何外部服务**；它只能产出文本，
是否可用由本地 deterministic validation 决定。

## 完整性检查、备份与恢复（Phase 9A，ADR-0031）

运行态的权威只有一个 SQLite 数据库（`assistant.db`）加上它引用的两类不可变对象：mail raw
RFC822 与 web normalized snapshot。备份就是把它们**一致地**取出来，恢复就是把它们**安全地**放回去。

```bash
pw integrity check                     # 只读、离线；不建库、不迁移、不修复

pw backup create ~/assistant-backup.gab
pw backup verify ~/assistant-backup.gab
pw backup inspect ~/assistant-backup.gab

pw backup restore ~/assistant-backup.gab --to ~/recovery/growing-assistant
```

- **一致性**：备份使用 SQLite 官方 backup API 取快照，**绝不**复制 `assistant.db` 或它的
  `-wal`/`-shm`（daemon 运行时复制出来的文件可能是撕裂的）。引用对象按数据库里记录的 SHA-256
  逐个复核：缺失是 `BackupSourceMissing`，hash 不符是 `BackupSourceCorrupt`，两种都让整次备份失败
  ——不会产生「成功但缺文件」的归档。
- **归档形状固定**：`manifest.json`（只带身份与 hash，不带正文）+ `runtime.sqlite3` +
  `mail/raw/…` + `web/snapshots/…`，顶层没有第五种成员。因此 **知识索引、原始 vault/local 文件、
  eHall 浏览器 profile、`config.toml`、`.env`、凭据、日志都不在备份里**：索引是可重建的派生数据，
  凭据与会话属于环境。
- **校验是只读的**：`pw backup verify` 不写任何地方，只做成员名安全检查（拒绝绝对路径、`..`、
  反斜杠、盘符、symlink、重复成员、未列出成员、缺失成员、超出大小/压缩比上界的「zip bomb」）、
  manifest 严格解析、逐成员 size + SHA-256 校验，以及对归档内数据库的 `integrity_check`、
  `foreign_key_check` 与迁移兼容检查。
- **恢复只写 staging**：`--to` 的目录必须不存在或为空，且不能是当前 runtime 数据目录本身/父/子；
  没有 `--in-place`、没有 `--force`。内容是先写到同 parent 的临时目录，验证 + finalization 全部通过后
  才原子 rename 成最终目录；失败时只删掉这条命令自己创建的临时目录。恢复出来的目录就是**一个
  完整的数据目录**：运行态数据库加上归档里两类被引用的对象，位置与该 runtime 目录下原本的
  布局完全一致（mail raw 仍在 `<runtime>/mail/…`，web snapshot 仍在 `<runtime>/web/snapshots/…`），
  因此它等价于 `$XDG_DATA_HOME/growing-assistant/`，按下面的方式用 `XDG_DATA_HOME` 指向它即可。
- **恢复会失效授权，但保留历史**：finalization 在恢复出来的数据库上把未消费的 `ApprovalChallenge`
  置为 consumed、把仍然有效的 `Approval` 置为 superseded（不会假装它被使用过）、作废未使用的
  mobile pairing token、revoke 所有 mobile session —— **恢复后必须重新 `pw mobile pair`**。
  `ActionRequest` / `Approval` / `ExecutionRun` 的历史行一条都不会删。
- **恢复不恢复凭据**：SMTP 密码、IMAP 密码、DeepSeek key、eHall 已登录的浏览器 profile 都不在备份里，
  必须由你重新提供（也不会生成 `.env`）。
- **`RUNNING` / `UNKNOWN` 的外部执行保持未决**：恢复不是对账，不声称外部副作用被回滚；这些
  `ExecutionRun` 原样保留，并继续挡住 `pw action execute` 的盲重试。
- **退出码**：`0` 成功/有效；`1` 归档或运行态有问题（或被策略拒绝，例如目标目录非空、指向 live
  runtime）；`2` 命令用法问题（例如 `FILE` 不是一个文件）——恢复流程里"用错了参数"和"备份坏了"
  值得区分。

### Disaster recovery

1. 停掉 `assistantd`（以及任何正在运行的 `growing-assistant-mcp` 会话）。
2. 验证备份：`pw backup verify ~/assistant-backup.gab`。
3. 恢复到新的 staging 目录：`pw backup restore ~/assistant-backup.gab --to ~/restored-assistant-data`。
4. 对恢复出来的运行态体检（恢复目录的父目录就是它的 `XDG_DATA_HOME`）：
   `XDG_DATA_HOME=~/recovery uv run pw integrity check`。
5. 单独重新配置凭据（SMTP / IMAP / `DEEPSEEK_API_KEY`），备份里没有它们。
6. 需要 eHall 时重新 `pw ehall login`（browser profile 不在备份里）。
7. 重新配对手机：`pw mobile pair`（所有 session 已在恢复时被 revoke）。
8. 在做任何事之前先看未决动作：`pw actions` / `pw action show …`，特别是 `RUNNING` / `UNKNOWN` 的
   `ExecutionRun`，用 Sent 对账或人工检查确认，而不是重试。
9. 重建知识索引：`pw reindex --root <id>`（索引是派生数据，从原始 root 重建）。
10. 用恢复出来的数据目录启动：`XDG_DATA_HOME=~/recovery uv run assistantd`（配置仍来自
    `XDG_CONFIG_HOME`，凭据仍来自环境变量）。

**不要**在 daemon 运行时把备份里的文件拷到 live 数据库上；本项目不提供这种路径，也不需要它。

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
  → pw ehall certificate prepare（白名单 ehall.submit-certificate）
  → 同一审批链 → headed browser（contract 校验 / 精确填写 / readback / 单次 submit）
  → result → archive → PlaybookCandidate → review/test → Playbook
```

进程模型：一个长期运行的 `assistantd`（Python 3.13 + asyncio），未来内部承载四类长期服务，
每类都有独立异常边界，单个服务失败不得拖垮整个 daemon。另有 CLI `pw`。Windows Task Scheduler
未来只在登录时负责拉起 WSL 内的 `assistantd`。

四条不可破坏的安全约束（详见 spec 与 ADR）：

1. 幂等由代码与持久状态保证，模型不参与幂等判断。
2. 任何外部副作用必须绑定具体 `ActionRequest` 的人工 `Approval`（approval 绑定 action
   fingerprint，内容变化即失效；runtime 没有自行批准的代码路径）。Phase 6A/6B/6C 已实现该边界：
   approval 只能由 `pw action approve` 用一次性 token 创建，执行前重新校验 fingerprint，
   production 能力集只有 `mail.send` 与 `ehall.submit-certificate` 两项（ADR-0023/0024/0025）。
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
