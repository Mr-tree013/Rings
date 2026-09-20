# Rings

一个与你一同成长的、**local-first** 的个人运营系统。

让每天的叶子，长成年轮。

[![CI](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml/badge.svg)](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml)

**语言：** 中文 ｜ [English](README_en.md)

**版本：** 1.0.0 · **参考运行环境：** Linux / WSL + Python 3.13

Rings 是一个 local-first 的个人运营系统。它运行在你的机器上，将状态保存在一个 SQLite 数据库以及该数据库所引用的文件中；只有通过你明确配置的 integrations，它才会与外部世界交互。

它会观察每天到来的信息——邮件、公开通知、你转发的文本——把你明确表达的意图转化为 commitments 和 plans，准备边界清晰的 external actions，并保留经过 review、会随着时间持续积累的历史记录。

Rings 不是一个通用的 autonomous computer agent。它没有 shell、没有通用文件系统控制、没有通用浏览器，也没有通用 HTTP client；任何生产环境中的外部副作用，都必须等待一次针对**精确动作**的人类 `Approval`。

## Tree Model

| 概念 | 含义 |
| --- | --- |
| **Roots** | 长期、可追溯的个人真实来源：你建立索引的文档，以及由你确认并保留 provenance 的事实。 |
| **Seeds** | 你主动种下的意图与 commitments：tasks、deadlines、calendar events 和 planning intent。 |
| **Branches** | Tree 可以调用的、受控的能力领域。Branches 是 capability modules，而不是 autonomous sub-agents。 |
| **Leaves** | 每天进入系统的信息：邮件、observations、粘贴或转发的文本。Leaf 可以被解释，但不会自动变成 commitment 或 action。 |
| **Rings** | 随时间累积的成长记录：work sessions、executions、fact history 和经过 review 的 playbooks。 |
| **Tree** | 真正与你交互的协调者，你可以通过 CLI、mobile page 和编辑器与它沟通。它不是一个不受限制的 autonomous agent。 |

Tree 协调这一切，但永远不会绕过 `ActionRequest`、人类 `Approval`、`ExecutionRun`、fact confirmation、playbook review 或 capability registry。完整的概念说明见 [docs/concepts/rings-language.md](https://github.com/Mr-tree013/Rings/blob/main/docs/concepts/rings-language.md)。

## Rings 能做什么

| Branch | 能力 |
| --- | --- |
| **Planning** | 管理 Tasks、deadlines、calendar events、work sessions，以及由你 review 并应用的 deterministic weekly proposals。 |
| **Knowledge** | 为你配置的本地和 vault Roots 建立索引，并只基于这些来源进行带引用的回答。 |
| **Mail** | IMAP ingestion、deterministic threads、bounded analysis、本地 reply drafts，以及经过明确 Approval 的 SMTP delivery。 |
| **Observation** | 观察你配置的公开 HTTPS 页面，以及手动输入和 QQ 转发内容。 |
| **Actions** | 精确的 `ActionRequest` → 人类 `Approval` → `ExecutionRun`，并绑定不可变的 payload fingerprint。 |
| **eHall** | 一个范围严格受限、需要 Approval 的 NJU certificate workflow；没有通用 browser automation。 |
| **Mobile** | 在可信 LAN 内提供 review 和 Approval 界面。 |
| **Learning** | `Correction` → `FactCandidate` → `ConfirmedFact` 的人工确认流程，以及经过 review、不可执行的 Playbooks。 |
| **MCP** | 面向 VS Code 的、受控的本地 stdio integration。 |
| **Operations** | Integrity check、backup、verify 和 staging restore。 |

## 安全设计

```text
ActionRequest
→ exact payload fingerprint
→ human Approval
→ ExecutionRun
```

- Model 不能批准 external actions，background worker 也不能；任何后台服务都没有创建 `Approval` 的路径。
- `Approval` 只绑定一个精确 payload，只能使用一次，并且会在执行开始前被消费。
- 修改 payload 后，必须重新创建 `ActionRequest` 和新的 `Approval`。
- 对外部系统产生的模糊结果不会盲目重试；它们会保持 unresolved，直到由人处理。
- Mobile 可以批准，但不能执行；eHall submission 与邮件发送使用同样的 Approval chain。
- Playbooks 是经过 review 的参考，而不是可执行 workflow；v1 中 `ConfirmedFact` 也不会自动填入任何地方。

完整边界说明见 [docs/specs/0001-system-design.md](https://github.com/Mr-tree013/Rings/blob/main/docs/specs/0001-system-design.md) 和 [Architecture Decision Records](https://github.com/Mr-tree013/Rings/blob/main/docs/adr)。

## 快速开始

需要 Python 3.13、[uv](https://docs.astral.sh/uv/) 以及 Linux 或 WSL。

```bash
git clone https://github.com/Mr-tree013/Rings.git
cd Rings

uv sync --frozen

mkdir -p ~/.config/growing-assistant
cp docs/examples/config.toml \
  ~/.config/growing-assistant/config.toml

uv run pw doctor
uv run pw integrity check
uv run assistantd
```

然后在第二个终端中运行：

```bash
uv run rings
uv run pw status      # 高级接口：状态总览
```

日常入口是对话，不是命令：

```text
Tree > 你好，我是 Tree。今天想安排什么？
You > 明天下午三点提醒我交软件工程报告
Tree > 已创建任务「交软件工程报告」，截止 2026-09-21 15:00（+08:00）。
You > 帮我安排一下这周
Tree > 周计划提案（3 个时间块）。要应用这个计划吗？回复「可以」我就写进计划。
You > 可以
Tree > 已应用周计划提案：新增 3 个时间块，替换 0 个。
```

Tree 用自然语言处理本地日常：任务、日历、工作记录、周计划和资料问答。外部动作（发邮件、
eHall 提交）**不会**从对话里执行，它们仍然必须走 `pw` 的审批链。

| 入口 | 用途 |
| --- | --- |
| `uv run rings` | 对话式主入口，日常使用 |
| `uv run pw chat` | 进入同一个对话运行时 |
| `pw …` | 高级 / 管理员接口：完整命令集 |
| `assistantd` | 后台运行时：索引、提醒、邮件、监控、手机页 |
| `growing-assistant-mcp` | 编辑器集成 |

示例配置在你主动修改之前不会启用任何外部能力：Mail、eHall、Mobile 和 MCP 默认关闭，也没有配置任何 watcher。

Credentials 通过环境变量提供，不写入配置文件：

- `DEEPSEEK_API_KEY`
- `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD`
- `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD`

`uv run pw mail accounts` 和 `uv run pw model status` 可以显示当前主机能够看到的配置状态，但不会打印 secret。

完整安装流程、全部配置字段、credential 规则和 WSL 启动方式见 [docs/guides/getting-started.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/getting-started.md)。

## 常用示例

创建一个 Task 并查看列表：

```bash
uv run pw task add "Write the SE lab report" --estimate 300 --deadline 2026-10-20T23:59:00+08:00
uv run pw tasks
```

把你从其他地方收到的信息转发给 Rings：

```bash
uv run pw ingest text "Forwarded notice..." --source qq-forward
```

检查并同步邮件：

```bash
uv run pw mail status
uv run pw mail sync
```

查看被监控页面发生了什么变化：

```bash
uv run pw watch observations
```

检查系统并创建备份：

```bash
uv run pw status
uv run pw integrity check
uv run pw backup create ~/assistant-backup.gab
uv run pw backup verify ~/assistant-backup.gab
```

完整的 Mail Approval chain、eHall、Mobile pairing、Facts 和 Playbooks 使用方式，请查看下面的 guides。

## 文档

| 主题 | 文档 |
| --- | --- |
| 对话式主入口：Tree Conversation | [docs/guides/conversation.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/conversation.md) |
| 快速上手、配置、Credentials、WSL 启动 | [docs/guides/getting-started.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/getting-started.md) |
| Rings / Tree 概念体系 | [docs/concepts/rings-language.md](https://github.com/Mr-tree013/Rings/blob/main/docs/concepts/rings-language.md) |
| Tasks、Calendar、Work Sessions、Weekly Planning | [docs/guides/planning.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/planning.md) |
| Knowledge indexing 与 grounded answers | [docs/guides/knowledge.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/knowledge.md) |
| Mail：同步、分析、Drafts、Approved Sending | [docs/guides/mail.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/mail.md) |
| eHall certificate application | [docs/guides/ehall.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/ehall.md) |
| Mobile control plane | [docs/guides/mobile.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/mobile.md) |
| Watchers 与手动 / QQ 转发输入 | [docs/guides/watchers-and-manual-input.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/watchers-and-manual-input.md) |
| Facts 与 Playbooks | [docs/guides/learning.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/learning.md) |
| MCP / VS Code | [docs/guides/mcp.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/mcp.md) |
| Integrity、Backup 与 Recovery | [docs/guides/backup-and-recovery.md](https://github.com/Mr-tree013/Rings/blob/main/docs/guides/backup-and-recovery.md) |
| 升级旧 runtime | [docs/upgrade-to-v1.md](https://github.com/Mr-tree013/Rings/blob/main/docs/upgrade-to-v1.md) |
| 系统架构 | [docs/specs/0001-system-design.md](https://github.com/Mr-tree013/Rings/blob/main/docs/specs/0001-system-design.md) |
| Architecture Decisions | [docs/adr/](https://github.com/Mr-tree013/Rings/blob/main/docs/adr) |
| Release Notes（当前版本：v1.0.0） | [docs/releases/1.0.0.md](https://github.com/Mr-tree013/Rings/blob/main/docs/releases/1.0.0.md) |
| Contributing 规则 | [CONTRIBUTING.md](https://github.com/Mr-tree013/Rings/blob/main/CONTRIBUTING.md) |
| Security Policy | [SECURITY.md](https://github.com/Mr-tree013/Rings/blob/main/SECURITY.md) |

## 架构概览

```text
Interaction
    ↓
Observation
    ↓
Intelligence
    ↓
Domain
    ↓
Execution
    ↓
Data
```

**Modular Monolith · Event Driven · Ports & Adapters · SQLite durable state · Explicit state machines**

一个 `assistantd` daemon 负责监管彼此隔离的 services；CLI、LAN Mobile 页面和本地 MCP server 都是在同一份 durable state 之上的 interaction surfaces。Model provider、mail transport、browser、web surface 和 clock 都位于 ports 之后；ModelPort 没有 tool calls、没有 filesystem 权限，也不能访问 database。

Rings 是公开的项目名称；为了保持 v1 compatibility，历史 identifiers 不变：Python package 仍叫 `assistant`，distribution 仍叫 `growing-assistant`；`pw`、`assistantd`、`growing-assistant-mcp` 这些 entry points、XDG 中的 `growing-assistant` 目录，以及 `GROWING_ASSISTANT_*` 环境变量都保持不变。

更多内容见 [docs/specs/0001-system-design.md](https://github.com/Mr-tree013/Rings/blob/main/docs/specs/0001-system-design.md) 和 [docs/adr/](https://github.com/Mr-tree013/Rings/blob/main/docs/adr)。

## 已知限制

- Mobile 是可信 LAN 内的 HTTP 页面，不是面向公网的服务。
- SMTP 不保证 exactly-once；发送过程中断时，状态会变为 `UNKNOWN`。
- eHall 页面发生变化时会 fail closed，而不是自动适配。
- eHall 的 `UNKNOWN` 状态需要人工检查。
- Watchers 只观察公开、无需身份验证的 HTTPS 页面。
- Knowledge retrieval 使用本地 FTS5，而不是 vector database。
- v1 中 `ConfirmedFact` 不会自动填入任何地方。
- Playbooks 不会执行。
- MCP 只支持本地 stdio，并且默认 read-only。
- Backup 不包含 credentials，也不包含 eHall browser session。
- Linux / WSL 是参考运行环境。
- 不支持 database downgrade。

## 开发

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Tests 在设计上不访问网络：一个 autouse guard 会让任何 socket 打开尝试直接失败。

每个 Pull Request 和每次 push 到 `main`，都会通过 GitHub Actions 运行仓库的质量门（[`.github/workflows/ci.yml`](https://github.com/Mr-tree013/Rings/blob/main/.github/workflows/ci.yml)）。更多贡献规则见 [CONTRIBUTING.md](https://github.com/Mr-tree013/Rings/blob/main/CONTRIBUTING.md)。

## License

目前尚未选择 License。在添加 License 之前，本仓库没有授权他人对代码进行复用。
