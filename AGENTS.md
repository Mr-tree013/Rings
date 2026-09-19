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

当前为 **Phase 1B 已完成**：在 1A 的 SQLite 持久层（`store/db.py`、`store/migrations.py`、
`store/events.py`）、`InboundEvent` 领域模型与状态机、`EventRepository` 端口之上，
新增异步持久层边界（ADR-0009）与 `EventInbox` 幂等摄取入口（`application/event_inbox.py`）。

以下能力全部属于后续 Phase，尚未实现，不得在文档或回答里描述成已完成：

Event worker/processor（claim、retry、crash recovery）/ IMAP / SMTP / DeepSeek / FTS5 /
Vault 扫描 / Web Server / eHall /
Playwright / scheduler / approval token / Task、Case、Approval 等其余 domain entity /
Windows Task Scheduler 配置。

新增能力前先确认它属于哪个 Phase，并在 spec 或 ADR 里落了设计再动手。

## 7. 版本管理

- 使用 Conventional Commits（`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:`）。
- 不重写历史、不强推、不删除用户已有提交。
- `uv.lock` 必须入库；不要提交其它锁文件。
