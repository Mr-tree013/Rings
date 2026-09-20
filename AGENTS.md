# growing-assistant — 项目规则（面向后续 Codex / Agent 会话）

本文件是**项目级**规则，优先级高于通用习惯；与本文件或架构文档冲突时，停止实现并报告，
不要自行改架构。

## 0. 修改前必读

1. `docs/specs/0001-system-design.md`（Architecture v1，已冻结）
2. 与任务相关的 `docs/adr/0001`–`0007`
3. 与任务相关的源码与测试

读完再动手。若需求与 spec/ADR 冲突：**停止该部分实现，报告冲突**，不要扩大 scope。

## 1. 工作纪律

- 一次只完成当前明确任务，不顺手做其它改进。
- 禁止无关重构；禁止为了美观重排既有代码。
- 禁止擅自增加 production dependency。需要新增时先说明理由与替代方案，等确认。
- 任何行为变更都必须有测试；不写无意义测试去凑覆盖率。
- 完成任务前必须运行 `uv run ruff check .`、`uv run mypy src`、`uv run pytest`，修复错误后才算完成。
- 不谎报完成。验证命令的输出才是证据。

## 2. 分层边界（Modular Monolith）

```text
domain        纯领域模型与规则：不依赖任何 adapter、框架、I/O
application   用例编排：依赖 domain 与 ports
ports         接口定义（ModelPort、StorePort、MailPort、HumanApprovalPort…）
store         SQLite 持久化实现：唯一 runtime store（ADR-0003）
adapters      实现 ports 的外部适配（DeepSeek、IMAP/SMTP、Playwright、Web）
```

硬性约束：

- **Domain 层不得依赖 Adapter**，也不得 import DeepSeek、Playwright、SQLAlchemy、任何 Web 框架或数据库驱动。
- Domain 不得发起 I/O；副作用只能出现在 adapter 或 application 的显式步骤里。
- **Async application code must not directly execute blocking sqlite3 calls.** 阻塞的
  sqlite3 只能在 store adapter 内部的 private `_*_sync` 方法里执行，且必须经
  `asyncio.to_thread` 进入；connection 必须在执行 SQL 的那个 worker thread 内创建和关闭，
  禁止跨线程传递 connection，禁止设置 `check_same_thread=False`（ADR-0009）。
- **External source adapters ingest through `EventInbox` rather than writing events
  directly to SQLite.** 任何新的 input source 都只能调用
  `assistant.application.event_inbox.EventInbox.ingest()`，不得自己调 repository 写事件。
- **Event consumers must use atomic `claim_next` semantics; `list_pending` is not a
  work-claim API.** 领取工作只有一条合法路径：repository 的 `claim_next()`（单事务
  select+update，带 fencing token）。`list_pending()` 仅用于查看/兼容，禁止用它拼 worker。
- **Event processing is at-least-once. Handlers must be idempotent or only create durable
  downstream intents.** 崩溃发生在 "handler 成功" 与 "记录完成" 之间时，该事件会被重新
  处理；handler 不得假设自己只跑一次（ADR-0010）。
- **`CancelledError` must not be converted into an ordinary event-processing failure.**
  取消必须向上传播，事件保持 PROCESSING 并靠 lease 过期恢复；禁止把取消写成 FAILED。
- **Physical filesystem paths are runtime locations, not persistent document identities.**
  文档身份只能是 `root_id + relative_path`（`local://…` / `vault://…`）；`/mnt/e/...`、
  `E:\...` 只能作为可变的 runtime metadata（`storage_roots.last_known_path`），
  绝不允许进入 logical URI 或长期引用（ADR-0011）。
- **Filesystem metadata scanning must not follow symlinks.** file/dir symlink 一律跳过并计数，
  禁止 `follow_symlinks=True` 的遍历（ADR-0011）。
- **Incomplete scans must never mark unseen catalog entries missing.**
  只有 `complete=True` 的 snapshot 才能执行 missing 判定；scanner 出错时 `marked_missing` 必须为 0。
- **Metadata scans must not read full file contents.** 本阶段禁止在扫描里 `open()` / `read_bytes()` /
  hash / OCR；size 与 mtime 只能来自目录项元数据（有静态测试守）。
- **Full-text indexes are derived data; original files remain authority.** 正文与 FTS 索引可随时重建，
  绝不放进 `assistant.db`；Vault 的索引在 `<vault>/.pa/index.sqlite3`，Local root 的索引在
  `$XDG_CACHE_HOME/growing-assistant/knowledge/<root-id>/`（ADR-0012）。
- **Archive Vault full-text content must not be silently copied into host runtime storage.**
  Vault 离线时只允许返回 catalog metadata，绝不允许把 Vault 正文复制到主机或缓存里"备用"。
- **Search results referring to file content must carry a source span.** 每个 content hit 必须带
  `page N` 或 `lines A-B`；没有定位信息的正文命中不允许出现在结果里。
- **Indexers must revalidate filesystem metadata and symlink safety at read time.**
  读正文前必须重新校验 relative path、拒绝 symlink 与非普通文件，并比对 catalog 的 size/mtime
  （读取前后各一次）；不一致即为 ERROR，不得写入索引。
- **Raw user text must not be passed directly as FTS `MATCH` syntax.**
  用户查询一律当作纯文本：长查询用带转义的 phrase，短于 3 个 code point 用 literal `LIKE`
  （`%`/`_`/`\` 必须转义并 `ESCAPE`）。
- **Periodic reconciliation is the correctness mechanism for storage indexes; filesystem
  notifications may only be an optimization.** 索引与 catalog 的正确性来自周期性
  reconciliation（scan → catalog → knowledge index），不得依赖 watcher 事件不漏（ADR-0013）。
- **Configured physical paths are host-local locations, not persistent storage identities.**
  config 里的 path 只是本机运行时位置；身份仍是 `root_id`（Vault 还要用 `.pa/vault.toml` 复核）。
- **An incomplete catalog scan must not trigger knowledge reindexing.** 扫描不完整时保留已见
  metadata、跳过知识索引，且不得执行 missing 判定，避免临时权限错误抹掉可搜索内容。
- **A root-level operational failure must not prevent reconciliation of unrelated roots.**
  offline / identity mismatch / index 损坏只影响该 root；host runtime DB 故障才向上抛给 supervisor。
- **Task, Deadline, CalendarEvent, PlanBlock, and WorkSession are distinct domain concepts and
  must not be collapsed into one schedule item.** Task 不是日历事件；Deadline 不是时间块；
  PlanBlock 是计划、WorkSession 是事实（ADR-0014）。
- **Actual work is derived from WorkSession records, never from PlanBlock duration.**
  估时用 `Task.estimated_minutes`，实际耗时只来自 WorkSession 的精确秒数；禁止从 PlanBlock 推断。
- **Deadlines do not occupy calendar time.** `get_busy_intervals` 只包含 active CalendarEvent 与
  active PlanBlock；deadline 永远不出现在忙碌时间里。
- **Task terminal transitions and cancellation of unfinished PlanBlocks must be atomic.**
  complete/cancel 与"取消 `ends_at > terminal time` 的未完成 plan block"必须在同一事务内完成。
- **Application services use Clock for domain timestamps; domain code never reads wall-clock
  time directly.** 时间来源只有 `Clock`（domain 与 application 都有静态测试守），
  CLI 用户输入的时间必须是带 offset 的 ISO 8601，绝不猜测本机时区。
- **Planner output must be a reviewable proposal; planning must not silently mutate
  PlanBlocks.** Planner 先写 durable `PlanProposal`，只有用户显式 apply 才产生 plan block（ADR-0015）。
- **Manual PlanBlocks are user-owned busy time and must never be replaced by the automatic
  planner.** apply 只能取消/替换 `origin=planner` 且与 proposal window 相交的 block。
- **Planner-generated blocks must retain proposal provenance.** `origin=planner` 必须携带
  `proposal_id`，`origin=manual` 必须为 NULL（domain 与 DB CHECK 双重保证）。
- **A stale proposal must never be applied.** apply 事务中必须校验 commitment revision 与
  proposal 记录一致；不匹配就标 STALE 且不写任何 plan block。
- **Remaining task effort is estimated effort minus recorded WorkSession effort; PlanBlock
  duration is never treated as actual work.** 估时来自 `Task.estimated_minutes`，实际耗时只来自
  WorkSession；PlanBlock（含旧 planner block）不减少剩余工作量。
- **ScheduledJob intent must be durable; asyncio timers are never the source of truth.**
  reminder / rolling replan 意图写在 runtime SQLite（`scheduled_jobs`），daemon 重启后必须从
  DB 恢复；进程内定时器只能作为唤醒手段（ADR-0016）。
- **Deadline reminder jobs must be updated atomically with deadline/task mutations.**
  set/clear deadline、complete/cancel task 与 reminder job 的 materialize/cancel 必须在同一个
  transaction 内完成；不允许先 commit task 再补 job。
- **Rolling replanning may create a proposal but must never auto-apply it.**
  自动重规划只产生新的 `PENDING` `PlanProposal`（并写 `PLAN_READY` notification）；apply 永远
  由用户显式执行。
- **Notification creation must be idempotent across scheduler crash/retry.**
  notification 的 `dedup_key` 由 DB UNIQUE 强制，一个 job 最多产生一条通知；禁止只靠
  SELECT-then-INSERT。
- **ScheduledJob execution is at-least-once and fenced by a lease claim token.**
  complete/retry/dead-letter 必须带 claim token 且只作用于 `processing` 行；过期 lease 可被
  reclaim，旧 token 立即失效。
- **Scheduler job payloads are typed JSON data, never executable code.**
  payload 只允许 canonical JSON object（固定 schema）；禁止 pickle / module path / eval /
  shell string。
- **Application code depends on ModelPort, never on a concrete model provider.**
  provider、base URL、model 名、HTTP 细节只能存在于 `adapters/model/`（ADR-0017）；core 只认
  `ModelPort.complete()`。
- **Model output is untrusted input and must pass deterministic validation before use.**
  structured output 必须本地 `json.loads` + `Draft202012Validator` 校验；provider 声称的
  structured-output guarantee 不能替代本地校验。
- **The model must never own durable state or directly write application databases.**
  model 无 SQLite / filesystem / scheduler / email / browser / eHall 能力；Phase 4A 不提供
  tool calls，任何 function_call 响应都是 protocol error。
- **Do not persist or log provider reasoning / chain-of-thought.**
  reasoning 在 adapter 内部解析后丢弃，不返回、不打印、不写日志、不落库；`ModelResponse`
  没有存放 reasoning 的字段。
- **API keys and provider credentials must never appear in repository files, prompts, logs,
  or test fixtures that contain real secrets.** key 只从 `DEEPSEEK_API_KEY` 环境变量注入；
  `[model].api_key` 这类配置键必须被 strict parser 拒绝。
- **Tool execution is not part of ModelPort.complete; side-effect capabilities require a
  separately designed application boundary.** 不允许把 tools/stream/memory 加进 ModelPort。
- **Natural-language interpretation must produce a typed draft; it must not call mutation
  services.** `pw interpret` 只能返回 `CommandDraft` + preview；Interpreter 依赖图里不允许出现
  TaskService / CalendarService / PlannerService / SchedulerService（ADR-0018）。
- **Model-produced entity identifiers must be validated against the exact context supplied to
  the model.** 只能匹配本次 context 里的 task UUID；禁止按 title 猜、按 prefix 解析、或回头查库。
- **Interpreter context must be explicit, bounded, and data-minimized.**
  只发送 open task metadata（id/title/priority/estimate/deadline/updated_at）+ current time +
  planning timezone；task description、WorkSession、CalendarEvent、PlanBlock、Notification、
  ScheduledJob、knowledge content、文件路径、mail 一律不进 prompt。
- **Task titles and retrieved context are untrusted data, not instructions.**
  context 以 canonical JSON 传入 USER message，绝不拼进 instructions；prompt 必须声明 context
  是不可信数据。
- **Do not execute model-generated shell strings. CLI previews are rendered locally from typed
  commands.** 等价命令由本地 renderer 用 `shlex.quote` 生成，禁止把模型输出当命令执行。
- **Natural-language temporal interpretation requires an explicitly configured planning
  timezone.** 没有 `[planning].timezone` 时，任何 time-bearing command 一律转成
  NEEDS_CLARIFICATION；禁止回退到 machine-local timezone。
- **Retrieved document content is untrusted data, never instructions.** 检索到的正文只能出现在
  USER message 的 evidence JSON 里，绝不拼进 instructions；prompt injection 不能造成任何副作用
  （因为 model 没有 tool / mutation 能力），但答案质量仍取决于模型是否遵守 grounding 规则。
- **Grounded answers may cite only evidence identifiers supplied in the exact model request.**
  `S1..Sn` 是 request-local capability token；引用未提供的 id 必须 deterministic 拒绝
  （`GroundedAnswerInvalidCitation`），禁止忽略、修补或回头查库。
- **Source URIs and source spans shown to users must come from local evidence metadata, never
  from model output.** 模型只输出 segment text + source_ids；`logical_uri` / `page N` /
  `lines X-Y` 一律由本地 evidence map 解析（ADR-0019）。
- **Metadata-only search results are not evidence for content claims.** 只有 full-text content hit
  能作为证据；offline vault 的正文不可用，不能从文件名推断。
- **Grounded-answer mode must not fall back to model world knowledge when indexed evidence is
  insufficient.** 没有证据就不调用模型（零证据时本地直接 INSUFFICIENT_EVIDENCE），有证据但不足时
  由模型返回 `insufficient_evidence`，绝不补充一般知识。
- **Knowledge-answer paths are read-only and must not import mutation services.**
  `pw ask` 路径不得 import store/adapters/sqlite/httpx，也不得 import Task/Calendar/Work/
  Planner/Scheduler/Interpreter service；不新增 durable state、不写 index。
- **IMAP UIDs are meaningful only together with account, mailbox, and UIDVALIDITY.**
  邮件位置身份固定为 `(account_id, mailbox_name, uidvalidity, uid)` 且 DB UNIQUE；禁止把 UID 当成
  全局持久身份。
- **A mailbox cursor must never be described as a permanent exactly-once guarantee.**
  cursor 只是增量优化：只能推进到本批次已 durable 处理的 UID；UIDVALIDITY 变化时做 bounded
  reconciliation，且文档不得声称“永不重复/永不漏”。
- **Inbound mail content, HTML, headers, and attachment names are untrusted data.**
  正文/标题/HTML/附件名只存储、不渲染、不执行；HTML 只做最小 text extraction，附件不落地。
- **Mail synchronization must not mark messages read.** 只读 select + `BODY.PEEK`；禁止
  `BODY[]`/`RFC822` 之类会设置 `\Seen` 的取法。
- **A persisted MailMessage must eventually be bridged to exactly one logical InboundEvent
  through durable reconciliation.**
  每轮 sync 都要修复未 link 的 message（bounded），两个 crash 窗口都必须可恢复；`InboundEvent`
  只带 message UUID/account id，绝不含正文。
- **Mail passwords and app passwords are environment-only secrets.**
  `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD`；config 里的 `password`/`secret` 键必须被拒绝，
  secret 不进入 domain object、日志或异常。
- **Phase 5A mail ingress is receive-only; no SMTP capability exists.** 不分类、不起草、不发送、
  不建 Task/Case。
- **Mail threading is deterministic code; the model never decides message identity.**
  parent 只按 `In-Reply-To` 然后 `References`（由近到远）解析，且必须同 account 且只匹配到唯一一条
  已存储 message；重复 Message-ID 一律 `AMBIGUOUS`，禁止任选、禁止按 subject 猜、禁止回溯改写
  已决定的 membership（ADR-0021）。
- **Mail classification creates analysis/candidates only and must not directly create Tasks or
  Cases.** handler 只写 `mail_analyses` + thread membership + InboundEvent 状态；不得 import
  Task/Calendar/Work/Planner/Scheduler/Interpreter service，不得建 Task、不得改 Deadline、
  不得起草或发送。
- **Mail bodies and thread history supplied to the model are untrusted data.** 正文、标题、header、
  线程历史只能出现在 USER message 的 canonical JSON 里；`To`/`Cc`、附件 bytes/hash、raw `.eml`、
  storage key、凭据以及 Task/Calendar/Scheduler/Notification/Knowledge 状态一律不进 context。
- **Deadline candidates and event-start candidates are distinct concepts.**
  `DEADLINE`（by when）与 `EVENT_START`（when it happens）是不同取值，禁止合并成单个 date 字段；
  带时间的 candidate 必须带原文时间或 instant，`interpreted_at` 必须 timezone-aware。
- **A durable analysis with the same input fingerprint must be reused across EventWorker retries.**
  `(analyzer_version, input_fingerprint)` 相同即直接复用、不再调用模型；fingerprint 变化才允许
  原子替换。oversize（无正文）不得调用模型，只记录 `UNKNOWN` 分析。
- **Incoming mail must never trigger personal-knowledge retrieval solely from mail content.**
  只有用户在 `pw mail draft create` 上显式给出 `--context-query` 时才允许调用 Knowledge search；
  没有 query 时 `mail_drafts.py` 里唯一的 knowledge 调用点必须走 `if query is None: return (),
  ()` 分支，且不得把 mail body 当作检索 query（ADR-0022）。
- **Mail reply recipients and reply subjects are deterministic local data, never
  model-generated.** recipient 固定为 `Reply-To`（否则 `From`）解析出的 mailbox，subject 由纯函数
  `reply_subject()` 派生；draft schema 里没有 `to`/`cc`/`bcc`/`subject` 字段。
- **Reply drafting is explicit and local; generating a draft is not sending mail.**
  只有 `pw mail draft create` 会构造带 provider 的 draft service；EventWorker / MailSync /
  Scheduler / daemon startup 都不得创建 draft。本阶段没有 SMTP、没有 Approval、没有 ActionRequest。
- **Missing personal facts must be surfaced for user input rather than hallucinated.**
  模型只能写被 mail thread 或本次显式提供的 knowledge evidence 支持的个人事实；无法支持时写进
  `needs_user_input`，不得编造。
- **MailDraft edits use optimistic concurrency.** `UPDATE … WHERE id=? AND version=?`；版本不匹配
  必须抛 `StaleMailDraftUpdate`，禁止静默覆盖；编辑不允许改 recipient。
- `store/` 是 package，不是单个 `store.py`；未来按 `db / schema / mail / cases / approvals / knowledge / audit`
  拆分，禁止把它养成 God Object。
- 模型通过 `ports` 中的 `ModelPort` 接入；DeepSeek 未来只是一个 adapter（ADR-0005）。

## 3. 安全红线（不可谈判）

- **LLM 不能直接执行副作用。** 模型只能产出建议、草稿、事实候选与 `ActionRequest`。
- 任何外部副作用（发信、提交表单、写外部系统）必须绑定具体 `ActionRequest` 的人工 `Approval`；
  approval 绑定 action fingerprint，`ActionRequest` 内容变化后旧 approval 自动失效；
  Agent Runtime 不得存在自行批准 Action 的代码路径。
- 高风险 eHall 能力（退课、撤销申请、退宿等）必须**不存在于 capability set**，不靠 prompt 禁止；
  production agent 不得获得 generic browser `click_anything()` 之类万能能力，eHall 只能通过显式注册的
  pipeline 暴露能力。
- 事实分级：`FactCandidate` 与 `ConfirmedFact` 必须区分；自动填表只允许满足
  `confirmed AND not expired AND source traceable AND permitted for target field` 的事实。
- 幂等由代码与持久状态保证，模型不参与幂等判断。邮件以 `UIDVALIDITY + UID` 为增量同步基础；
  UIDVALIDITY 变化必须进入 reconciliation，而不是沿用旧 cursor；出站邮件使用持久 outbox 状态机
  （`DRAFT / APPROVED / SENDING / SENT / SENDING_UNKNOWN`）。
- 不得对无法保证的语义做强承诺（例如 SMTP 跨系统 exactly-once）。

## 4. 数据与凭据

- 个人 Vault 与代码仓库**物理分离**（默认 `~/personal-vault/`），绝不放进本仓库（ADR-0004）。
- 仓库内不得出现 API key、邮箱密码或授权码、真实个人数据、运行态数据库。
- 运行状态只写 `~/.local/share/growing-assistant/`，配置读写 `~/.config/growing-assistant/`，
  缓存只写 `~/.cache/growing-assistant/`。
- 冷数据归档使用稳定逻辑 URI（如 `vault://archive-main/...`），不得用挂载路径或 Windows 盘符作持久 ID。

## 5. 冻结的领域术语

`Task` 用户需要完成的工作 · `Deadline` 截止点 · `Event` 固定时间事件 · `PlanBlock` 计划时间块 ·
`WorkSession` 实际工作记录 · `Case` 跨邮件/资料/表单的一件完整事务 · `ScheduledJob` daemon 后台定时作业 ·
`InboundEvent` 外部输入进入 Core 的统一事件 · `ActionRequest` 准备产生副作用的动作请求 ·
`Approval` 用户对具体 ActionRequest 的批准 · `ExecutionRun` 动作实际执行记录 ·
`FactCandidate` 未确认事实 · `ConfirmedFact` 已确认且有来源的事实。

**禁止把 `Case`、`Task`、`ScheduledJob` 混用。**

## 6. Phase 现状

**Phase 1 已完成（v0.1.0）**：durable event core。包括 SQLite 持久层与迁移系统
（`store/db.py`、`store/migrations.py`、`store/events.py`）、`InboundEvent` 领域模型与
状态机、数据库级去重、async repository 边界（ADR-0009）、`EventInbox` 幂等摄取入口、
原子 claim + lease + fencing、确定性 retry/backoff、dead letter 与取消语义（ADR-0010）。

**Phase 2A 已完成**：稳定存储身份与元数据 catalog（ADR-0011）：`local://` / `vault://`
逻辑 URI、`.pa/vault.toml` manifest、不跟随 symlink 的 metadata scanner、
`storage_roots` / `catalog_entries`（迁移 0003）、增量扫描与安全 missing 判定、
离线 Vault 元数据保留、`pw vault init|status|scan`。

**Phase 2B 已完成**：正文抽取与可重建全文索引（ADR-0012）：文本/PDF 抽取、SHA-256 内容指纹、
带 source span 的 chunking、per-root FTS5（trigram）索引、`pw reindex`、`pw search`
（content hit 带 page/lines，metadata-only hit 与 offline root 明确区分）。

**Phase 2 已完成（v0.2.0）**：Host config（`~/.config/growing-assistant/config.toml`）、
`IndexSyncService` 周期 reconciliation（scan → catalog → index）、daemon 集成与 supervisor
（确定性 backoff、单 root 失败隔离）、`pw roots list`、`pw sync`（ADR-0013）。

**Phase 3A 已完成**：Commitment 领域与持久化（ADR-0014）：Task/Deadline/CalendarEvent/
PlanBlock/WorkSession 五个独立概念、迁移 0004、`CommitmentRepository` + `WorkRepository`、
乐观并发（`StaleTaskUpdate`）、原子终态转换、`TaskService`/`CalendarService`/`WorkService`
与结构化 CLI（`pw tasks`、`pw task …`、`pw calendar`、`pw plan …`、`pw work …`）。

**Phase 3B 已完成**：确定性周计划（ADR-0015）：`PlanningConfig`/weekly availability、
纯 `GreedyPlanner`、commitment revision fencing、持久 `PlanProposal`/`ProposedPlanBlock`/
`PlanningIssue`、原子 apply（只替换 `origin=planner` 的 block，永不改 manual block）、
`PlannerService` 与 CLI（`pw plan week|proposals|show|apply`、`pw task edit`）。

**Phase 3C 已完成（v0.3.0）**：durable scheduler（ADR-0016）：`ScheduledJob` /
`Notification` 领域、迁移 0006、lease + claim token fencing、deterministic retry/dead-letter、
deadline reminder 与 deadline/task mutation 同事务 materialize、durable notification inbox
（`pw notifications` / `pw notification show|read` / `pw scheduled`）、debounced rolling replan
（只产生 `PENDING` proposal + `PLAN_READY`）、daemon 新增 supervised `scheduler` service。

**Phase 4A 已完成**：model 基础设施（ADR-0017）：provider-neutral
`ModelPort` / `ModelRequest` / `ModelResponse`，DeepSeek Responses API adapter（
`adapters/model/deepseek.py`）、`FakeModelAdapter`、本地 JSON + JSON Schema 校验
（`application/structured_model.py`）、`[model]` config（key 只走 `DEEPSEEK_API_KEY` 环境变量）、
`pw model status|test` 与 `pw doctor` 只读诊断。

**Phase 4B 已完成（v0.4.0）**：自然语言 Interpreter（ADR-0018）：有限、确定性的 task context
（仅 open task metadata，最多 50 条）、严格 JSON Schema（`oneOf` + `const` + `additionalProperties:
false`）、typed non-executing `CommandDraft`（7 种命令）、task UUID 必须来自 context 的引用校验、
时区策略（无 `[planning].timezone` 时 time-bearing 命令转 clarification）、本地渲染等价结构化
命令（`shlex.quote`）、`pw interpret`（preview only，禁止 `--apply`/`--yes`/`--execute`）。
Interpreter 不执行任何 mutation、不持久化、无 tool loop。

**Phase 4C 已完成（仍是 0.4.0）**：source-grounded knowledge answers（ADR-0019）：
deterministic retrieval（question 原样检索，miss 后按本地关键词回退）、bounded evidence
（每 chunk 4000 / 总 24000 字符、按 rank 编号、`(root_id, chunk_id)` 去重）、logical URI +
SourceSpan 保留、strict grounded-answer schema（每个 segment 必须有 citation）、本地
citation-id 校验、来源元数据本地解析、零证据不调用模型、无一般知识回退、`pw ask`（只读）。

**Phase 5A 已完成（仍是 0.4.0）**：durable IMAP inbound mail（ADR-0020）：`[mail]` 配置 +
环境变量凭据、TLS-only IMAP adapter（read-only select / `BODY.PEEK`）、RFC822 parser、
content-addressed raw `.eml` 存档、`(account, mailbox, uidvalidity, uid)` 位置身份、
bounded initial history、增量 cursor、UIDVALIDITY reconciliation、attachment metadata、
MailMessage → InboundEvent bridge（两个 crash 窗口可恢复）、daemon `mail-sync` service、
`pw mail accounts|sync|status|messages|show`。仍无分类/起草/发送/SMTP，也未启动 EventWorker。

**Phase 5B 已完成（仍是 0.4.0）**：mail threading + structured analysis（ADR-0021）：
deterministic thread linker（`In-Reply-To` → `References` 由近到远、同 account、唯一匹配；
`root`/`linked`/`unresolved`/`ambiguous` 四态、幂等、防循环）、closed analysis schema
（category / requires_reply / summary / action_candidates）、`DEADLINE` 与 `EVENT_START` 分离、
bounded untrusted thread context（当前邮件 + 最多 5 条历史、每条 3000 / 总 12000 字符）、
input fingerprint + 幂等复用（EventWorker retry 不再付费）、oversize 不调用模型、
第一个真实 `EventHandler`（`MailInboundEventHandler` + `InboundEventDispatcher`）、
daemon `event-worker`（仅在同时配置 mail 与可用 model 时启动）、
`pw mail threads|thread show|analysis`。仍不建 Task/Case、不起草、不发送、无 SMTP。

**Phase 5C 已完成（仍是 0.4.0）**：durable reply drafts + explicit knowledge context（ADR-0022）：
`Reply-To` 持久化与 stdlib 地址解析、deterministic recipient（`Reply-To` → `From`）与
`reply_subject()`、durable `MailDraft`（origin / version / needs_user_input /
generation_input_fingerprint）+ `mail_draft_sources`（仅 root/entry/chunk/logical URI/span）、
closed draft schema（body / used_source_ids / needs_user_input）、本地 source-id 校验、
**默认不检索个人知识**（只有显式 `--context-query` 才调用既有 bounded GroundedContext）、
oversize 无正文直接拒绝、optimistic edit（CAS）、`pw mail drafts|draft create|show|edit`。
仍然不发送、无 SMTP / Approval / ActionRequest、无 EventWorker 自动起草。

以下能力全部属于后续 Phase，尚未实现，不得在文档或回答里描述成已完成：

mail 线程回溯修复（parent 迟到不改写历史）/ mail 分类结果自动转 Task / SMTP 发送与审批 /
site watcher / QQ 渠道 / eHall / browser / 个人估时学习 /
agent tool loop / 多工具只读编排 / command execution boundary（`pw interpret --apply` 之类）/
重复任务与重复事件 / embedding 与向量检索 / OCR / Office 文档与压缩包展开 /
filesystem watcher 快速路径 / Web Server / Playwright /
OS·手机推送投递（当前 reminder 只进 durable notification inbox）/ approval token /
Case、Approval 等其余 domain entity / Windows Task Scheduler 配置。

（Task/Deadline/CalendarEvent/PlanBlock/WorkSession、PlanProposal、ScheduledJob 与
Notification 已实现；Case、Approval 等其余 domain entity 仍属后续 Phase。）

新增能力前先确认它属于哪个 Phase，并在 spec 或 ADR 里落了设计再动手。

## 7. 版本管理

- 使用 Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:`）。
- 不重写历史、不强推、不删除用户已有提交。
- `uv.lock` 必须入库；不要提交其它锁文件。
- **当前没有配置 git remote，也从未推送**：按阶段在本地提交并打 annotated tag，等用户明确要求时
  再一次性上传（步骤见 `docs/ops/remote-upload.md`）。不要擅自 `git remote add` 或 `git push`。
- 阶段收尾必须：工作树 clean、tag 已打且信息明确、提交里不含 `*.db`/`*.sqlite3`/`secrets/`/
  `state/`/个人 Vault；并定期在仓库外做 `git bundle` 备份。
- 已发布的 tag（`v0.0.1`、`v0.1.0`、`v0.2.0`）永不改写，也不要在其上追加提交。
