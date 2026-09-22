# Rings

一个与你一同成长的、**local-first** 的个人运营系统。

让每天的叶子，长成年轮。

[![CI](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml/badge.svg)](https://github.com/Mr-tree013/Rings/actions/workflows/ci.yml)

**语言：** 中文 ｜ [English](README_en.md)

**版本：** 1.4.0 · **参考运行环境：** Linux / WSL + Python 3.13

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
| **Tree** | 真正与你交互的协调者，你可以通过浏览器对话页、终端、mobile page 和编辑器与它沟通。它不是一个不受限制的 autonomous agent。 |

Tree 协调这一切，但永远不会绕过 `ActionRequest`、人类 `Approval`、`ExecutionRun`、fact confirmation、playbook review 或 capability registry。完整的概念说明见 [docs/concepts/rings-language.md](https://github.com/Mr-tree013/Rings/blob/main/docs/concepts/rings-language.md)。

## Rings 能做什么

| Branch | 能力 |
| --- | --- |
| **Planning** | 管理 Tasks、deadlines、calendar events、work sessions，以及由你 review 并应用的 deterministic weekly proposals。 |
| **Knowledge** | 为你配置的本地和 vault Roots 建立索引，并只基于这些来源进行带引用的回答。 |
| **Mail** | IMAP ingestion、deterministic threads、bounded analysis、本地 reply drafts，以及经过明确 Approval 的 SMTP delivery。 |
| **Observation** | 观察你配置的公开 HTTPS 页面，以及手动输入和 QQ 转发内容。 |
| **Actions** | 精确的 `ActionRequest` → 人类 `Approval` → `ExecutionRun`，并绑定不可变的 payload fingerprint。 |
| **eHall** | 一个范围严格受限、需要 Approval 的 NJU certificate workflow（可以用 `ehall.status` 读表单、用 `ehall.certificate.prepare` 准备申请）；没有通用 browser automation。 |
| **Chat** | 浏览器里的 Tree 对话页：对话历史、消息队列、进度提示、结构化确认卡片。 |
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
uv run rings up        # 起服务（或复用）+ 自动配对 + 打开 /chat
uv run rings down      # 优雅停止
uv run rings autostart install   # 可选：Windows 登录时自动常驻（remove 撤销）
```

凭据（邮箱应用专用密码、模型 key）可以放一份属于你自己的 `~/.config/growing-assistant/secrets.env`
（`0600`，环境变量优先，项目从不写它也不打印它的值）。细节见
[docs/guides/getting-started.md](docs/guides/getting-started.md)。终端对话仍是 `uv run rings`。

## 浏览器对话（Tree Web Chat）

日常最顺手的入口是浏览器里的 Tree 对话页。它由 `assistantd` 提供，和终端共用同一套对话运行时、
同一份对话数据库和同一套确认语义。

```bash
# config.toml 里启用网页控制面
[mobile]
enabled = true
bind = "loopback"     # 只在本机；"lan" 则在同一可信局域网内可达
port = 8791
```

然后打开 `http://127.0.0.1:8791/chat`，或直接让 `rings` 帮你打开：

```bash
uv run rings --web    # 打开 /chat；控制面没在运行时会告诉你该怎么启动
uv run rings          # 默认仍然是终端对话，行为与 v1.1 一致
```

浏览器里可以做的事：看完整对话历史、切换会话或开一个新对话、在 Tree 还在工作时继续输入
（新消息会排队，并且各自成为一条独立消息）、看到 Tree 当前在做什么（"正在理解你的请求…"
这类粗粒度状态，不会看到模型推理过程）、以及用卡片确认需要你决定的事情。

需要你确认的事情会以卡片出现，点按钮就等于在终端里说出那句话：

| 卡片 | 按钮 |
| --- | --- |
| 邮件发送预览（就是实际会发出的内容） | 确认发送 / 取消 |
| 周计划 | 应用计划 / 取消 |
| 固定安排 | 保存固定安排 / 取消 |
| 长期信息 | 确认记住 / 不要记 |

卡片按钮不经过模型：它直接结算那张卡片指的那一件事，并且会重新校验 ActionRequest 的
fingerprint。卡片过期（例如你已经改过内容）时，点击会被拒绝并刷新成最新状态，绝不会
用旧内容做出新动作。想改内容就直接在输入框里说，Tree 会重新准备一份给你确认。

在 Tree 工作时可以随时「停止」，但只在真正安全的时候：排队中的消息随时可以取消；
Tree 还在理解你的请求、尚未写入任何东西时可以停止；一旦已经进入写入或对外执行阶段，
按钮会如实告诉你不能安全停止，而不是假装停下来了。已经开始的对外动作不会被"停止"回滚。

输入框对中文输入法友好：Enter 发送、Shift+Enter 换行，候选词选择时的 Enter 不会误发送。
页面在手机宽度下可用；对话页和 v1.1 的 mobile review 页面并存，互不影响。

日常入口是对话，不是命令：

```text
Tree > 你好，我是 Tree。今天想安排什么？
You > 明天下午三点提醒我交软件工程报告
Tree > 已创建任务「交软件工程报告」，截止 2026-09-21 15:00（+08:00）。
You > 帮我安排一下这周
Tree > 周计划提案（3 个时间块）。要应用这个计划吗？回复「可以」我就写进计划。
You > 可以
Tree > 已应用周计划提案：新增 3 个时间块，替换 0 个。
You > 每周一 10 点到 12 点有课，记下来，再帮我安排下周
Tree > 已加入固定安排：
       每周一 10:00–12:00 · 计算机系统基础课
       从 2026-09-21 起持续到你删除。
       周计划提案（2 个时间块，避开这节课）。要应用这个计划吗？
You > 发个打招呼的邮件给我自己
Tree > 将要发送的邮件（以下内容就是实际发出的内容）：
       发件账号：smail（me@example.edu） · 收件人：me@example.edu
       主题：打个招呼
       正文：你好，这是一封来自 Rings 的测试邮件。
       确认发送吗？回复「确认发送」我就发送，或回复「取消」。
You > 记住我的办公室在仙林。
Tree > 我准备记录这条长期信息：
       - profile.office：仙林
       （来自你的话：「记住我的办公室在仙林。」）
       如果要长期保存，请明确说「确认记住」；说「不要记」我就不保存。
You > 确认记住
Tree > 已记住：
       - profile.office：仙林
You > 我今天有什么事？
Tree > 今天（2026-09-21，Asia/Shanghai）：
       安排
       - 10:00–12:00 计算机系统基础课（每周）
       任务
       - 交软件工程报告（10-09 截止）
```

Tree 用自然语言处理本地日常：任务、日历、固定安排（每周重复）、工作记录、周计划和资料问答。
固定安排只支持「每周固定星期几」，一条规则一个星期几；「单双周」「每两周」「每月」这些
本版本还不能记。只说「我每周一十点到十二点有课」时，Tree 会先问你要不要加入固定安排，
不会不声不响地写下来。新邮件可以发给三种收件人：你写出来的邮箱地址、你记过的联系人
（例如「张老师邮箱是 zhang@example.edu，记成联系人」，之后就能说「给张老师发邮件」），
或者你自己的已配置邮箱（「发给我自己」）；地址不在你话里、名字查不到、有多个同名联系人时，
Tree 会问你而不是猜。外部动作（发邮件、eHall 提交）**不会**由模型执行：发邮件时 Tree 会先起草，
把**将要发出的完整内容**给你看，你回复「确认发送」之后才发出（`pw` 的审批链仍然是背后那套机制）。

长期信息（例如「记住我的办公室在仙林」）会先给你看要保存的内容，只有你明确回复「确认记住」
才真正保存；平铺直叙的一句话（「我的办公室在仙林」）不会自动变成记忆，泛泛的「可以」也不会。
「你记得我的办公室在哪里吗？」只依据你确认过的长期信息回答。「我今天有什么事？」会按你配置的
时区给出今天的安排、任务和需要处理的事，只读，不会替你确认或执行任何东西。

模型偶尔给出不合规的回答时，Tree 只会重试**一次**并且不执行任何操作；终端给的输入无法解码时，
它会说明并让你重新输入，而不是结束会话。在真实终端里，输入用的是真正的行编辑器：左右键移动光标、
Home/End、Backspace/Delete、上下键翻本次会话的输入历史都可以用，删掉一个汉字不会留下半个字符；
历史只存在内存里，不会写入任何 history 文件。Ctrl-C 取消这一行，Ctrl-D（空行时）正常退出。
修改一个还没确认的提议时，旧提议会被作废，所以一次「可以」只会确认最新的那一组。
「你能做什么」的回答来自当前运行时的真实配置。

| 入口 | 用途 |
| --- | --- |
| `uv run rings --web` | 打开浏览器对话页（需要网页控制面已启用并运行） |
| `uv run rings` | 终端对话式入口，SSH / 开发者 / 备用 |
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
| Release Notes（当前版本：v1.4.0） | [docs/releases/1.4.0.md](https://github.com/Mr-tree013/Rings/blob/main/docs/releases/1.4.0.md) |
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

- 网页对话页和 Mobile 页面都只是可信 LAN 内的 HTTP 页面，不是面向公网的服务；它是 v1.1 同一套 session / CSRF 保护。
- 发信邮箱的配置仍然是配置文件里的事，本版本没有网页配置向导，也不支持附件、抄送、多收件人或定时发送。
- 固定安排只支持每周同一天、不跨夜；复杂重复规则还没有。
- 对话里只能准备 eHall 申请：`ehall.status` 读表单，`ehall.certificate.prepare` 准备一份精确的申请并给你确认，而且只有你亲口说「确认提交」才会提交；eHall 仍然是 `pw` 下的同一条 typed pipeline，没有通用 browser automation。
- eHall 只支持 `证明书申请` 这一项服务；退课、撤销申请、退宿等能力在代码里不存在。
- eHall 表单不会自动填充：每个提交的值都必须出现在你自己的消息里。
- 「停止」不能回滚已经开始的对外执行；SMTP 的 `UNKNOWN` 仍然需要人工对账，绝不自动重发。
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
