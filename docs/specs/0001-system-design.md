# 0001 — growing-assistant System Design (Architecture v1)

- Status: Accepted (frozen)
- Date: 2026-09-19
- Scope: Architecture v1. Phase 0 delivered the repository skeleton; Phase 1 (v0.1.0)
  delivered the durable event core: persistence, ingestion, atomic claiming with leases
  and fencing, retry/backoff and dead letter. Everything else below is still a plan, not a
  description of existing code.

## 1. 目标

构建一个**持续为本人工作**的个人助手：把邮件、个人资料、办事大厅与手机端连成一条可审计的闭环，
并把每次成功的流程与用户的纠正沉淀为可读、可改、可测试的规则。

五项基础能力（后续 Phase 逐步实现，Phase 1 结束时均不存在）：

1. 个人数据库：索引、检索个人资料，随资料变化更新索引，回答时能定位到原文。
2. 持续关联 smail 邮箱：增量获取新邮件，关联历史往来，结合资料拟回复草稿；草稿由用户确认后发送；
   重复检查邮箱不得重复处理或重复发送。
3. 办理 eHall 事务：查询信息，完成至少一种事务的材料准备与表单填写；提交前展示关键字段与后果，
   由用户明确确认；退课、撤销申请等操作不得自行决定。
4. 手机端联动：可从手机发起任务、查看进度、编辑草稿、确认操作；不依赖桌面端常开的对话窗口。
5. 能力组合：上述能力共享上下文，完成跨工具的完整流程，并沉淀为可复用 playbook。

## 2. 非目标（当前范围之外）

- 多用户、多租户、团队协作。
- 微服务、消息队列、分布式部署。
- 向量数据库 / embedding 检索（先 FTS5，见 ADR-0007）。
- 任何形式的外部副作用自动化（发信、提交表单）在没有人工 approval 的情况下发生。
- 高风险 eHall 操作（退课、撤销申请、退宿等）的自动化——这类能力在代码层面不存在。

## 3. 进程模型

一个长期运行的 daemon：`assistantd`（Python 3.13 + asyncio）。

未来在 daemon 内运行四类长期服务：

1. Mail service（IMAP 增量拉取 / SMTP 出站 outbox）
2. Index service（Vault 扫描与全文索引维护）
3. Web service（手机端页面：任务、进度、草稿、确认）
4. Scheduler service（定时作业：轮询、重试、过期清理）

约束：

- 每类长期服务必须有独立异常边界；单个服务失败不得导致 daemon 整体退出。
- daemon 只负责生命周期与监督，不承载业务规则。
- Windows Task Scheduler 未来只在登录时拉起 WSL 内的 `assistantd`，不承载业务逻辑。
- 当前唯一在跑的长期服务是 `index-sync`（周期性 storage reconciliation，ADR-0013）：
  daemon 启动时先跑一次，然后按 `interval_seconds` 周期重复；它**不**启动 durable EventWorker
  （没有真实业务 handler）。daemon 侧由 supervisor 负责 restart/backoff。

另有 CLI `pw`，未来命令集合：

`search` · `reindex` · `cases` · `tasks` · `scheduled` · `approve` · `run` · `status` · `doctor`

（Phase 0 只实现 `status` 与 `doctor`；其余命令不注册，避免出现假功能。）

## 4. 分层与模块边界（Modular Monolith）

```text
domain        纯领域模型与不变量：无 I/O，无框架依赖
application   用例编排：依赖 domain + ports
ports         ModelPort / StorePort / MailPort / HumanApprovalPort … 的接口定义
store         SQLite 持久化实现（唯一 runtime store）
adapters      DeepSeek、IMAP/SMTP、Playwright、Web 等具体实现
```

硬性约束：

- Domain 不得依赖 adapter，不得 import DeepSeek / Playwright / SQLAlchemy / Web 框架 / 数据库驱动。
- Domain 不得发起 I/O；副作用只允许出现在 adapter 或 application 的显式步骤中。
- `store/` 必须是 package，未来按 `db / schema / mail / cases / approvals / knowledge / audit` 拆分，
  不允许发展成 God Object。
- 模型访问必须经 `ports.ModelPort`，SDK 类型不得越过该边界。

### 4.1 Tree Conversation Runtime（Phase 10A，ADR-0033）

对话是 v1.1 的主交互方向，但它只是**一层新的 surface + 编排层**，不改变上面的分层：

```text
rings / pw chat
      ↓  用户一句话
ConversationService（application）—— 有状态：thread / turn / operation
      ↓  有界上下文（最近消息 + 最近实体 + planning timezone）
ConversationInterpreter（application）→ ModelPort，只产出 ConversationPlan
      ↓  封闭的 typed operation（Phase 10A 只有任务/日历/工时/计划/提醒/知识问答）
ConversationCapabilityRegistry —— 代码拥有的 confirmation policy
      ↓  既有 application services（TaskService / CalendarService / WorkService /
          PlannerService / GroundedAnswerService / scheduler repository）
```

硬性约束：

- 模型没有 tool / function / shell / filesystem / browser / HTTP，也没有数据库或 service 句柄；
  它只能产出 schema 封闭的 `ConversationPlan`，不存在通用 tool loop。
- 模型不能决定风险等级、是否需要确认、由谁执行——这些属于确定性代码
  （`ConfirmationPolicy`：READ / LOCAL_WRITE / CONFIRM_LOCAL，本阶段没有 EXTERNAL_WRITE）。
- 对话不能创建 `Approval`、不能执行 `ActionRequest`、不能发 SMTP、不能提交 eHall；
  `ActionRequest → Approval → ExecutionRun` 语义不变。
- 相对时间只依据显式配置的 `[planning].timezone`；缺失时提问，不猜宿主机时区。
- 对话历史是 durable 的，但不会自动变成 `ConfirmedFact`，也不替代 Roots / Facts。
- 本地写入先持久化 `APPLYING`，中断则成为 `UNKNOWN_LOCAL`，绝不自动重放。

### 4.2 对话式外部动作（Phase 10B，ADR-0034）

发邮件是唯一一种能从对话里触发的**外部**副作用，而它仍然完全走既有边界：

```text
用户一句话
      ↓
ConversationInterpreter → mail.reply_draft / mail.prepare_reply_send
      ↓  （既有 MailDraftService → MailSendActionService）
immutable ActionRequest（mail.send）
      ↓
conversation_external_reviews：指向该 action 的 id / type / fingerprint，30 分钟有效
      ↓  用户下一条消息是明确发送语（确认发送 / 发送 / 发吧 / confirm send）
ConversationExternalReviewService（确定性，无 ModelPort）
      ↓  issue challenge → create Approval → execute
ExecutionRun（既有）
```

硬性约束：

- 预览必须由 `ActionRequest` 的 payload 渲染，模型不参与撰写预览；
- 首轮只能说"准备"：即使说"直接发"，也只会得到预览；
- 通用确认语（可以 / 好 / ok）**不能**发送邮件，只对本地计划有效；
- challenge 明文只在一次确定性调用里存在，不落库、不进模型；
- payload / 收件人 / 主题 / 正文 / 回复元数据任一变化都会让 review 变成 STALE，必须重新准备；
- `mail.send` 不在模型能力表里；`approval.*`、`action.execute`、`execution.*` 同样不存在；
- SMTP `UNKNOWN` 永不自动重试；重启不会自动执行已审批或未知的动作。

### 4.3 每周固定安排（Phase 10D，ADR-0036）

每周重复的课是**权威日历状态**，不是 `ScheduledJob`，也不是一堆 `CalendarEvent`：

```text
RecurringCalendarRule（一条规则 = 一个星期几 + 本地起止时间 + IANA 时区 + starts_on/ends_on）
      ↓  expand(range)（纯 zoneinfo 运算，不读宿主时区）
RecurringCalendarOccurrence（派生视图，不落库）
      ↓
PlannerService：与 CalendarEvent / 手工 PlanBlock 合并为 busy intervals
      ↓
PlanProposal（仍由人确认后才 apply）
```

硬性约束：

- 规则是权威状态，occurrence 是派生视图：不物化、不缓存、不写入 `calendar_events`；
- 一条规则只表示一个星期几；「周一和周三」= 两条规则；
- 新建规则默认使用 `[planning].timezone`；缺失时提问，绝不使用 `datetime.now().astimezone()`、`TZ` 或隐式 UTC；
- v1.1 只支持 WEEKLY、同日、不过夜；单双周 / 每两周 / 每月 / 每年 / 节假日例外 / 考试周例外一律拒绝，不做近似；
- 陈述句（「我每周一十点到十二点有课。」）不等于写入指令：先询问，得到确认才写；「记下来 / 加到日历 / 放进固定安排」才是写入指令；
- 编辑保留规则身份（同 id、同 created_at，新 fingerprint），只影响未来；retire 停止未来 occurrence，不删除历史；
- 重复请求幂等（同 fingerprint = 同一条规则）；
- 对话层不写 SQL、不拥有第二套规则模型：`ConversationHandlers` 只调用 `RecurringCalendarService`；
- 每周规则是**本地**写入：不产生 `ActionRequest` / `Approval` / `ExecutionRun`，不触达外部系统。

### 4.4 对话式新建邮件与联系人（Phase 10E，ADR-0037）

回复邮件与新邮件在**执行前收敛到同一条管道**，区别只在草稿从哪来：

```text
mail.compose_new（模型写 subject/body；runtime 解析收件人与发件账号）
      ↓  持久化 NewMailDraft（version 单调递增）
mail.prepare_new_send
      ↓  与回复共用 MailSendActionService → 同一个 immutable ActionRequest("mail.send")
mail_send_links（closed variant：draft_id 或 new_draft_id，二者恰有其一）
      ↓  ConversationExternalReviewService：从 ActionRequest payload 渲染精确预览
      用户明确说「确认发送」→ 既有 Approval → ExecutionRun → SMTP executor（唯一一个）
```

硬性约束：

- 收件人只有三种来源：**用户本条消息里出现的地址**、唯一一个 ACTIVE `Contact`、「我自己」对应的已配置可发信账号；
- 模型不得发明地址：它提出的 explicit address 必须（归一化后）出现在用户原文里，否则在第一次写入前拒绝，零草稿 / 零 ActionRequest / 零 review；
- 名字查不到、同名多个、可发信账号多个 → 追问，绝不猜；「我自己」只由已配置的 send-ready 账号决定，与本地邮件数量无关；
- 联系人（`contacts`）是本地结构化身份记录，**不是** `ConfirmedFact`，不授予任何权限；同名不同地址是两条记录；
- 新邮件草稿（`new_mail_drafts`）与回复草稿分表：回复保持「必须有来源邮件」的不变量，两者在**执行前**收敛；
- `MailSendPayload` 增加闭合判别字段 `kind`（reply / new），new 变体不带任何回复头；历史 payload 仍然有效；
- 编辑预览后的草稿 → 旧 review STALE，draft 版本 +1，准备新的 ActionRequest（fingerprint 不同），必须重新明确确认；
- SMTP `UNKNOWN` 语义不变、永不自动重试；对账仍按 payload 中的稳定 Message-ID；
- 本阶段不支持附件、定时发送、自动发送、通讯录/网络查询；对话能力集仍无 `mail.send` / `approval.*` / `action.*` / `execution.*`。

### 4.5 对话式长期事实确认（Phase 10F，ADR-0038）

对话历史**不是**长期记忆；只有 `ConfirmedFact` 才是。模型可以*提议*，绝不能*确认*：

```text
fact.propose（key / value / 用户原话）
      ↓  复用既有 Correction + FactCandidate(PENDING)（无新表、无新 migration）
      运行时展示精确预览：「我准备记录这条长期信息：profile.office：仙林」
      ↓  用户用明确短语回复（确认记住 / 记住 / 确认保存 / 确认记录）
ConversationService 确定性解析（无 ModelPort）→ LearningService.confirm_fact
      ↓
ConfirmedFact（同 key 旧值 superseded，历史保留）
```

硬性约束：

- 普通陈述（「我的办公室在仙林」）不创建任何候选；只有显式「记住 / 以后记得 / 保存为长期信息」才提议；
- 泛泛的「可以 / 好 / 嗯 / continue / ok」**不能**确认长期信息；「不要记 / 别记 / 取消」把待确认候选置为 rejected（保留审计）；
- 最终确认是本地知识写入：不产生 `ActionRequest` / `Approval` / `ExecutionRun`，且不经过模型；
- 模型能力集只有 `fact.list` / `fact.show` / `fact.propose`，不存在 `fact.confirm` / `fact.delete` / `memory.*`；
- 事实查询只依据 `confirmed_facts`（提案不算知识，联系人不是事实，对话历史不作证据）；
- 只有 fact key 进入模型上下文，**值不进**：值由运行时从已确认行渲染；
- pending candidate 本身就是可持久化的待确认状态，因此重启后仍可重新展示，且永不自动确认；
- `ConfirmedFact` 在本阶段不自动填入邮件收件人、邮件正文、表单或任何外部动作。

### 4.6 今日概览 Today Brief（Phase 10G，ADR-0039）

「我今天有什么事？」是一个**只读、确定性**的聚合，没有 ModelPort、没有写操作、没有快照：

```text
Clock + [planning].timezone ──► 用户自己的 [00:00, 24:00)
        ├── 安排：当天 CalendarEvent + 派生 RecurringCalendarOccurrence + 已应用 PlanBlock
        ├── 任务：逾期 / 今天截止 / 临近截止 / 高优先级（有界）
        ├── 需要处理：未读提醒、需要回复的邮件（只显示发件人/主题/时间元数据）
        ├── 等待确认：已准备未发送的邮件、待应用提案、待确认固定安排、待确认长期信息
        └── 需要检查：仍未确定的外部执行结果（标注 unknown，绝不称为失败、绝不自动重试）
```

硬性约束：

- “今天”由 `[planning].timezone` 决定；缺失时提问，绝不使用宿主机时区；
- 每节有上限（安排/任务 ≤ 10、邮件 ≤ 5、提醒 ≤ 5、等待/检查 ≤ 5），超出时显示「另有 N 项」；
- 邮件只出现元数据，绝不出现正文；
- 概览不执行、不批准、不重试、不确认任何东西，也不产生任何持久化快照（无 migration、无 integrity 新 section）。

## 5. 核心数据主线

### 5.1 v1 architecture snapshot（Phase 9B 冻结，ADR-0032）

v1.0 的实际结构就是下面这张图；任何一格之外的能力都不存在（不是"没开"，是代码里没有）。

```text
Interaction
  CLI (pw)            LAN mobile control plane            MCP / VS Code (local stdio)

Observe
  IMAP (receive-only)   configured public HTTPS watcher   manual / QQ-forward text
        │                        │                                  │
        └──────────────► InboundEvent ◄───────────────────────────┘

Understand
  deterministic parsing & threading
  ModelPort structured analysis (candidates only)
  grounded local knowledge answers (FTS5 + citations)

Commit / Plan
  Task · Deadline · CalendarEvent · PlanBlock · WorkSession · durable scheduler

Execute
  Case → ActionRequest (immutable, fingerprinted) → human Approval (single-use, exact)
       → ExecutionRun → mail.send | ehall.submit-certificate   （仅此两个 production 能力）

Learn
  Correction → FactCandidate → ConfirmedFact（人工确认，无自动消费者）
  successful run → PlaybookCandidate → dry-run replay → Playbook（非执行）

Operate (Phase 9A/9B)
  single-instance assistantd · read-only integrity check · .gab backup · staging-only restore
```

### 5.2 长期主线

```text
smail
  ↓
IMAP incremental fetch
  ↓
InboundEvent
  ↓
classification
  ├── ordinary correspondence
  ├── receipt/result
  └── actionable notice
           ↓
        Case
           ↓
   personal knowledge search
           ↓
    material checklist
           ↓
      draft / prepare
           ↓
      ActionRequest
           ↓
   explicit human approval
           ↓
        execute
           ↓
        result
           ↓
        archive
           ↓
 PlaybookCandidate
           ↓
       review/test
           ↓
       Playbook
```

## 6. 领域术语（冻结）

| 术语 | 含义 |
| --- | --- |
| `Task` | 用户需要完成的工作 |
| `Deadline` | 截止点 |
| `Event` | 固定时间事件 |
| `RecurringCalendarRule` | 每周固定占用的时间（一条规则一个星期几），occurrence 为派生视图 |
| `PlanBlock` | 计划用于完成 Task 的时间块 |
| `WorkSession` | 实际工作记录 |
| `Case` | 跨邮件、资料、表单等组成的一件完整事务 |
| `ScheduledJob` | daemon 后台定时作业 |
| `InboundEvent` | 所有外部输入进入 Core 的统一事件 |
| `ActionRequest` | 准备产生副作用的动作请求 |
| `Approval` | 用户对某个具体 ActionRequest 的批准 |
| `ExecutionRun` | 动作实际执行记录 |
| `FactCandidate` | 模型或程序抽取出的未确认事实 |
| `ConfirmedFact` | 用户确认且具有来源的事实 |
| `Contact` | 本地结构化身份记录（姓名 + 邮箱地址），用于确定性地解析收件人；不是事实，不授予权限 |

禁止把 `Case`、`Task`、`ScheduledJob` 混用，也禁止把 `Contact` 当作 `ConfirmedFact`。

### 6.1 Product Language（公开概念词汇，post-v1）

公开产品名是 **Rings**。围绕这个名字存在一组**公开概念词汇**：`Roots`、`Seeds`、`Branches`、
`Leaves`、`Rings`、`Tree`（完整定义见 `docs/concepts/rings-language.md`）。

这些词是 **public conceptual vocabulary**，用于 README、产品说明与用户对话；它们**不是
replacement domain model**：

- 不新增、也不替换任何 domain entity。§6 的全部正式技术名（`Task`、`Deadline`、
  `CalendarEvent`、`PlanBlock`、`WorkSession`、`Case`、`ScheduledJob`、`InboundEvent`、
  `ActionRequest`、`Approval`、`ExecutionRun`、`FactCandidate`、`ConfirmedFact`、
  `PlaybookCandidate`、`Playbook`）**全部保留**：代码类型名、表名、迁移名、event type 与 action
  type 都不因产品语言而改名。
- 概念词汇只做映射，不引入新概念：Roots ↔ 知识 root 与来源可追溯性（catalog / 索引 /
  `Correction` provenance / `ConfirmedFact` 历史）；Seeds ↔ commitment 与 planning intent；
  Branches ↔ capability modules（**不是 autonomous sub-agent**）；Leaves ↔ 外部输入
  （`MailMessage` / `WebObservation` / `ManualInput` / `InboundEvent`）；Rings ↔ 已有的 durable
  history（`WorkSession` / `ExecutionRun` / fact supersession / reviewed `Playbook` / 提案与通知）；
  Tree ↔ 面向用户的 coordinator，通过 CLI / LAN mobile / MCP 交互。
- `Tree` **不是**无限制的 autonomous agent，也没有新的权限：`ActionRequest`、人工 `Approval`、
  `ExecutionRun`、fact confirmation、playbook review 与 capability registry 这些边界（§7）对产品
  语言层同样成立，产品语言不得被用来描述一个绕过审批的执行者。
- 兼容性标识属于 v1 compatibility surface，保持冻结：Python 包 `assistant`、distribution
  `growing-assistant`、`pw` / `assistantd` / `growing-assistant-mcp`、XDG 目录
  `growing-assistant`、`GROWING_ASSISTANT_*` 环境变量、MCP resource/tool 名。

## 7. 四条不可破坏的安全约束

### 7.1 幂等由代码与持久状态保证

- 邮件以 `UIDVALIDITY + UID` 作为正常增量同步基础。
- `UIDVALIDITY` 变化时必须进入 reconciliation，而不是继续使用旧 cursor。
- 出站邮件必须使用持久 outbox 状态机，至少包含：
  `DRAFT` / `APPROVED` / `SENDING` / `SENT` / `SENDING_UNKNOWN`。
- 不允许宣称 SMTP 可以严格实现跨系统 exactly-once；`SENDING_UNKNOWN` 是必须显式处理的现实状态。

### 7.2 任何外部副作用必须绑定人工 Approval

- Approval 绑定 action fingerprint；`ActionRequest` 内容变化后，已有 Approval 自动失效。
- Agent Runtime 没有自行批准 Action 的代码路径。

### 7.3 事实具有可信等级

- `FactCandidate` 与 `ConfirmedFact` 必须区分。
- 自动填表只允许使用满足以下全部条件的事实：
  `confirmed AND not expired AND source traceable AND permitted for target field`。

### 7.4 高风险能力在代码能力集合中物理不存在

- 退课、撤销申请、退宿等高风险行为不得仅靠 prompt 禁止。
- Production agent 不得获得 generic browser `click_anything()` 一类万能能力。
- eHall 只能通过显式注册的 pipeline 暴露能力，且高风险 pipeline 不存在提交路径。

### 7.5 事件处理语义（Phase 1，ADR-0010）

- 事件处理是 **at-least-once**，不承诺 exactly-once；handler 必须幂等或只产生可去重的
  durable intent。
- 领取工作只能通过 repository 的原子 `claim_next`（单事务 select + update，带 `claim_token`
  fencing token）；`list_pending` 不是领取 API。
- `complete_claim` / `fail_claim` 必须携带当前 token；lease 过期被重新领取后，旧 token 的
  完成/失败会以 `StaleEventClaim` 被拒绝。
- `asyncio.CancelledError` 不算业务失败：取消向上传播，事件保持 `PROCESSING`，靠 lease 过期恢复。
- 重试使用确定性 exponential backoff（无 jitter）；尝试耗尽或 handler 抛
  `PermanentEventError` 时进入终态 `DEAD_LETTERED`，不会自动重新处理。

## 8. 存储边界

Git 仓库只存：source code、tests、docs、rules、playbooks、evals、migrations、prompts、
example configuration。

```text
~/.local/share/growing-assistant/    运行状态（SQLite、游标、作业、审计）
~/.config/growing-assistant/         配置
~/.cache/growing-assistant/          缓存
```

个人 Vault 与 Git 仓库物理分离，例如 `~/personal-vault/` 或移动存储
`/mnt/e/archive-vault/`，未来包含 `facts/`、`inbox/`、`attachments/`、`archive/`。

冷数据 Archive Vault 使用稳定逻辑 URI：`vault://archive-main/...`；本机目录使用
`local://<root-id>/...`。不得使用 `/mnt/e/...` 或 Windows 盘符作为持久 ID：物理路径只是
runtime metadata（ADR-0011）。

主机端 SQLite 维护**元数据 catalog**（`storage_roots` / `catalog_entries`）：由扫描派生、
可重建，原始文件始终是 authority，本阶段不保存正文、摘要、embedding 或内容 hash。只有一次
**完整**扫描才能把未出现的条目标记为 `MISSING`；Vault 离线不等于文件缺失。

U 盘等移动存储属于 archive storage，不是 Agent runtime。

## 9. Operational Recovery：运行态权威、备份边界与恢复（Phase 9A，ADR-0031）

### 9.1 运行态权威分层

恢复的正确性首先来自"什么是权威"这件事说清楚。四类内容有四种地位，备份与恢复的行为各不相同：

| 内容 | 地位 | 备份 | 恢复 |
| --- | --- | --- | --- |
| runtime SQLite（`assistant.db`） | **durable runtime authority** | 用 SQLite backup API 取一致快照（绝不复制文件） | 恢复后必须通过 `integrity_check` / `foreign_key_check` / 迁移兼容检查 |
| mail raw RFC822、web snapshot 对象 | DB 引用的 **durable referenced source object** | 按 DB 记录的 SHA-256 复核后一并归档 | 逐成员校验 size + hash 后才算可用 |
| per-root knowledge FTS 索引 | **derived（可重建）** | 排除 | 从原始 root 重建 |
| vault / local 原始知识文件 | **external authority**（属于用户） | 排除，绝不复制进归档 | 不恢复，用户自带 |

此外：eHall browser profile 是 session/凭据类材料（排除，恢复后重新登录）；环境凭据、`config.toml`、
`.env`、logs、临时文件都不是 runtime 状态（排除，恢复后由用户重新提供）。

### 9.2 Backup Boundary

归档是**一个文件、一种形状**：`manifest.json`、`runtime.sqlite3`、`mail/raw/…`、`web/snapshots/…`，
顶层不允许出现其它名字，因此"顺手带上一个索引/一份 profile/一个 config"在格式上无处可放。

- 对象成员是 content-addressed 的（`<sha 前两位>/<sha256>.<ext>`），名字必须与内容一致；
- manifest 是 canonical JSON，只带**身份**（format version、application version、created_at、
database member/hash、migration 文件名、每个对象的 storage key/sha256/size、聚合计数），
绝不带 mail 正文、网页正文、Action payload、凭据、token 或原始文档的物理路径；
- 引用集合来自**备份出来的那份数据库**，不来自 live 数据库；引用对象缺失或 hash 不符则整次备份失败
  （`BackupSourceMissing` / `BackupSourceCorrupt`），不产生"成功但缺文件"的归档；
- 写入是原子的：同目录临时文件 → fsync → 自校验 → `os.replace`；已存在的目标文件直接拒绝（V1 无 overwrite）；
- 成员数量、manifest 大小、数据库大小、单对象大小、总解压大小与压缩比都有具名上界，超界即拒绝。

### 9.3 Restore Authorization Invalidation

恢复是**重新武装**：备份里可能带着当时仍然有效的授权能力，而那是过去某次会话（可能在另一台机器上）
授予的。因此恢复分两段：

```text
verify 每个成员（名字 / size / hash）
   ↓
在 restored DB 上跑 integrity_check + foreign_key_check + 迁移兼容
   ↓
finalization 单事务（只写时间戳，不删行）
   ↓
原子 rename 进 caller 指定的空 staging directory
```

finalization 的语义冻结：

- 未消费的 `ApprovalChallenge` → `consumed_at = restore_time`（capability token 被花费，
  **不代表**任何人批准过）；
- 未消费的 `Approval` → `superseded_at = restore_time`（绝不写 `consumed_at`，那等于声称它被执行使用过）；
- pairing token → `consumed_at = restore_time`；所有未 revoke 的 session → `revoked_at = restore_time`
  （恢复后必须重新 `pw mobile pair`）；
- `ActionRequest` / `Approval` / `ExecutionRun` 历史行**永不删除**；
- `RUNNING` / `UNKNOWN` 的 `ExecutionRun` 原样保留为未决审计状态，恢复后仍然挡住 `pw action execute`
  （恢复不是 reconciliation，也不声称外部副作用被回滚）。

恢复目标必须是**不存在或为空**的目录，且不得是 active runtime data dir 自身/父/子；没有 `--in-place`、
`--force`，也不会生成 `.env`。

### 9.4 Acceptance Lifecycle

```text
Observe → Understand → Commit → Plan → Execute → Review → Learn
```

`tests/acceptance/` 用这条主线验收整个系统：真实 SQLite store、migration runner、EventWorker、
Scheduler 与审批链，只有外部边界是 fake（Model / IMAP / SMTP / eHall page / HTTP watcher），
并且**全程开着 outbound socket guard**、使用临时 XDG root。覆盖内容包括：

- 手工/转发文本 → InboundEvent → bounded analysis → 人工建 Task → reminder → plan proposal → apply →
  WorkSession → complete（Observe…Review 真实串通，analysis 绝不自动建 Task）；
- 邮件 → thread/analysis → 草稿 → Case → `mail.send` prepare → challenge → 人工 Approval →
  fake SMTP 恰好一次 DATA → SUCCEEDED → 显式 PlaybookCandidate → dry-run → 人工 promote；
- 知识 grounding → citation → Correction → FactCandidate → 人工 confirm；
- eHall contract 变化导致 click 前 FAILED（submit=0）、匹配页面成功（submit=1）；
- mobile / MCP 边界（手机可审批但不能执行；MCP 不能审批、执行、确认 fact、晋升 playbook）；
- watcher baseline/change 语义、EventWorker crash/reclaim 后 provider 调用次数仍为 1；
- 跨 bootstrap 实例的 restart（Task / InboundEvent retry / ScheduledJob / mail bridge / web+manual bridge）、
  lease 过期 reclaim 与 fencing、`UNKNOWN` 恢复后零重试、supervisor 隔离与 stop event、
  以及一次 seed 了 sentinel 的日志隐私扫描。

## 10. 错误处理与可观测性（设计意图）

- 每个长期服务在自己的异常边界内运行，失败后被监督重启，并向用户可见地报告降级状态。
- 所有会改变外部世界的动作都有 `ExecutionRun` 记录与审计日志。
- 模型调用记录（prompt 版本、模型标识、token 用量、结果哈希）进入审计，便于复现与控成本。
- 不确定状态（`SENDING_UNKNOWN`）永远显式呈现给用户，不猜测、不静默重试导致重复副作用。

## 11. 测试策略

| 目录 | 职责 |
| --- | --- |
| `tests/unit/` | 纯逻辑：domain 规则、状态机、解析器 |
| `tests/integration/` | 组合件：store + application 用例，含 SQLite |
| `tests/contract/` | ports 契约：每个 adapter 必须满足的接口行为 |
| `tests/regression/` | 由真实纠正转化而来的固定用例（"成长"的证据） |
| `tests/e2e/` | 端到端流程：从 InboundEvent 到归档，使用假外部系统 |
| `tests/acceptance/` | 跨系统验收：真实 store / worker / scheduler / 审批链 + fake 外部边界，临时 XDG root，socket guard 全程开启（§9.4） |

原则：不为了覆盖率写无意义测试；每个安全约束至少有一条测试证明它不可被绕过。

## 12. 演进路线

| Phase | 内容 | 状态 |
| --- | --- | --- |
| 0 | 工程初始化、架构文档、版本管理、最小可运行骨架 | 本 Phase |
| 1 | SQLite schema、Event Inbox、domain 实体与状态机 | 已完成（v0.1.0）：`InboundEvent`、迁移 0001/0002、async `EventRepository`、`EventInbox`、`EventWorker`（claim/lease/fencing/retry/dead letter） |
| 2 | 模型接入（ModelPort + DeepSeek adapter）、FTS5 知识检索、Vault 扫描 | 已完成（v0.2.0）：2A 稳定身份与 metadata catalog、2B 正文抽取/SHA-256/per-root FTS5/带 source span 检索、2C host config + 周期 reconciliation + daemon supervisor；模型接入见 Phase 4A |
| 3 | Commitment 领域、Planner、IMAP/SMTP、outbox、草稿与确认链路 | 进行中：3A 已完成（Task/Deadline/CalendarEvent/PlanBlock/WorkSession、迁移 0004、乐观并发与原子终态转换、结构化 CLI）；3B 已完成（确定性 greedy weekly planner、持久可审阅 PlanProposal、commitment revision fencing、原子 apply、planner/manual block 来源，ADR-0015）；3C 已完成（durable `ScheduledJob` + notification inbox、迁移 0006、lease/fencing/retry/dead-letter、deadline reminder 与 mutation 同事务 materialize、debounced rolling replan 只产生 proposal、daemon `scheduler` service，ADR-0016）；邮件、outbox、草稿与确认链路、个人估时学习、自然语言解析未实现 |
| 4 | 模型接入与自然语言 Interpreter、Web 手机端、eHall 低风险 pipeline、playbook 沉淀与 evals | 进行中：4A 已完成（provider-neutral `ModelPort`、DeepSeek Responses API adapter、`FakeModelAdapter`、本地 JSON + JSON Schema 校验、`[model]` config 与 `DEEPSEEK_API_KEY` 环境变量注入、`pw model status|test`、doctor 只读诊断，ADR-0017）；4B 已完成（single-command Interpreter、bounded/deterministic task context（仅 open task metadata，上限 50）、严格 Interpreter JSON Schema、typed non-executing `CommandDraft`、task UUID 必须来自 context、时区策略、本地安全渲染等价结构化命令、`pw interpret`，ADR-0018）；4C 已完成（source-grounded knowledge answers、bounded evidence（字符预算 + rank 编号 + 去重）、logical URI + SourceSpan、strict citation schema、本地 citation-id 校验、来源元数据本地解析、无证据不调用模型、`pw ask`，ADR-0019）；read-only multi-tool agent、conversation memory、command execution boundary、Web 手机端、eHall 与 evals 未实现 |
| 5 | 外部输入：IMAP/SMTP、QQ、站点 watcher、邮件分类与 action extraction | 进行中：5A 已完成（TLS-only IMAP adapter、read-only + `BODY.PEEK`、RFC822 parser、content-addressed raw `.eml`、`(account, mailbox, uidvalidity, uid)` 位置身份、bounded initial history、增量 cursor、UIDVALIDITY reconciliation、attachment metadata、MailMessage → InboundEvent bridge（crash-safe）、daemon `mail-sync`、`pw mail …`，ADR-0020）；5B 已完成（deterministic thread linking（`In-Reply-To` → `References` 由近到远、同 account 唯一匹配、四态 link status、幂等 + 防循环）、bounded untrusted thread context（≤5 条历史 / 每条 3000 / 总 12000 字符）、closed mail-analysis schema（category / requires_reply / summary / action_candidates）、`DEADLINE` 与 `EVENT_START` 分离、durable `MailAnalysis` + input fingerprint 幂等复用、oversize 不调用模型、真实 `MailInboundEventHandler` + `InboundEventDispatcher`、daemon `event-worker`（仅在 mail + model 同时可用时启动），ADR-0021）；5C 已完成（`Reply-To` 持久化 + deterministic recipient/subject、durable `MailDraft` + optimistic edit、closed draft schema、**默认不检索个人知识**、显式 `--context-query` 才走既有 bounded GroundedContext、本地 source-id 校验 + 只存 provenance、missing fact → `needs_user_input`、`pw mail drafts|draft create|show|edit`，ADR-0022）；正文索引/问答、thread 回溯修复（迟到的 parent 不改写历史）、分类结果自动转 Task、SMTP 发送与审批、server-side deletion、QQ 与站点 watcher 未实现 |
| 6 | Action 边界：Case / ActionRequest / Approval / ExecutionRun、真实 executor、Web 与手机端审批 UI | 进行中：6A 已完成（durable `Case`、immutable `ActionRequest` + canonical SHA256 fingerprint、hash-only 单次 challenge（TTL 600s）、exact-fingerprint binding 的人工 `Approval`（同一 action 同时仅一个有效、`superseded_at` 保留历史）、`ExecutionRun`（RUNNING/SUCCEEDED/FAILED/UNKNOWN）、atomic begin-execution 与并发 fencing、UNKNOWN/崩溃不自动 retry、`ActionExecutor` port + **空 production capability set**、`pw cases|case …` 与 `pw actions|action …`，ADR-0023）；SMTP executor、eHall executor、browser executor、mobile approval UI、executor-specific reconciliation 未实现 |
| 7 | 真实外部执行：SMTP 发送、Sent 对账、更多 executor、移动端审批 | 进行中：6B 已完成（`mail.send` executor（唯一注册能力，且仅在配置 SMTP 时）、draft `needs_user_input` acknowledgement、TLS-only SMTP config（starttls/ssl，无 plain） + `GROWING_ASSISTANT_MAIL_<ID>_SMTP_PASSWORD`、immutable `MailSendPayload` + prepare 时生成的稳定 RFC Message-ID、`mail_send_links`（一个 draft version 最多一个 send action）、executor offline `supports` preflight（不消费 approval）、显式 SMTP 阶段跟踪（pre-DATA = FAILED，DATA 之后不明 = UNKNOWN）、零自动重发、只读 Sent 对账（FOUND → SUCCEEDED；NOT_FOUND 不证明失败；AMBIGUOUS 不任选；UNAVAILABLE 保持原状），ADR-0024）；eHall executor、browser executor、mobile approval UI、更多对账策略未实现 |
| 8 | 白名单 eHall 事务：证明书申请、更多低风险事项、移动端审批 | 进行中：6C 已完成（manual `pw ehall login` + persistent headed Chromium profile + NJU top-level origin allowlist、typed only pipeline（`inspect_form` / `submit_certificate`，无 generic browser API）、只读 inspect、page-contract fingerprint、显式 `--field KEY=VALUE` prepare（不在网页填表）、immutable `ActionRequest("ehall.submit-certificate")` + critical preview、既有 challenge/approve/execute 链、执行前 contract 复核 + 精确填写 + readback + 单次 whitelisted submit、click 前 FAILED / click 后 UNKNOWN 且零自动重试，ADR-0025）；其它 eHall 事务、generic browser agent、mobile approval UI 未实现 |
| 9 | 同一局域网手机控制面：查看进度、建/完成任务、改草稿、审批 exact action | 进行中：6D 已完成（默认关闭的 `[mobile]`（`enabled` / `bind=loopback\|lan` / `port`）、migration 0012（`mobile_pairing_tokens` / `mobile_sessions`，只存 SHA-256、pairing TTL 600s 一次性、session 30 天可撤销）、`MobileAuthService` + `pw mobile status\|pair\|sessions\|revoke\|approval-link`、FastAPI 只存在于 `adapters/web/`（/docs /redoc /openapi.json 关闭 + CSP 等安全 header + private-client middleware 按 socket peer 判定且不信任 forwarding header）、mutation 需 session cookie + CSRF header + CSRF cookie、dashboard / task create+complete / cases / notifications / mail draft 查看与 CAS 编辑 / action 查看与 challenge+approve（复用 `ApprovalService`）、sessionless approval-link（fragment token，只 preview + approve）、daemon `mobile-web` supervised service，ADR-0026）；**手机端无执行入口**（无 SMTP / eHall / ActionExecutionService / ModelPort）、公网部署与 cloud relay、VPN、第三方登录、手机推送未实现 |
| 10 | 个人学习：durable correction、candidate → 人工确认 → ConfirmedFact、未来 autofill 的可信来源 | 进行中：7A 已完成（migration 0013（`corrections` / `fact_candidates` / `confirmed_facts`）、`Correction`（用户原话，provenance 唯一来源）、开放但受约束的 fact key 命名空间 + credential-like segment `ForbiddenFactKey`、candidate 与 correction 同 transaction 写入、`LearningRepository` + `SqliteLearningRepository` + `LearningService`、confirm 单 transaction 内 supersede 旧 current fact + 插入新 fact + 单次状态转换（`UNIQUE(fact_key) WHERE superseded_at IS NULL`）、expiry 为读取期派生（无 job）、历史永不删除、`pw corrections\|correction add\|show` 与 `pw fact candidates\|candidate add\|show\|confirm\|reject`、`pw facts\|fact show`，ADR-0027）；**本阶段无 autofill、无 model fact context、无自动 candidate、无 Playbook**；PlaybookCandidate → replay/test → Playbook、事实参与填表/model context、个人估时学习属后续 Phase 7B+ |
| 11 | 复核过的 Playbook：成功执行 → PlaybookCandidate → 人工复核 → 无副作用 dry-run → Playbook | 进行中：7B 已完成（migration 0014（`playbook_candidates` / `playbook_replay_tests` / `playbooks`）、`PlaybookCandidate` / `PlaybookReplayTest` / `Playbook`（ACTIVE→RETIRED）、候选只能来自明确 SUCCEEDED 的 run（创建时重读 action/run + 重新 hash payload + `EXECUTED` + `SUCCEEDED` + `finished_at`）、`UNIQUE(source_action_id)`、candidate 元数据不可编辑、`PlaybookReplayValidator` port（非 `ActionExecutor`）+ `PlaybookReplayRegistry`（仅 `mail.send` / `ehall.submit-certificate`，无动态 import）、两个纯 parser dry-run validator、bounded issue codes、`replay_input_fingerprint`（含 validator type + contract version）、promote 单 transaction 要求当前 version + 精确 fingerprint 的 PASSED test、`pw playbook candidates\|candidate add\|show\|test\|promote\|reject` 与 `pw playbooks\|playbook show\|retire`，ADR-0028）；**dry-run 不联网、不开浏览器、不发信、不调用 executor、不创建 Approval/ActionRequest/ExecutionRun，PASS 仅表示当前代码仍理解该 payload**；Playbook 实例化与参数化、workflow 自动晋升属后续 Phase |
| 12 | 外部观察：配置好的公开网页 watcher + 手工/转发文本输入 → durable 版本化观察 → InboundEvent → bounded analysis | 进行中：8A 已完成（migration 0015（`web_watch_state` / `web_observations` / `web_observation_event_links` / `manual_inputs` / `manual_input_event_links` / `observation_analyses`）、`[watchers]` + `[[watchers.web]]`（id grammar + HTTPS-only URL：无 userinfo / 无 IP-literal / 无显式端口）、`WebSource` port + `HttpWebSource`（无 cookie/认证/JS/浏览器、不跟随 redirect、resolved address 必须 public、流式 byte cap、ETag/Last-Modified 仅为优化 + `full_fetch_every` 强制 unconditional）、stdlib HTML 抽取 + deterministic normalization + `sha256` 内容身份、content-addressed snapshot（`web/snapshots/`）、baseline 不发事件、change observation + 幂等 `web.page.changed` bridge（两个 crash window 均 bounded repair）、`ManualInput` + `pw ingest text\|list\|show`（先持久化再 ingest `manual.input.received`）、bounded change diff/manual context、closed analysis schema（category / summary / action_candidates；deadline ≠ event-start）、`ObservationInboundEventHandler`（fingerprint 幂等复用、permanent vs retryable model 错误）、daemon `web-watch` service + worker 启动条件改为「有可用 model」，ADR-0029）；**无 generic HTTP/browser 能力、无模型生成 URL、分析不创建 Task/Case/Fact/Playbook/Action/Approval/Notification**；需要登录或渲染 JS 的站点、非默认端口、QQ 自动接入、候选自动转 Task/Case 属后续 Phase |
| 13 | 开发者集成：受控本地 MCP（stdio）→ VS Code | 进行中：8B 已完成（official MCP Python SDK v2（`mcp>=2,<3`，`MCPServer`）、`[mcp]`（`enabled` 默认 false / `write_scope=none\|tasks` / `expose_knowledge` 默认 false，无 transport/port/host/trusted-client 键）、独立 stdio console script `growing-assistant-mcp`（不托管在 daemon、日志只走 stderr、disabled 时 stdout 为空并非零退出、未识别参数报错）、`adapters/mcp/`（唯一 import SDK 处）、`application/mcp_facade.py`（不 import SDK，只包装既有服务为 bounded DTO）、4 个固定 resource（status / tasks/open / cases/open / plan/current，只读且不含 mail/draft/fact/playbook/action）、2 个 read tool + 可选 `assistant_search_knowledge`（本地 deterministic 全文检索，单条 ≤1200 / 总 ≤6000，仅 logical URI + span + excerpt）+ 可选 `assistant_create_task` / `assistant_complete_task`（仅 `TaskService`，保留 reminder/replan/revision 语义）、typed tool error、`pw mcp status\|vscode-config`（只打印 snippet），ADR-0030）；**无 Approval/Execution/SMTP/eHall/Fact/Playbook 能力、无 filesystem/shell/HTTP/browser/sampling、无 migration**；Streamable HTTP/SSE transport、public 部署、MCP prompts/apps 属后续 Phase（当前无计划） |
| 14 | 运行态加固：只读完整性检查、一致备份、staging-only 恢复、恢复时的授权失效 | **已完成（v1.0.0）**：9A 已完成（`domain/backup.py`（固定 archive layout / canonical manifest / 成员名·size·压缩比 bounds）、`domain/integrity.py`、port `RuntimeBackup` / `BackupArchive` / `IntegrityRepository` / `ContentObjectReader`、`store/backup.py`（`sqlite3.Connection.backup()` 一致快照 + 引用对象 hash 复核 + finalization 单事务失效 challenge/approval/pairing/session）、`store/integrity.py`（只读跨域审计）、`adapters/backup/archive.py`、`application/backup_service.py` + `application/integrity_service.py`、`Database.read_only()`、CLI `pw integrity check` 与 `pw backup create\|verify\|inspect\|restore --to`、`tests/acceptance/`，ADR-0031）；9B 已完成（ADR-0032：`adapters/runtime/instance_lock.py`（`flock` 单实例 + `pw daemon status`）、`adapters/runtime/permissions.py`（项目创建对象 `0700`/`0600`，只报告不重写既有权限）、`.gab` 严格 EOF（拒绝尾部附加字节 / ZIP comment / 追加归档）、`DatabaseMigrationIncompatible` + `require_compatible_history()`（未来 schema fail closed）、每个 migration prefix 的升级矩阵与 future-DB 测试、`docs/examples/config.toml` safe-by-default、wheel 内含 `assistant/migrations/` 与 web 静态资源、`pw --version` / `assistantd --version`、`tests/release/`（package smoke / 权限 / 压力 / 重启安全 / 能力冻结 / fresh & historical acceptance / 隐私扫描））；**无 migration（仍 0001–0015）、无新 production dependency、无新外部能力**；自动/后台/云端备份、就地恢复与 `--force`、外部副作用回滚声明、downgrade migration 均不在范围内 |

## 13. 后续阶段的未决决策（明确不属于早期 Phase）

以下问题在对应 Phase 开始前必须单独决策并落 ADR，早期 Phase 不做任何实现或假设：

- ~~手机网页的鉴权方式（设备令牌 / 一次性链接 / 局域网信任边界）~~ → 已由 ADR-0026 决定：
  一次性配对码（只存 hash）→ 可撤销的 hashed session + CSRF；私网客户端边界按 socket peer 判定。
- 邮箱授权码与模型 API key 的存放方式（0600 文件 / 系统 keyring）。
- 邮件分类器的实现路径（规则优先还是模型优先，及其评测方式）。
- Vault 文本抽取的格式支持范围与 OCR 是否纳入。
- eHall pipeline 的具体注册机制与 capability 粒度。

## 14. 相关 ADR

- ADR-0001 Modular Monolith
- ADR-0002 Event Inbox
- ADR-0003 SQLite as runtime authority
- ADR-0004 External personal Vault
- ADR-0005 Model port
- ADR-0006 Action approval boundary
- ADR-0007 FTS5 before vector search
- ADR-0008 Direct `sqlite3` access behind repository ports
- ADR-0009 Async boundary for blocking SQLite access
- ADR-0010 At-least-once event processing with leases and fencing
- ADR-0011 Stable storage identity and rebuildable metadata catalog
- ADR-0012 Rebuildable per-root full-text knowledge index
- ADR-0013 Configured storage roots and periodic reconciliation
- ADR-0014 Explicit commitment and time-planning domain model
- ADR-0015 Deterministic weekly planning with reviewable proposals
- ADR-0016 Durable scheduling, reminders, and rolling replanning
- ADR-0017 Provider-independent model port and structured output boundary
- ADR-0018 Natural-language interpretation produces typed non-executing command drafts
- ADR-0019 Source-grounded answers from bounded untrusted evidence
- ADR-0020 Durable UIDVALIDITY-aware IMAP ingestion
- ADR-0021 Deterministic mail threading and durable model analysis
- ADR-0022 Durable reply drafts with explicit knowledge context
- ADR-0023 Actions, human approval and the execution boundary
- ADR-0024 Approved SMTP delivery with ambiguous-result reconciliation
- ADR-0025 An approved eHall certificate pipeline
- ADR-0026 A same-LAN mobile control plane
- ADR-0027 Human-confirmed personal facts
- ADR-0028 Successful runs become reviewed non-executing playbooks
- ADR-0029 Durable web and manual observation
- ADR-0030 A controlled local MCP interface
- ADR-0031 Operational integrity, safe backup, and recovery
- ADR-0032 Version 1 runtime, upgrade, and release contract
- ADR-0033 The Tree conversation runtime
- ADR-0034 Conversational external action review
- ADR-0035 A conversation reliability boundary
- ADR-0036 Weekly recurring calendar rules
- ADR-0037 Conversational outbound mail and deterministic recipient resolution
- ADR-0038 Conversational fact confirmation
- ADR-0039 Deterministic today brief
- ADR-0040 Interactive terminal line editing and pending confirmation groups
