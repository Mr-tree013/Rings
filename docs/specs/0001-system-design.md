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

## 5. 核心数据主线

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

禁止把 `Case`、`Task`、`ScheduledJob` 混用。

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

## 9. 错误处理与可观测性（设计意图）

- 每个长期服务在自己的异常边界内运行，失败后被监督重启，并向用户可见地报告降级状态。
- 所有会改变外部世界的动作都有 `ExecutionRun` 记录与审计日志。
- 模型调用记录（prompt 版本、模型标识、token 用量、结果哈希）进入审计，便于复现与控成本。
- 不确定状态（`SENDING_UNKNOWN`）永远显式呈现给用户，不猜测、不静默重试导致重复副作用。

## 10. 测试策略

| 目录 | 职责 |
| --- | --- |
| `tests/unit/` | 纯逻辑：domain 规则、状态机、解析器 |
| `tests/integration/` | 组合件：store + application 用例，含 SQLite |
| `tests/contract/` | ports 契约：每个 adapter 必须满足的接口行为 |
| `tests/regression/` | 由真实纠正转化而来的固定用例（"成长"的证据） |
| `tests/e2e/` | 端到端流程：从 InboundEvent 到归档，使用假外部系统 |

原则：不为了覆盖率写无意义测试；每个安全约束至少有一条测试证明它不可被绕过。

## 11. 演进路线

| Phase | 内容 | 状态 |
| --- | --- | --- |
| 0 | 工程初始化、架构文档、版本管理、最小可运行骨架 | 本 Phase |
| 1 | SQLite schema、Event Inbox、domain 实体与状态机 | 已完成（v0.1.0）：`InboundEvent`、迁移 0001/0002、async `EventRepository`、`EventInbox`、`EventWorker`（claim/lease/fencing/retry/dead letter） |
| 2 | 模型接入（ModelPort + DeepSeek adapter）、FTS5 知识检索、Vault 扫描 | 已完成（v0.2.0）：2A 稳定身份与 metadata catalog、2B 正文抽取/SHA-256/per-root FTS5/带 source span 检索、2C host config + 周期 reconciliation + daemon supervisor；模型接入仍未实现 |
| 3 | Commitment 领域、Planner、IMAP/SMTP、outbox、草稿与确认链路 | 进行中：3A 已完成（Task/Deadline/CalendarEvent/PlanBlock/WorkSession、迁移 0004、乐观并发与原子终态转换、结构化 CLI）；3B 已完成（确定性 greedy weekly planner、持久可审阅 PlanProposal、commitment revision fencing、原子 apply、planner/manual block 来源，ADR-0015）；自动重规划、提醒、个人估时学习、自然语言解析与邮件未实现 |
| 4 | Web 手机端、eHall 低风险 pipeline、playbook 沉淀与 evals | 计划 |

## 12. 后续阶段的未决决策（明确不属于早期 Phase）

以下问题在对应 Phase 开始前必须单独决策并落 ADR，早期 Phase 不做任何实现或假设：

- 手机网页的鉴权方式（设备令牌 / 一次性链接 / 局域网信任边界）。
- 邮箱授权码与模型 API key 的存放方式（0600 文件 / 系统 keyring）。
- 邮件分类器的实现路径（规则优先还是模型优先，及其评测方式）。
- Vault 文本抽取的格式支持范围与 OCR 是否纳入。
- eHall pipeline 的具体注册机制与 capability 粒度。

## 13. 相关 ADR

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
