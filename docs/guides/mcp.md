# MCP / VS Code integration

## What it does

Rings exposes a local stdio MCP server so an editor — and an agent inside that editor — can read open
tasks, open cases, the current plan and (optionally) bounded knowledge excerpts. It is a read-only
surface by default and it can never approve or execute anything.

## Enable / configure

```toml
[mcp]
enabled = false
write_scope = "none"        # none | tasks
expose_knowledge = false    # only when you explicitly want it
```

```bash
uv run pw mcp status
uv run pw mcp vscode-config
uv run pw mcp vscode-config --project-root /path/to/project
```

Paste the printed snippet into your VS Code MCP configuration (a workspace or user `mcp.json`), then
start and trust the server and inspect the read-only surface first. `pw mcp vscode-config` only
prints; it writes nothing anywhere.

## Common workflow

```bash
uv run pw mcp status         # what this host would register, read-only, starts no server
uv run pw mcp vscode-config  # print the snippet
```

The default surface is four resources (status, open tasks, open cases, current plan) and two
read-only tools. Cases expose lifecycle fields only, not their actions.

## Commands

```text
pw mcp status
pw mcp vscode-config
pw mcp vscode-config --project-root /path/to/project
```

The `growing-assistant-mcp` server itself is started by the editor over stdio, not run by hand.

## Safety behavior

- **Local stdio only.** The server is spawned by the editor, is not hosted in `assistantd`, and
  listens on no port. stdout carries protocol messages and all logging goes to stderr.
- **Disabled by default.** With `enabled = false` the server explains why on stderr and exits; the
  daemon is not required either way.
- **Read-only by default.** Optional task writes are registered only when you set
  `write_scope = "tasks"`, and they call the same task service as the CLI. When they are not enabled
  the tools do not exist at all — an editor's tool-approval prompt is user experience, not an
  authorization boundary.
- **Optional bounded knowledge exposure** requires `expose_knowledge = true`, and returns logical
  URIs, source spans and bounded excerpts through local full-text search — never physical paths and
  never a whole document.
- **No approval and no execution.** MCP cannot create an approval, execute an action, send mail,
  submit eHall, confirm a fact, promote a playbook, read arbitrary files, run a shell, browse, or
  make arbitrary HTTP requests.
- MCP creates no durable server state and no session; it reads the same host configuration your
  other commands use.

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| The server exits immediately | `[mcp] enabled = false`. The stderr message says so. |
| A tool name is unknown in the editor | It is not part of the enabled surface; check `pw mcp status`. |
| The editor asks you to trust the server | That is the editor's own decision and is not treated as authorization by Rings. |

- There are no MCP prompts, sampling or elicitation capabilities, and no workspace file access.
- Tool names and resource URIs are part of the frozen v1 compatibility surface.

## Implementation notes

- Controlled local MCP interface: [ADR-0030](../adr/0030-controlled-local-mcp-interface.md)
