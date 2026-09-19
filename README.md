# growing-assistant

一个会成长的个人助手：把邮件、个人资料、办事大厅和手机端连成一条可审计的闭环，并把每次成功的
流程与你的纠正沉淀成可读、可改、可测试的规则。

## 当前状态：Phase 0（工程初始化）

Phase 0 只交付工程骨架、架构文档与版本管理。**当前不存在任何真实能力**：

- 没有收信/发信（IMAP 与 SMTP 均未实现），没有邮箱凭据
- 没有 eHall 操作，没有 Playwright，没有浏览器自动化
- 没有模型调用（无 DeepSeek / OpenAI 适配器），没有 API key
- 没有个人资料索引（无 FTS5，无 Vault 扫描，无文件抽取）
- 没有 Web UI，没有审批令牌，没有调度器
- `assistantd` 只负责启动与优雅退出，四类长期服务（Mail / Index / Web / Scheduler）均为空

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
冷数据引用使用稳定逻辑 URI（如 `vault://archive-main/...`），不使用 `/mnt/e/...` 或盘符作为持久 ID。

## Repository layout

```text
src/assistant/{domain,application,ports,store,adapters}/   分层包（Phase 0 仅占位）
docs/specs/                                                架构设计文档
docs/adr/                                                  架构决策记录
rules/ playbooks/ memory/ evals/ prompts/ migrations/      规则、流程、记忆、评测、提示词、迁移
tests/{unit,integration,contract,regression,e2e}/          测试分层
```
