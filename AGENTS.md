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
- **Every external side effect must be authorized by a human Approval bound to one exact
  ActionRequest fingerprint.** `Approval` 永远绑定 `action_id + action_fingerprint`；fingerprint
  变化即失效，必须重新走 `pw action challenge` → `pw action approve`。
- **ActionRequest payloads are immutable; changed content requires a new ActionRequest.**
  没有 update/edit API；payload 只能是 JSON 数据（拒绝 NaN/Infinity/bytes/datetime/任意对象），
  fingerprint = SHA256(canonical JSON)，且加载时重新 hash 校验，绝不只信 fingerprint 字段。
- **Approval challenge secrets are single-use, short-lived, and stored only as hashes.**
  token >=256-bit 随机、TTL 600s、DB 只存 `sha256(token)`；plaintext 只在
  `create_challenge()` 返回一次，禁止 log、禁止在其它命令再次显示、错误信息不得回显 token。
- **Approval is consumed before execution begins and can never authorize a second execution.**
  `begin_execution` 必须在同一 transaction 内消费 Approval + 创建 `RUNNING` ExecutionRun；
  FAILED 的 approval 也已消费，再次执行必须重新人工 approve。
- **An ambiguous external execution result must never be blindly retried.**
  `UNKNOWN`（以及崩溃留下的 `RUNNING`）必须阻塞同一 ActionRequest 的再次执行，直到未来的
  executor-specific reconciliation；`CancelledError` 直接传播，不得自动标 FAILED。
- **High-risk capabilities are absent from the executor set unless explicitly implemented.**
  Phase 6A 的 production executor set 为空；禁止 generic shell/browser/HTTP executor，也禁止动态
  import executor；「能命名某个 action type」不等于「有能力执行它」。
- **Models and autonomous workers have no code path to create Approval records.**
  Interpreter / GroundedAnswer / MailAnalysis / MailEventHandler / MailDraftService / EventWorker /
  Scheduler 都不得 import ApprovalService；ApprovalService 也不得依赖 ModelPort/StructuredModel。
- **SMTP delivery uses an exact immutable ActionRequest snapshot.**
  `pw mail send prepare` 把某个 draft version 冻结成 payload；执行时发送的字节只能来自该 payload，
  绝不重新读取 draft。draft 之后被编辑只影响新 action，旧 approval 不会转移。
- **A stable RFC Message-ID is created before approval and is part of the approved payload.**
  Message-ID 在 prepare 时生成（>=128 bits + sender domain）、写入 payload/fingerprint/发出的字节，
  Sent 对账也只用这一个值；重试或对账时禁止重新生成。
- **SMTP delivery is never automatically retried after an ambiguous result.**
  DATA 之后连接断开/超时/协议不明 → `UNKNOWN`；不得自动重发，也不得因为
  `NOT_FOUND` 就重发。唯一的重发路径是人工取消 action、推进 draft、重新 prepare 并重新 approve。
- **Sent-folder absence is not proof that a message was not sent.**
  `NOT_FOUND` 只记录审计行，execution 状态保持不变；`AMBIGUOUS`（两个 exact match）不得任选 UID；
  Sent 不可读 → `UNAVAILABLE`，同样不改变状态。
- **Mail sending can only occur through ActionExecutionService with an exact consumed human
  Approval.** SMTP capability 只注册在 `registered_action_executors(config)`，只有
  `pw action execute` 会经过它；发送前先跑纯 offline `executor.supports(action)`，不满足则
  `CapabilityUnavailable` 且**不消费 approval**。
- **Draft/model/background workers have no SMTP capability.**
  daemon / EventWorker / Scheduler / MailSync / MailAnalysis / MailEventHandler / MailDraftService /
  Interpreter / GroundedAnswer 都不得 import SMTP executor 或 mail-send services；
  `smtplib` 只允许出现在 `adapters/mail/smtp.py`。
- **eHall capabilities are registered typed pipelines, never a generic browser tool.**
  Playwright 只允许出现在 `adapters/ehall/`；application/ports/domain 不得出现 page/selector/click/
  goto/fill 之类标识符，也不得 import playwright。Port 只有 `inspect_form` 与 `submit_certificate`。
- **The first production eHall capability is certificate submission only.**
  唯一 action type 是 `ehall.submit-certificate`；drop-course / withdraw / cancel-application /
  delete / dorm checkout / arbitrary-submit 这类能力在代码里**不存在**（不是 prompt 禁止）。
- **eHall form values are explicit user input in this phase; no model or personal-fact autofill.**
  所有可写字段只来自 `--field KEY=VALUE`；禁止 Knowledge search / MailAnalysis / LLM /
  ConfirmedFact 参与填表。
- **Preparation must not mutate the remote form.**
  `pw ehall certificate prepare` 只做只读 inspect + 本地校验，不在网页里 fill，避免 autosave /
  远端校验在用户 approve 之前发生。
- **A page-contract change invalidates the prepared action.**
  fingerprint 覆盖 service identity / page markers / 有序字段定义（key,label,kind,required,options）/
  required materials / submit control；执行前必须重新 inspect 并要求 fingerprint 完全一致，否则
  `EHallPageChanged`，且**不输入任何值**、不提交。
- **The final submit click requires an exact ActionRequest human Approval.**
  唯一提交入口是 `pw action execute`；没有 `pw ehall submit`，adapter 内部也不得重复点击或重试。
- **Failures after the submit click are ambiguous unless a success/failure state is explicit, and
  must never be blindly retried.** click 之前失败 = 明确 FAILED（approval 已消费）；click 之后无法
  判定 = UNKNOWN，阻塞同一 action 的再次执行，并提示人工去 eHall 查看，绝不自动重发。
- **Mobile web is a same-LAN control plane, not a public service.**
  默认 `[mobile] enabled = false`；`bind` 只允许 `loopback` / `lan`（映射 127.0.0.1 / 0.0.0.0），
  没有 public hostname、没有 proxy trust、没有 CORS 列表、没有 TLS 例外、没有 cloud relay。
  LAN 模式下由 private-client middleware 按 **socket peer** 判断（loopback / private / link-local
  才放行），**绝不读 `X-Forwarded-For` / `Forwarded` / `X-Real-IP`**：伪造 header 不能绕过。
- **Web sessions and pairing tokens are stored only as hashes.**
  pairing token 一次性、TTL 600s，session TTL 30 天且可 revoke；DB 只存 SHA-256（带 `CHECK`
  约束），plaintext 只出现在创建它的那一次响应里。pairing code 由 `pw mobile pair` 打印一次，
  **不得放进 URL query / fragment**；approval link 的 token 只走 URL fragment（不发给 server），
  页面加载后立刻 `history.replaceState` 抹掉。日志默认 `access_log=False`，token、cookie、
  mail/draft 正文与 Action payload 一律不写日志。
- **All web mutations require an authenticated session plus CSRF protection.**
  session cookie（HttpOnly）+ `X-CSRF-Token` header + CSRF cookie 三者一致才允许 mutation；
  V1 是 LAN HTTP，因此**不得**虚假声称 `Secure` cookie。
- **Mobile approval may create Approval but may never execute an ActionRequest.**
  web 审批复用 `ApprovalService`（同一个 exact-fingerprint 语义），approve 成功即停止；
  `adapters/web/` 不得 import `ActionExecutionService`，也不得 import SMTP / eHall / Playwright /
  Sent lookup。**Action execution has no web endpoint**：路由表里不存在 execute / send / submit /
  retry / resend，也不存在 generic action-creation endpoint。手机只 review + approve。
- **User-controlled text must never be inserted as HTML.**
  所有用户内容（task title、mail subject/body、Action payload、notification）只能经 `textContent`
  写入 DOM；静态文件里禁止 `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `eval` /
  `new Function`，也禁止任何第三方 JS/CSS/font/CDN 引用（全部 self-contained）。
- **Mobile routes must use application services, not direct SQLite access.**
  `domain/mobile.py`、`application/mobile_auth.py`、`store/mobile_sessions.py` 与 `adapters/web/`
  不得 import store/adapters/model/executor/FastAPI 之外的框架；fastapi / starlette / uvicorn
  只允许出现在 `adapters/web/`。
- **Personal learning is candidate-first and human-confirmed.**
  学习永远从 durable candidate 开始，绝不从「模型的记忆」或行为推断开始；Phase 7A 里
  candidate 的唯一来源是用户显式写下的 `Correction`（`pw fact candidate add KEY VALUE --note ...`）。
- **FactCandidate is never equivalent to ConfirmedFact.**
  candidate 只是提案：不能被 autofill、不能进任何 model context、不能被任何消费者读取；
  只有 `ConfirmedFact` 才可能在未来被消费，而且必须满足 `Active` 定义。
- **Only active, unexpired ConfirmedFacts may be considered by future autofill logic.**
  `active = superseded_at IS NULL AND (valid_until IS NULL OR valid_until > now)`；
  expiry 是**读取期派生状态**，不排 job、不做后台 mutation。
- **No model or background worker may promote a FactCandidate.**
  `confirm_fact` / `confirm_candidate` 只能来自 `pw fact candidate confirm`（CLI → LearningService）；
  Interpreter / GroundedAnswer / MailAnalysis / MailEventHandler / MailDraftService /
  MailSyncService / EventWorker / Scheduler / mobile web adapter / ModelPort 路径都不得
  import 或调用 learning repository/service，也不得创建 candidate。
- **Every fact must retain durable provenance.**
  candidate 必须引用产生它的 `Correction`（FK，无 `ON DELETE`、无 delete 路径）；
  correction 文本按用户原话保存，只做 trim，不改写不摘要。
- **Superseding a fact preserves history.**
  确认同一 key 的新值在**同一个 transaction** 里 supersede 旧 current row（含已过期但未
  superseded 的行）再插入新值：`UNIQUE(fact_key) WHERE superseded_at IS NULL` 保证任一提交状态
  下最多一个 current fact；历史行永不覆盖、永不删除。confirm 只允许 `PENDING` 单次转换，
  失败必须整体 rollback（旧 fact 仍 current、candidate 仍 pending）。
- **The fact store must never be used for passwords, tokens, credentials or private keys.**
  key 中 credential-like 的完整 segment 一律 `ForbiddenFactKey`（`password` / `passwd` /
  `secret` / `token` / `credential(s)` / `api_key` / `apikey` / `private_key`，
  按 `.` 与 `_`/`-` 分词匹配，故 `mail.smtp_token` 也被拒绝）。**Fact store is not a
  credential store**：绝不扫描 value 猜测它「像不像密码」。
- **A successful external action never becomes a Playbook automatically.**
  成功的 `mail.send` / `ehall.submit-certificate` 不会自动产生任何学习记录；`playbook_candidates`
  只在用户显式运行 `pw playbook candidate add ACTION --name ... --note ...` 时增长（有真实
  integration regression 锁定）。
- **PlaybookCandidates require explicit human creation and review.**
  候选必须来自**明确 SUCCEEDED 的 ExecutionRun**（action 为 `EXECUTED`、payload fingerprint 重新校验通过、
  run 属于该 action 且 `finished_at` 存在），FAILED / UNKNOWN / RUNNING / 从未执行的 PREPARED 一律
  `PlaybookSourceNotEligible`；没有 replay validator 的 action type 在创建时就
  `PlaybookSourceUnsupported`（保证每个 pending candidate 都可被测试）。**Candidate 不是 Playbook。**
- **Playbook replay tests are side-effect-free validation only; they never invoke ActionExecutor.**
  dry-run 只做纯本地解析：`mail.send` 重新走 `MailSendPayload.from_payload`，
  `ehall.submit-certificate` 重新走 typed certificate parser。**不读凭据、不开 socket、不开浏览器、
  不查 Sent、不生成新 Message-ID、不调用 ActionExecutor**，也不需要凭据/浏览器可用即可 PASS。
  `PASS` 只意味着「当前代码仍理解这份 historical payload」，**不**意味着凭据有效、远端页面未变、
  收件人仍然正确或动作值得再做一次。
- **Promotion requires a current passing dry-run and explicit human action.**
  `promote_candidate` 在一个 transaction 内要求 candidate 仍 PENDING、存在 **当前 contract version
  + 精确 input fingerprint** 的 PASSED test，然后写入 Playbook 并记录转换；没有合格 PASS 则
  `PlaybookCandidateNotTested`。没有 `--force` / `--skip-test` / `--auto`，也没有自动晋升。
  replay contract version 变化即让旧 PASS 失效（旧 Playbook 保留其 promoting version，不自动重测/退休）。
- **Playbooks do not contain or grant Approval.**
  Playbook 不包含 Approval、不消费 Approval、不创建 `ActionRequest` / `ApprovalChallenge` / `Approval` /
  `ExecutionRun`，也不绕过任何既有审批要求；它只保存 provenance（source action / run / fingerprint /
  contract version / promotion test）。
- **Phase 7 Playbooks are non-executing reference blueprints.**
  没有 `pw playbook run|execute|apply|instantiate`，没有 `PlaybookExecutor` / `PlaybookRunner`，没有
  `to_action_request`，也没有 `${...}` / `{{...}}` 参数化模板：**Phase 7B 不做实例化、不做参数推断**。
  唯一状态转换是 `ACTIVE → RETIRED`，被拒绝的 candidate 与被退休的 Playbook 都永久保留为审计历史。
- **No model may create, test, promote, parameterize or execute a Playbook.**
  playbook 路径不 import `ModelPort` / `StructuredModel` / provider adapter；EventWorker / Scheduler /
  MailAnalysis / MailDispatch / ActionExecution / Interpreter / GroundedAnswer / mobile web 都不能
  import 或调用 playbook service。创建与晋升入口只有 `pw playbook` → `PlaybookService`。
- **Watcher URLs are explicit configuration, never model-generated.**
  `[[watchers.web]]` 的 `id`/`url` 是唯一来源：URL 只接受 `https://`、不允许 userinfo、不允许
  IP-literal host、不允许显式端口；模型/CLI/事件都不能指定 URL，也没有 `pw watch fetch <url>`。
- **Web watchers are public HTTPS read-only observers, not generic HTTP tools.**
  没有 cookie、没有认证、没有 JavaScript、没有浏览器、不跟随 redirect（3xx = `WebWatchRedirectNotAllowed`）；
  fetch 前解析 hostname 并要求**每个** resolved address 都是 public（`is_global`），
  否则 `WebWatchUnsafeAddress`。响应流式读取并在 `max_response_bytes` 硬停止；
  只接受 text/html、text/plain、application/json。
- **Initial watcher fetch establishes a baseline and does not emit a change event.**
  第一次成功 fetch 只写 snapshot + baseline observation + state（**不发事件**）；只有 normalized content
  hash 变化才产生 change observation 与一条 `web.page.changed`；configured URL 变化 = 重新建立 baseline。
- **HTTP validators are optimizations; periodic unconditional fetches preserve correctness.**
  `checks_since_full < full_fetch_every` 时才可发送 `If-None-Match`/`If-Modified-Since`；达到阈值即强制
  unconditional GET 并清零计数。永远 304 的服务器**不能**隐藏变化（有测试锁定）。
- **Web/manual content is untrusted data.**
  page 文本与粘贴文本都以 quoted JSON 数据进入 prompt，永不拼进 instructions；analysis context
  不附带 Knowledge / Facts / Tasks / Calendar / mail / Playbooks / Actions，web diff 也**不含 URL**。
- **Watcher/manual analysis may classify and extract candidates only; it must not mutate commitments, facts, playbooks or actions.**
  `ObservationInboundEventHandler` 只写 `observation_analyses`（event 状态由 EventWorker 推进）：
  不建 Task/Deadline/CalendarEvent/Case/ActionRequest/FactCandidate/PlaybookCandidate/Notification，
  也没有任何 import 能做到（architecture + no-mutation 测试锁定）。schema 里没有放 command/task/url/tool 的字段。
- **Manual input is persisted before EventInbox bridging.**
  `pw ingest text` 先写 `manual_inputs` 再幂等 ingest `manual.input.received`（`external_id =
  manual-input:<uuid>`，content 只含 `manual_input_id` 与 `source`）；两个 crash window 都由每轮 bounded
  repair 修复。source 只有 `manual` / `qq-forward` / `other` 三个名字，不含可执行语义。
- **MCP is a local adapter, not an autonomous runtime.** 它是 VS Code 启动的本地 stdio 子进程，
  不托管在 `assistantd` / `mobile-web`，不需要 daemon 运行，也不通过 localhost RPC 联系 daemon。
- **MCP is stdio-only in V1.** 没有 streamable-http / SSE / websocket / TCP / Unix listener；
  `growing-assistant-mcp` 最多接受 `--version`，`--transport http` 之类的参数直接报错退出。
  **MCP protocol stdout must contain protocol messages only**：日志一律走 stderr，server 内不得 `print`。
- **The default MCP surface is read-only.** 默认 `[mcp] enabled = false`；即便开启，
  `write_scope = "none"`、`expose_knowledge = false`，只注册 4 个 resource
  （`assistant://status|tasks/open|cases/open|plan/current`）与 2 个 read tool
  （`assistant_get_task` / `assistant_get_case`，均标 `readOnlyHint`）。
- **MCP task writes require explicit server-side write_scope=tasks.**
  只有 `write_scope = "tasks"` 时才注册 `assistant_create_task` / `assistant_complete_task`，
  且只能调用既有 `TaskService`（deadline reminder、commitment revision、rolling replan 语义照旧）。
  capability 未开启时工具**不存在**（客户端按名字调用得到 unknown tool），
  绝不依赖 VS Code 的确认框作为授权边界。没有 `force` / `bulk-complete` / `complete-all`。
- **MCP cannot create Approval or execute ActionRequest.**
  整个 `adapters/mcp/` 与 `application/mcp_facade.py` 不得 import/name `ApprovalService`、
  `create_challenge`、`approve`、`ActionExecutionService`、`ActionExecutor`，
  也不得暴露 SMTP/eHall/browser/Sent lookup：MCP client 永不成为“人类 Approval”。
- **MCP cannot confirm facts or promote playbooks.**
  两个模块都不得 import `LearningService` / `PlaybookService`，因此无法确认/拒绝 fact、
  无法创建/晋升/拒绝/退休 playbook —— Phase 7 的人类边界继续保持 CLI-only。
- **MCP never exposes generic filesystem, shell, HTTP or browser capabilities.**
  没有 SQLite 查询工具、没有文件读写、没有 shell/subprocess、没有任意 HTTP/browser tool，
  也不使用 MCP roots 访问 workspace 文件；没有 sampling / elicitation / prompts / MCP Apps。
- **Knowledge exposure through MCP is opt-in and bounded.**
  只有 `[mcp] expose_knowledge = true` 才注册 `assistant_search_knowledge`：本地 deterministic
  full-text search（不调用 `GroundedAnswerService` / `StructuredModel` / ModelPort / web search），
  输入 query ≤1000 / root_id 可选 / limit 1..8，输出只有 logical URI + source span + bounded excerpt
  （单条 ≤1200、总计 ≤6000），绝不含物理路径、挂载路径、rowid 或整篇文档；tool description 明确说明
  内容会被发送给连接的 MCP client/model。
- **Live SQLite backups must use the SQLite backup API, never filesystem copying.**
  运行中的 WAL 数据库只能用 `sqlite3.Connection.backup()` 取一致快照（`store/backup.py`）；
  禁止 `shutil.copy` / 手动拷贝 `assistant.db`+`-wal`+`-shm`，也禁止在 backup 里"补" live 之后的数据。
  backup 引用的 mail raw / web snapshot 一律按 DB 里记录的 SHA-256 逐个复核，缺文件或 hash 不符即整次失败。
- **Derived knowledge indexes are rebuildable and are not operational backup authorities.**
  归档只允许 4 类顶层成员（`manifest.json` / `runtime.sqlite3` / `mail/raw/…` / `web/snapshots/…`）；
  `cache/`、per-root FTS 索引、原始 vault 文件、eHall browser profile、`config.toml`、`.env`、
  凭据与日志一律不进归档，恢复后索引从原始 root 重建。
- **Restores must never preserve live authorization capability silently.**
  恢复在 finalization 事务里把未消费的 `ApprovalChallenge` 置 `consumed_at`、把未消费的 `Approval` 置
  `superseded_at`（绝不写 `consumed_at`，那等于声称它被使用过）；历史行永不删除，只失去"还能用"的属性。
- **Mobile sessions and unconsumed approval capabilities are invalidated on restore.**
  pairing token 置 `consumed_at`、所有 session 置 `revoked_at`；恢复后必须重新 `pw mobile pair`，
  且恢复不会带回任何凭据（SMTP/IMAP/DeepSeek key、eHall 登录态都由用户重新提供）。
- **RUNNING and UNKNOWN external executions survive recovery as unresolved audit state and must never
  be blindly retried.** 恢复不改写任何 `ExecutionRun` / `ActionRequest` 历史；`RUNNING` / `UNKNOWN`
  仍然挡住 `pw action execute`（restore 不是 reconciliation，也绝不声称外部副作用被回滚）。
- **Backup restore targets a new staging directory; no in-place restore exists.**
  `pw backup restore … --to DIR` 要求 DIR 不存在或为空，且不得是 active runtime data dir 的自身/父/子；
  先写同 parent 的临时目录，验证 + finalization 通过后原子 rename；没有 `--in-place` / `--force`。
- **Operational integrity checks are read-only and network-free.**
  `pw integrity check` 只用只读连接（不建库、不迁移、不修复），只报告 `integrity_check`、
  `foreign_key_check`、迁移状态与跨域一致性；pending migration 报 `PENDING`，offline vault 报 offline。
- **Cross-system acceptance tests must use fake external adapters with the socket guard enabled.**
  `tests/acceptance/` 只能用临时 XDG root + fake model/IMAP/SMTP/eHall/WebSource，真实 SQLite store、
  migration runner、EventWorker、Scheduler 与审批链必须是真件；禁止用 mock application service 代替。
- **assistantd is single-instance per runtime data root.**
  单实例的权威是 `flock(LOCK_EX|LOCK_NB)` 持有的 **OS advisory lock**
  （`~/.local/share/growing-assistant/assistantd.lock`，0600），不是"文件是否存在"、也不是 pid；
  第二个实例在启动任何 supervisor 之前非零退出。没有 OS lock 的旧 lock 文件**必须**允许启动。
  lock 在 `serve` 返回之后（`finally`）才释放；daemon 永远不执行 `ActionRequest`。
- **Release hardening must not widen capability surfaces.**
  9B 只允许 runtime hardening / packaging / 单实例 / 升级兼容 / 权限 / release acceptance / docs /
  release metadata。不得新增 ActionExecutor、外部集成、watcher 类型、MCP tool/resource、mobile mutation、
  model 能力、fact consumer、playbook 执行或自动 commitment/外部动作；`tests/release/test_capability_surface_freeze.py`
  与 architecture checks 把 mobile route / MCP surface / executor set / event types 都钉住。
- **Historical migrations are immutable.** 已发布的 `migrations/*.sql` 永不修改、改名或删除；
  迁移只向前。改历史迁移会被 `require_compatible_history()` 判定为 incompatible（name 不符）。
- **A database from a newer migration history must fail closed.**
  `DatabaseMigrationIncompatible`：bootstrap / `assistantd` / 任何会写的 `pw` 命令都必须拒绝；
  `pw integrity check`（只读）报告 `INCOMPATIBLE` + FAIL，且**不得**修改数据库。没有 downgrade migration。
- **Project-created personal-state files use restrictive permissions where supported.**
  本项目创建的目录 `0700`（runtime root / mail raw / web snapshot / restore staging / eHall profile /
  knowledge cache），文件 `0600`（runtime DB、raw mail、web snapshot、backup 及其临时文件、lock、恢复内容）。
  只处理"这次调用新建的"对象；既有文件与用户文件只报告不重写（`pw integrity check` 的 permissions 段）。
- **No release artifact may contain credentials or runtime personal data.**
  wheel/sdist 必须带 `assistant/migrations/0001–0015` 与 web 静态资源，但绝不含 `.env`、`assistant.db`、
  `mail/raw`、`web/snapshots`、eHall profile、backup 或任何 fixture secret；
  `tests/release/test_package_smoke.py` 在真正 build 出来的 artifact 上检查。
- **All v1 release tests are network-free.** `tests/release/` 与 `tests/acceptance/` 只允许 fake/in-process
  adapter；package smoke 安装 wheel 时用 `--no-deps --target`，不访问 provider 或包索引。
- **Playbooks remain non-executing.** Playbook 只是"当前代码仍理解这份 payload"的复核记录；
  没有执行入口，也不与 ActionRequest/Approval 打通。
- **ConfirmedFacts remain non-autofilling unless a future ADR explicitly changes that boundary.**
  没有 fact → eHall 字段 / mail 草稿 / model context 的注入路径；`pw integrity check` 只审计 provenance。
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

### 5.1 公开产品语言（Rings，post-v1）

- The public product name is **Rings**。
- **Tree** is the user-facing coordinator metaphor.
- **Roots**、**Seeds**、**Branches**、**Leaves** 与 **Rings** 是 product-language concepts；它们
  **不替代**上述冻结的领域术语。
- Do not rename `Task`、`Case`、`ActionRequest`、`Approval`、`ExecutionRun`、`FactCandidate`、
  `ConfirmedFact`、`Playbook` 或其它既有 domain type，只为了让命名贴合 tree 比喻。
- **Branches are capability modules, not autonomous sub-agents.**
- **Tree never bypasses capability, approval, fact-confirmation or playbook-review boundaries.**
- 这些词只用于 README、`docs/concepts/rings-language.md` 与对用户的解释：不在代码里新增
  `Seed` / `Ring` / `BranchAgent` / `SubAgent` / `AgentRegistry`，也不新增 table 或 migration。
- Compatibility 标识保持冻结，不因品牌改名：包名 `assistant`、distribution `growing-assistant`、
  `pw` / `assistantd` / `growing-assistant-mcp`、XDG 目录 `growing-assistant`、
  `GROWING_ASSISTANT_*` 环境变量、SQLite 表名、migration 名、event type、action type 与 MCP
  resource/tool 名。

## 6. Phase 现状

### 6.1 Tree Conversation Runtime 规则（Phase 10A，ADR-0033）

- Tree conversation 是**编排层**：handler 必须调用既有 application service，不得写 SQL、
  不得复制 Task / Planner 逻辑、不得建并行任务模型。
- 模型只能提出 `ConversationCapabilityRegistry` 里显式存在的 typed operation；
  操作集合是封闭的，新增操作 = 改 schema + registry + ADR，不是加一个字符串。
- 模型不接触 service、数据库、shell、filesystem、browser、HTTP、`Approval` 或 executor；
  没有 `ModelTool` / `ToolExecutor` / agent tool loop。
- 相对民用时间只依据显式配置的 `[planning].timezone`；缺失就提问，禁止用宿主机时区兜底。
- 对话中的陈述**不会**自动变成 `ConfirmedFact`；Fact confirmation 仍是人类流程。
- 外部动作（`mail.send`、eHall、`action.*`、`approval.*`）不进入对话能力集；要接入必须先写
  新的 ADR 并设计对话式审批。
- 本地写入必须先落 `APPLYING`，中断即 `UNKNOWN_LOCAL`，永不自动重放；对话日志不记录原始
  消息文本。

### 6.2 对话式外部动作规则（Phase 10B，ADR-0034）

- 模型只能"准备"外部动作：`mail.reply_draft` / `mail.prepare_reply_send`。`mail.send`、
  `approval.*`、`action.execute`、`execution.*` **不得**进入模型能力表。
- 审批链保持不变：`ActionRequest → Approval → ExecutionRun`；对话只是它的前端。
- 预览必须由 `ActionRequest` payload 渲染；不得让模型总结"将要发送什么"。
- 只有**人的原始消息**能触发确定性发送解析；`可以 / 好 / ok` 不是发送确认（那是本地计划的语汇）。
- challenge 明文只在一次确定性调用中存在：不落库、不进模型、不返回给调用方。
- `ConversationExternalReviewService` 不得依赖 `ModelPort` / interpreter / prompt；
  `ConversationInterpreter` 与 capability handler 不得依赖 approval / execution service。
- 任一 payload 变化（收件人、主题、正文、回复元数据、draft 版本）必须让 review 变 STALE，
  并重新准备一个新的 `ActionRequest`；旧指纹永不授权新内容。
- SMTP `UNKNOWN` 不自动重试；重启不自动执行；eHall 仍不在对话能力内。

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
仍然不发送、无 SMTP、无 EventWorker 自动起草。

**Phase 6A 已完成（仍是 0.4.0）**：Case + ActionRequest + Approval + ExecutionRun 安全基础
（ADR-0023）：durable `Case`（OPEN→COMPLETED/CANCELLED，无 reopen）、不可变 `ActionRequest`
（canonical JSON payload + SHA256 fingerprint + 加载时重新校验）、`ApprovalChallenge`（随机 token、
DB 只存 hash、TTL 600s、single-use）、`Approval`（exact fingerprint 绑定、一次消费、
`superseded_at` 保留历史、partial unique index 保证同一 action 同时只有一个有效 approval）、
`ExecutionRun`（RUNNING/SUCCEEDED/FAILED/UNKNOWN）、atomic begin-execution（消费 approval + 建
RUNNING 同 transaction）、并发 fencing、UNKNOWN/崩溃语义（不自动 retry）、`ActionExecutor` port +
**空的 production capability set**、`pw cases|case add|show|done|cancel` 与
`pw actions|action show|challenge|approve|execute|cancel`。仍然不发送、无 SMTP/eHall/browser。

**Phase 6C 已完成（v0.6.0）**：第一个白名单 eHall 事务 —— 证明书申请（ADR-0025）：
manual `pw ehall login` + persistent headed Chromium profile（XDG data，0700）+ NJU top-level
origin allowlist（ehall / ehallapp / authserver）、typed only pipeline（`inspect_form` /
`submit_certificate`；没有 generic browser API）、只读 inspect（service 精确匹配 + page marker）、
page-contract fingerprint（service / markers / 有序字段定义 / materials / submit control）、
显式 `--field KEY=VALUE` prepare（不在网页里填表）、immutable
`ActionRequest("ehall.submit-certificate")` + critical preview、既有
challenge / approve / execute 审批链、执行前重新校验 contract + 精确填写 + readback + 单次
whitelisted submit click、click 前 = FAILED / click 后不明 = UNKNOWN 且零自动重试、
`pw ehall login|status|certificate inspect|prepare|show`、doctor 检查 playwright + chromium。
无 migration 0012：durable audit state 仍由 Case / ActionRequest / Approval / ExecutionRun 承担。

**Phase 6B 已完成（v0.5.0）**：approved SMTP delivery + ambiguous-result reconciliation
（ADR-0024）：draft `needs_user_input` acknowledgement（`pw mail draft acknowledge`，编辑后重置）、
可选且必须完整有效的 SMTP config（starttls/ssl，无 plain/verify_tls=false）、
`GROWING_ASSISTANT_MAIL_<ID>_SMTP_PASSWORD` 环境凭据、immutable `MailSendPayload`
（canonical + 冻结的 From/To/Subject/body/Date/Message-ID/reply headers）、
prepare 时生成稳定 RFC Message-ID、`mail_send_links`（一个 draft version 最多一个 send action）、
`pw mail send prepare|show|reconcile` + `pw mail sends`、`mail.send` executor 的 offline
`supports` preflight（不满足不消费 approval）、显式 SMTP 阶段跟踪（pre-DATA 明确失败 = FAILED，
DATA 之后不明 = UNKNOWN）、零自动重发、只读 Sent 对账（FOUND 可把 UNKNOWN/RUNNING 提升为
SUCCEEDED，NOT_FOUND 不证明失败，AMBIGUOUS 不任选，UNAVAILABLE 保持原状）。
仍然不发送、无 SMTP / Approval / ActionRequest、无 EventWorker 自动起草。

**Phase 6D 已完成（仍是 0.6.0）**：同一局域网手机 Web 控制面（ADR-0026）：默认关闭的
`[mobile]`（`enabled` / `bind=loopback|lan` / `port`）、migration 0012（`mobile_pairing_tokens` /
`mobile_sessions`，只存 SHA-256）、`MobileAuthService`（pairing / session / CSRF / revoke，
token factory 在 composition root 注入）、`pw mobile status|pair|sessions|revoke|approval-link`、
FastAPI 只存在于 `adapters/web/`（/docs /redoc /openapi.json 关闭 + CSP 等安全 header）、
private-client middleware、dashboard / tasks create+complete / cases / notifications /
mail drafts 查看与 CAS 编辑 / actions 查看与 challenge+approve、
sessionless approval-link（fragment token，只 preview + approve）、daemon `mobile-web`
supervised service（crash 不影响 sibling，stop event 干净退出）。
**手机端没有任何执行入口**：无 SMTP、无 eHall、无 ActionExecutionService、无 ModelPort。

**Phase 7A 已完成（v0.7.0 的一部分）**：durable corrections + human-confirmed personal facts
（ADR-0027）：migration 0013（`corrections` / `fact_candidates` / `confirmed_facts`）、
`Correction` 与 `FactCandidate` / `ConfirmedFact` / `FactCandidateStatus` / `FactState`、
开放但受约束的 fact key 命名空间（`^[a-z][a-z0-9_.-]{0,127}$`，credential-like segment 一律
`ForbiddenFactKey`）、candidate + correction 同 transaction 写入（provenance 必须存在）、
`LearningRepository` + `SqliteLearningRepository`（candidate 写入、confirm/reject 单次状态转换）、
`LearningService`（`add_correction` / `propose_fact` / `confirm_fact` / `reject_fact` /
`list_candidates` / `list_facts` / `get_active_fact`）、confirm 在同一个 `BEGIN IMMEDIATE` 内
supersede 旧 current fact + 插入新 fact + 记录转换（partial unique index 保证最多一个 current key）、
expiry 为读取期派生（无 job）、历史永不删除、`pw corrections|correction add|show` 与
`pw fact candidates|candidate add|show|confirm|reject`、`pw facts|fact show`。
**本阶段不做 autofill、不把 fact 送进 model/mail/eHall、不自动产生 candidate、不做 Playbook。**

**Phase 7B 已完成（v0.7.0）**：successful execution → reviewed non-executing playbook（ADR-0028）：
migration 0014（`playbook_candidates` / `playbook_replay_tests` / `playbooks`）、
`PlaybookCandidate` / `PlaybookReplayTest` / `Playbook` + 三个状态枚举、
candidate 只能来自明确 SUCCEEDED 的 run（创建时重新读取 action/run、重新 hash payload、
要求 `EXECUTED` + `SUCCEEDED` + `finished_at`，否则 `PlaybookSourceNotEligible`）、
`UNIQUE(source_action_id)`（一次成功只能产生一个候选）、candidate 元数据不可编辑、
`PlaybookReplayValidator` port（`action_type` / `contract_version` / 纯 `validate`，**不是**
`ActionExecutor`）+ `PlaybookReplayRegistry`（只注册 `mail.send` 与 `ehall.submit-certificate`，
无动态 import）、两个纯 parser dry-run validator、bounded issue codes
（`payload-invalid` / `schema-version-unsupported` / `action-type-mismatch`）、
`replay_input_fingerprint`（candidate + source action/fingerprint/type + validator type + version）、
`PlaybookRepository` + `SqlitePlaybookRepository`（promote 单 transaction：PENDING + 精确 PASSED test
→ 插入 playbook + 记录转换）、`PlaybookService`（create / test / promote / reject / retire）、
`pw playbook candidates|candidate add|show|test|promote|reject` 与 `pw playbooks|playbook show|retire`。
**本阶段不做实例化、不做参数化、不执行、不创建 Approval，也无任何 model/后台参与。**

**Phase 8A 已完成（仍是 0.7.0）**：durable web watchers + manual inbound observation（ADR-0029）：
migration 0015（`web_watch_state` / `web_observations` / `web_observation_event_links` /
`manual_inputs` / `manual_input_event_links` / `observation_analyses`）、
`[watchers]` + `[[watchers.web]]` 配置（id grammar + HTTPS-only URL 校验）、
`WebSource` port + `HttpWebSource`（无 cookie/认证/JS/浏览器、不跟随 redirect、resolver 检查每个
resolved address 为 public、流式 byte cap、ETag/Last-Modified 仅作优化 + `full_fetch_every` 强制
unconditional）、stdlib HTML 抽取 + deterministic normalization + `sha256` 内容身份、
content-addressed snapshot store（`web/snapshots/<prefix>/<sha>.txt`）、baseline 语义（不发事件）、
change observation + 幂等 `web.page.changed` bridge（含两个 crash window 的 bounded repair）、
`ManualInput`（text/source/hash）+ `pw ingest text|list|show`（先持久化再 ingest
`manual.input.received`）、deterministic change context（difflib + 预算）与 bounded manual context、
closed analysis schema（category / summary / action_candidates，deadline ≠ event-start）、
`ObservationInboundEventHandler`（web + manual 两型，fingerprint 幂等复用，permanent vs retryable
model 错误分类）、`observation_analyses` durable 审计、daemon `web-watch` service（target 级隔离）、
worker 启动条件改为「有可用 model」、`pw watch targets|status|sync|observations|observation show`。
**本阶段无 Task/Case/Fact/Playbook/Action/Approval/Notification 自动创建，无 generic HTTP/browser
能力，无模型生成 URL。**

**Phase 8B 已完成（v0.8.0）**：controlled MCP / VS Code integration（ADR-0030）：
official MCP Python SDK v2（`mcp>=2,<3`，`from mcp.server import MCPServer`）、
`[mcp]` 配置（`enabled` / `write_scope=none|tasks` / `expose_knowledge`，默认全关）、
独立 console script `growing-assistant-mcp`（stdio-only，最多 `--version`，日志走 stderr，
disabled 时 stderr 报错且 stdout 为空）、`adapters/mcp/{server,resources,tools}.py`
（唯一 import MCP SDK 的包）、`application/mcp_facade.py`（不 import MCP SDK，只把既有
TaskService / CaseService / commitment 读模型 / planner week window / notification inbox /
可选 knowledge search 整理成 bounded DTO）、4 个 resource（status / tasks/open / cases/open /
plan/current）、2 个 read tool + 可选 `assistant_search_knowledge` +
可选 `assistant_create_task` / `assistant_complete_task`（仅 `TaskService`）、
typed tool error（`{"error":{"kind","message"}}`，不含 SQL/路径/stack trace）、
`pw mcp status` / `pw mcp vscode-config`（只打印 snippet，绝不写 `.vscode`）、
in-process contract tests + 真实 stdio subprocess integration test。
**本阶段无 migration（仍 0001–0015）、无 durable MCP state、无 Approval/Execution/Fact/Playbook
能力、无 filesystem/shell/HTTP/browser/sampling。**

**Phase 9A 已完成（仍是 0.8.0，未 tag）**：operational integrity + safe backup/restore
（ADR-0031）：`domain/backup.py`（固定 archive layout、canonical manifest、成员名/大小/压缩比
bounds）、`domain/integrity.py`（severity / section / report）、port
`RuntimeBackup` / `BackupArchive` / `IntegrityRepository` / `ContentObjectReader`、
`store/backup.py`（只用 `sqlite3.Connection.backup()` 取一致快照，backup DB 内引用对象逐个复核 hash；
restore finalization 单事务失效 challenge/approval/pairing/session）、`store/integrity.py`（只读跨域审计：
pragma、capability fingerprint、mail link、fact provenance、playbook provenance、observation lineage、
applied migrations）、`adapters/backup/archive.py`（zip 写入/校验/逐成员解压，拒绝 traversal / symlink /
duplicate / unlisted / oversized）、`application/backup_service.py`（create / verify / inspect /
staging-only restore）、`application/integrity_service.py`、`cli_ops.py`
（`pw integrity check`、`pw backup create|verify|inspect|restore --to`）、`Database.read_only()` 只读连接、
`pw doctor` 新增本地 operational 行（runtime 可写、mail raw root、web snapshot root、migration state）
并提示运行 `pw integrity check`，`tests/acceptance/` 新增 lifecycle / restart / lease / UNKNOWN /
supervisor / privacy-log 验收。**没有 migration 0016、没有新外部能力、没有新 CLI 副作用、
版本仍为 0.8.0。**

**Phase 9B 已完成（v1.0.0，Phase 9 结束）**：v1 release hardening（ADR-0032）：
daemon 单实例 OS lock（`adapters/runtime/instance_lock.py` + `pw daemon status`）、
私有权限（`adapters/runtime/permissions.py`，目录 0700 / 文件 0600，恢复与备份同样 0600）、
`.gab` 严格 EOF（尾部字节与 ZIP comment 一律拒绝）、`DatabaseMigrationIncompatible` +
`require_compatible_history()`（未来 schema fail closed；`pw integrity check` 报 INCOMPATIBLE）、
`tests/release/`（升级矩阵与每个 prefix、future DB、example config safe-by-default、daemon
子进程生命周期、权限、归档边界、wheel/sdist 内容与 installed smoke、CLI/version surface、
能力冻结、压力不变量、重启不执行 approved/UNKNOWN、fresh & historical acceptance、隐私扫描）、
wheel 内置 `assistant/migrations/` 与 web 静态资源、`pw --version` / `assistantd --version`、
README 快速开始 / 产品边界 / 已知限制 / WSL 启动示例、`docs/upgrade-to-v1.md`、
`docs/releases/1.0.0.md`。**无 migration 0016、无新 production dependency、无新能力。**

以下能力全部属于后续 Phase，尚未实现，不得在文档或回答里描述成已完成：

其它 eHall 事务（退课/撤销/删除/dorm checkout 等高风险能力**永不实现**）/ generic browser
agent / 公网部署与 cloud relay / VPN 集成 / 第三方登录 / 手机推送（APNs/FCM/Web Push）/
mail 线程回溯修复（parent 迟到不改写历史）/ mail 分类结果自动转 Task /
个人估时学习 / agent tool loop / 多工具只读编排 /
command execution boundary（`pw interpret --apply` 之类）/ 重复任务与重复事件 /
embedding 与向量检索 / OCR / Office 文档与压缩包展开 / filesystem watcher 快速路径 /
移动端执行动作（手机永不执行，执行只在 host 上 `pw action execute`）/
approval token 之外的动作审批扩展 / Windows Task Scheduler 配置 /
把 Playbook 实例化成新的 `ActionRequest`（Phase 7B 明确不做）/ playbook 参数化与模板推断 /
workflow 自动晋升 / 用已确认 fact 自动填 eHall 表单或自动注入 model/mail context（另行评审）/
把 web/manual analysis 的候选自动转成 Task/Case/Action / QQ 协议/客户端自动接入 /
需要登录、需要 JavaScript 渲染或使用非默认端口的站点（Phase 8A 只观察公开 HTTPS 页面）。
（operational hardening 只做**显式**的本地备份/恢复：没有自动备份、没有后台备份 daemon、
没有云端备份或同步，也没有"就地恢复/强制覆盖"路径。）

（site watcher 与 QQ 转发文本的**入口**已在 Phase 8A 实现：`[watchers]` + `pw watch`、
`pw ingest text --source qq-forward`；上一条列的是它们的自动化与扩展部分。）

（Task/Deadline/CalendarEvent/PlanBlock/WorkSession、PlanProposal、ScheduledJob 与
Notification 已实现；Case、Approval 等其余 domain entity 仍属后续 Phase。）

新增能力前先确认它属于哪个 Phase，并在 spec 或 ADR 里落了设计再动手。

### 6.3 对话可靠性规则（Phase 10C，ADR-0035）

- `rings` 是产品面：可恢复的输入/模型/应用错误必须是"一句话 + 继续"，绝不 traceback。
- 终端输入必须经 `ConsoleInput` 严格解码；解不出来的字节 → 不建 turn、不调模型、不改状态，提示重来。
- 模型输出是不可信输入：良性缺失可以补齐（null → 空列表等），未知操作/坏参数/歧义引用一律 fail closed。
- 只允许**一次**有界修复调用（无副作用、无 tool loop）；第二次失败 → 明确告知且零执行。
- 普通 UI 不得出现 jsonschema / Traceback / required property 等内部字串；细节只在 `RINGS_DEBUG=1`。
- 能力描述必须来自运行时元数据（registry + config）：`system.capabilities`、`/help`、模型上下文同源。
- 已配置账号（`mail.accounts`）与本地邮件数量是两个概念，不得混答；账号信息绝不含凭证。
- 多操作计划必须先整体预检再执行第一个变更；不合法则整轮零变更并说明原因。
- SMTP `UNKNOWN`、`ActionRequest`/`Approval`/`ExecutionRun` 与"确认发送"语义保持冻结。

### 6.4 每周固定安排规则（Phase 10D，ADR-0036）

- `RecurringCalendarRule` 是权威日历状态，**不是** `ScheduledJob`，也不是一堆 `CalendarEvent`；
  occurrence 一律按需派生，不物化、不缓存、不写入 `calendar_events`。
- 一条规则 = 一个星期几 + 本地起止时间 + 显式 IANA 时区（+ 可选 `starts_on` / `ends_on`）；
  「周一和周三」是两条规则，没有 weekday list、没有 recurrence string、没有 `rrule` 依赖。
- 新建规则的时区默认取 `[planning].timezone`；缺失时提问。**绝不**使用 `datetime.now().astimezone()`、
  `TZ` 或隐式 UTC 作为语义来源。
- v1.1 只支持 WEEKLY、同日、不过夜。单双周 / 每两周 / 每月 / 每年 / 节假日例外 / 考试周例外 /
  外部日历同步一律拒绝（`UNSUPPORTED_SEMANTICS`，零变更），不做近似。
- 陈述句（「我每周一十点到十二点有课。」）不是写入指令：必须复用现有本地确认机制先问再写；
  「记下来 / 记录一下 / 加到日历 / 放进固定安排」才是写入指令。不新增第二套审批框架。
- 编辑保留规则身份（同 id、同 `created_at`、新 fingerprint），只改变未来；retire 停止未来 occurrence，
  不物理删除；历史 `WorkSession` 与已应用计划永不改写。
- Planner 把派生 occurrence 当作 busy time（与 `CalendarEvent`、手工 `PlanBlock` 合并去重）；
  模型**永不**直接创建 `PlanBlock`。
- 每周规则是本地写入：不产生 `ActionRequest` / `Approval` / `ExecutionRun`，不新增外部能力。
- 对话层不写 SQL、不拥有第二套规则模型：handlers 只通过 `RecurringCalendarService` 访问；
  能力描述（`/help`、`system.capabilities`、模型上下文）继续同源于 registry + config。

### 6.5 对话式新建邮件与联系人规则（Phase 10E，ADR-0037）

- 新邮件与回复**收敛到同一条管道**：同一个 `ActionRequest("mail.send")`、同一张 `mail_send_links`、
  同一个 executor、同一个 review/approval/execution/对账路径。绝不新增第二个 SMTP 通道。
- 模型可以写 subject/body，**绝不**授权发送、绝不调用 SMTP、绝不创建 approval/execution。
- 首轮只能准备：展示从 `ActionRequest` payload 渲染的精确预览；第二条明确的人类消息才可能发送。
- 发送语汇沿用 ADR-0034：只有「确认发送 / 发送 / 发吧 / 确认发出 / send / confirm send」能结算；
  「可以 / 好 / 嗯 / continue / ok」对本地计划有效，**永不**发送邮件。
- 收件人只有三种来源：用户本条消息里出现的地址、唯一一个 ACTIVE `Contact`、「我自己」对应的已配置
  send-ready 账号。模型提出的地址必须出现在用户原文中，否则第一次写入前拒绝（零草稿/零 action/零 review）。
- 名字查不到、同名多个、可发信账号多个一律追问；「我自己」不看本地邮件数量，也不默认取第一个账号。
- `Contact` 是本地结构化身份，不是 `ConfirmedFact`；只存 display_name/email_address/email_key/status/
  fingerprint/时间戳，不存凭证、密码、token、邮件正文或对话文本。idempotent（同 fingerprint 一条）。
- 回复草稿（`mail_drafts`）保持「必须有来源邮件」；新邮件草稿（`new_mail_drafts`）独立成表，二者在执行前收敛。
- `MailSendPayload` 用闭合判别 `kind`（reply/new），new 不带回复头；历史 payload 保持有效。
- 编辑已预览的草稿 → 旧 review STALE + 新 version + 新 fingerprint + 重新确认；
  联系人地址变化**不会**改写已准备好的 action。
- SMTP `UNKNOWN` 语义不变、永不盲重试；取消 review = 零 approval / 零 execution / 零 SMTP。
- 本阶段不做附件、定时发送、自动发送、通讯录或网络查询收件人，也不把 eHall/fact/playbook 引入对话。

## 7. 版本管理

- 使用 Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:`）。
- 不重写历史、不强推、不删除用户已有提交。
- `uv.lock` 必须入库；不要提交其它锁文件。
- **已发布到 GitHub**：`origin` = `ssh://git@ssh.github.com:443/Mr-tree013/Rings.git`，本地 `main`
  与 `origin/main` 同步，`v1.0.0` 与全部历史 tag 已推送。只做用户明确要求的推送；不要
  `git push --force`、不要改写或重建已发布的 tag、不要擅自替换既有 remote
  （步骤见 `docs/ops/remote-upload.md`）。
- 阶段收尾必须：工作树 clean、tag 已打且信息明确、提交里不含 `*.db`/`*.sqlite3`/`secrets/`/
  `state/`/个人 Vault；并定期在仓库外做 `git bundle` 备份。
- 已发布的 tag（`v0.0.1`、`v0.1.0`、`v0.2.0`）永不改写，也不要在其上追加提交。
