# growing-assistant

一个会成长的个人助手：把邮件、个人资料、办事大厅和手机端连成一条可审计的闭环，并把每次成功的
流程与你的纠正沉淀成可读、可改、可测试的规则。

## 当前状态：Phase 2 完成（v0.2.0，personal knowledge + continuous storage indexing）

已完成：

- durable event core（v0.1.0）：领域模型与状态机、SQLite 迁移、数据库级幂等去重、
  `EventInbox`、带 lease + fencing 的原子 claim、retry/backoff、crash recovery、dead letter。
- 个人知识（v0.2.0）：稳定存储身份（`local://` / `vault://`）、metadata catalog、
  文本/PDF 正文抽取与 SHA-256、per-root FTS5（trigram）索引、带 `page`/`lines` 定位的检索。
- 持续索引（v0.2.0）：`~/.config/growing-assistant/config.toml` 配置 Local/Vault roots，
  `assistantd` 周期性 reconciliation（scan → catalog → index），root 失败隔离与 supervisor。

**尚未实现任何外部集成**：没有收发邮件、没有 eHall/Playwright、没有模型调用、没有 Web UI、
没有 Task/Planner。`assistantd` 现在会在启动时执行一次存储 reconciliation，然后按配置周期重复；
它仍然**不会**启动 durable EventWorker——没有真实业务 handler 时，假 handler 只会制造"已经实现"
的错觉。

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
区分且可追溯，stale proposal 永不应用。**尚未实现**：automatic replanning、reminders、
个人估时学习（personal effort learning）、LLM 任务解读、自然语言时间解析、重复任务/事件、
RAG 问答、embedding/向量检索、OCR、Office 文档与压缩包、filesystem watcher。

## Architecture summary

Architecture v1 是 **Modular Monolith + asyncio daemon + explicit state machines + SQLite**：

```text
smail
  ↓  IMAP incremental fetch (UIDVALIDITY + UID)
InboundEvent → classification → Case → knowledge search → material checklist
  → draft/prepare → ActionRequest → approval → execute → result → archive
  → PlaybookCandidate → review/test → Playbook
```

进程模型：一个长期运行的 `assistantd`（Python 3.13 + asyncio），未来内部承载四类长期服务，
每类都有独立异常边界，单个服务失败不得拖垮整个 daemon。另有 CLI `pw`。Windows Task Scheduler
未来只在登录时负责拉起 WSL 内的 `assistantd`。

四条不可破坏的安全约束（详见 spec 与 ADR）：

1. 幂等由代码与持久状态保证，模型不参与幂等判断。
2. 任何外部副作用必须绑定具体 `ActionRequest` 的人工 `Approval`（approval 绑定 action fingerprint，
   内容变化即失效；runtime 没有自行批准的代码路径）。
3. 事实区分 `FactCandidate` 与 `ConfirmedFact`；自动填表只允许使用已确认、未过期、来源可追溯、
   且被目标字段许可的事实。
4. 高风险能力（退课、撤销申请、退宿等）在代码能力集合中物理不存在，不靠 prompt 禁止。

运行时存储：标准库 `sqlite3` 直连，SQL 全部收在 `store/` 包内，领域与应用层不执行 SQL、
不 import 数据库驱动（ADR-0003、ADR-0008）。Repository 端口是 async 的，阻塞的 sqlite3
调用只在 worker thread 内执行，connection 不跨线程（ADR-0009）。

外部输入统一经 `EventInbox` 摄取为持久化的 `RECEIVED` 事件：重复的 `(source, external_id)`
是幂等成功（返回 `DUPLICATE`）而不是错误。**尚无事件处理 worker**——claim、retry、崩溃恢复
是后续 Phase 单独设计的内容。

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
