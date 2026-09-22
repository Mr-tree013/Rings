# Getting started with Rings

## What it does

Rings runs on your machine. Its durable state is one SQLite database plus the immutable objects that
database references; configuration lives under `$XDG_CONFIG_HOME/growing-assistant/`; credentials live
in environment variables and are never written to a configuration file or to a backup.

On a fresh install nothing reaches outside your machine: no mail account, no watcher, no browser, no
phone surface and no editor integration is switched on until you switch it on.

## Requirements

```text
Python 3.13          (requires-python = ">=3.13,<3.14")
uv                   (https://docs.astral.sh/uv/)
Linux or WSL         the v1 reference runtime
```

Windows-native execution of the daemon is not a v1 reference target: run `assistantd` inside WSL,
where Unix permission bits and the advisory lock exist.

Playwright Chromium is **not** required for a first run. It is needed only by the eHall pipeline; see
[ehall.md](ehall.md).

## Install

```bash
git clone https://github.com/Mr-tree013/Rings.git
cd Rings

uv sync --frozen
```

`uv sync --frozen` installs exactly the versions recorded in `uv.lock` and does not update the lock
file.

## Configure

```bash
mkdir -p ~/.config/growing-assistant

cp docs/examples/config.toml \
  ~/.config/growing-assistant/config.toml
```

The sample is deliberately inert until you change it:

- no mail accounts — mail disabled
- no watcher targets — watchers empty
- `[ehall] enabled = false`
- `[mobile] enabled = false`
- `[mcp] enabled = false`, `write_scope = "none"`, `expose_knowledge = false`

It does configure one example knowledge root and a planning timezone, because those are the parts
that are harmless to switch on. Edit the paths before using them:

```toml
format_version = 1

[[storage.roots]]
kind = "local"
id = "documents"
label = "Documents"
path = "/home/user/Documents"
enabled = true

[planning]
timezone = "Asia/Shanghai"
```

The strict parser **rejects** credential-shaped keys: a `password`, `secret` or `api_key` key in
`config.toml` is an error, not an override. Nothing in a configuration file is treated as a secret.

### Where the runtime keeps things

```text
~/.local/share/growing-assistant/   runtime state (SQLite, cursors, jobs, audit rows)
~/.config/growing-assistant/        configuration
~/.cache/growing-assistant/         derived knowledge indexes for local roots
```

Each location follows `XDG_DATA_HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME`, so a runtime can be
relocated by pointing those variables somewhere else. Your documents never move: a vault is
referenced where it is, and an unplugged vault is reported as offline rather than as corruption.

## Credentials

All credentials are environment variables. Do not put them in `config.toml`, do not commit a `.env`
that holds them, and do not expect a backup to contain them.

| Credential | Environment variable |
| --- | --- |
| Model provider API key | `DEEPSEEK_API_KEY` |
| IMAP password for account `<ACCOUNT_ID>` | `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_PASSWORD` |
| SMTP password for account `<ACCOUNT_ID>` | `GROWING_ASSISTANT_MAIL_<ACCOUNT_ID>_SMTP_PASSWORD` |

`<ACCOUNT_ID>` is the `id` you gave the account in `[[mail.accounts]]`, upper-cased with hyphens
turned into underscores: an account with `id = "smail"` reads
`GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD` and `GROWING_ASSISTANT_MAIL_SMAIL_SMTP_PASSWORD`. The IMAP
and SMTP passwords are separate: receiving mail does not require the sending credential, and
preparing a send requires neither.

```bash
export DEEPSEEK_API_KEY='...'
export GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD='...'
```

Check what the host sees without printing a secret:

```bash
uv run pw mail accounts     # whether a credential is present, never its value
uv run pw model status      # configured model boundary; never contacts the provider
uv run pw ehall status      # eHall capability state; never contacts the server
```

The eHall login is different in kind: a university password exists nowhere in this project. You log
in by hand in a headed browser, and the session cookie stays in a private browser profile under the
runtime data directory.

## First run

```bash
uv run pw doctor
uv run pw integrity check
uv run assistantd
```

In a second terminal:

```bash
uv run pw status
uv run pw daemon status
```

`pw doctor` is a read-only environment check, and `pw integrity check` audits the runtime database,
its content objects and the configured roots without creating or repairing anything. Neither needs
the daemon to be running.

### The daemon is single-instance

**One runtime data directory holds one `assistantd`.** It takes an advisory `flock` on
`assistantd.lock` inside the runtime data directory; a second instance exits before starting any
service and says why. The kernel releases the lock when the process ends, so a lock file left behind
by a power cut does not block the next start and nothing has to be deleted by hand. The pid recorded
in that file is diagnostic only — `pw daemon status` reports the lock, not the file.

The daemon supervises independent services, each with its own failure boundary:

| Service | Starts when | What it does |
| --- | --- | --- |
| `index-sync` | always | reconciles configured roots (scan → catalog → index), once at startup and then on the configured interval |
| `scheduler` | always | executes durable reminders and debounced rolling replans |
| `mail-sync` | at least one mail account is configured | receives mail incrementally |
| `event-worker` | mail and a usable model are both available | claims and analyzes events; without a model nothing is lost, events simply stay `RECEIVED` |
| `web-watch` | at least one watcher target is enabled | polls the configured public pages |
| `mobile-web` | `[mobile] enabled = true` | serves the LAN control plane |

A root that is offline, whose identity does not match, or whose index is corrupt affects only that
root. A failure of the host runtime database is the only kind that is escalated to the supervisor.

If you configure mail but have no model key, mail is still received and stored: events stay
`RECEIVED` until a provider is configured, and they are analyzed afterwards.

## One command to start (v1.4.0)

```bash
uv run rings up        # 起服务（或复用已在跑的）+ 自动配对 + 打开 /chat + 打印状态
uv run rings down      # 优雅停止；进行中的对话被标为 interrupted，不重放
uv run rings autostart install   # 可选：Windows 登录时自动常驻（一个 .cmd，删掉即撤销）
```

`rings up` 会：后台起 `assistantd`（日志在 `<runtime>/assistantd.log`，每次启动按 5 MB 轮转一次）、
等锁与网页控制面就绪（上限 20 秒，超时打印日志尾部而不是谎报成功）、生成一枚一次性配对码并打开
`/chat#pair=<token>`（fragment 不上服务器，页面读到后立刻 `replaceState` 抹掉；已配对的浏览器不会
再建第二个 session），最后打印一张状态表：daemon / web / 配对 / 凭据（只列变量名）/ 邮件账号与就绪数 /
eHall 状态。若 `[mobile] enabled = false`，它不打开浏览器、不配对，并打印该配置片段。

凭据（邮箱应用专用密码、模型 key）可以放一份属于你自己的文件，避免每个终端 export：

```bash
chmod 600 ~/.config/growing-assistant/secrets.env
```

```ini
DEEPSEEK_API_KEY=…
GROWING_ASSISTANT_MAIL_<ID大写>_PASSWORD=…
GROWING_ASSISTANT_MAIL_<ID大写>_SMTP_PASSWORD=…     # 只有要发信才需要
```

环境变量优先于文件；权限不是 `0600` 时文件会被拒绝并说明原因；出现这个集合以外的键（例如
`password=`）整份文件被拒绝；项目从不写它、也从不打印或记录它的值。

要调试可以 `uv run rings up --foreground`（前台运行 daemon，不自动配对），或在 CI 里用
`uv run rings up --no-open`（不打开浏览器）。

## Commands

```text
pw doctor
pw integrity check
pw status
pw daemon status
pw mail accounts
pw model status
pw ehall status
```

## Windows / WSL

The reference runtime is WSL. If you want the daemon to start at login, let Windows Task Scheduler
enter WSL and start exactly one daemon:

```text
wsl.exe -d <DISTRO> --cd <PROJECT_DIR> -- /bin/bash -lc 'exec uv run assistantd'
```

This project does not install a service and does not create or edit a scheduled task. If a task is
triggered twice, the single-instance lock makes the second process exit immediately instead of two
daemons writing the same runtime.

## Troubleshooting

| Symptom | What it usually means |
| --- | --- |
| `pw doctor` reports a missing runtime directory | Nothing is wrong yet: the directory is created on first use. Run `pw status` or `pw integrity check` again after any command has run. |
| A second `assistantd` exits immediately | The lock is held by the running instance. `pw daemon status` tells you which one. |
| Mail is stored but nothing is analyzed | No model provider is configured, or the daemon is not running. Events stay `RECEIVED` and are analyzed later. |
| A vault shows as offline | Its storage is not mounted. Metadata-only answers are still available; content is not. |

## Implementation notes

- Architecture and runtime contract: [docs/specs/0001-system-design.md](../specs/0001-system-design.md)
- SQLite runtime store: [ADR-0003](../adr/0003-sqlite-runtime-store.md); async boundary:
  [ADR-0009](../adr/0009-async-sqlite-boundary.md)
- v1 runtime, upgrade and release contract: [ADR-0032](../adr/0032-v1-runtime-and-release-contract.md)
- Upgrading an older runtime: [docs/upgrade-to-v1.md](../upgrade-to-v1.md)
