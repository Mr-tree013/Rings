# ADR-0030 — A Controlled Local MCP Interface

## Title

VS Code gets a local stdio MCP server with a fixed, bounded surface: read-only by default, explicit
about the two capabilities it can be granted, and structurally unable to approve, execute, send,
submit, confirm or promote anything.

## Status

Accepted

## Context

The assistant's state lives on one machine, and the place a person wants to *see* it while working is
their editor. MCP is the protocol editors have converged on for exactly that, and VS Code can launch
a local stdio server from `mcp.json`. So the integration is worth building — and the risk is
obvious from the shape of everything that came before.

- **an MCP server is an agent-facing surface.** Whatever it exposes, a model behind the editor can
  read and, if it is a tool, call. The whole project is built on the rule that nothing reaches an
  effect without a human approval; a server that exposed `approve` would make an editor agent the
  approver;
- **"just give me the data" is how a capability list grows.** A tool for mail, a tool for drafts, a
  tool for facts, a tool for playbooks, a generic SQLite query "for flexibility", and a filesystem
  tool "to look at the repository" are each one small step from the last — and each one bypasses a
  boundary a previous phase spent its effort building;
- **the client's confirmation dialog is not authorization.** VS Code will ask before running a tool
  that is not marked read-only, and that is a good UX. It is also a decision made by the *client*
  about the *client's* model, which cannot be the server's security boundary;
- **stdout is the protocol.** A `print`, a Rich table or a stray warning on stdout corrupts the
  JSON-RPC stream, and the failure looks like a broken editor rather than a logging bug;
- **the daemon is not the server.** Wiring MCP into `assistantd` or the mobile web service would
  create a listening surface where a stdio subprocess is enough, and would couple an editor session
  to a background process that may not be running.

Phase 8B therefore builds a small adapter around services that already exist, and spends its
attention on what is *absent*: no approval, no execution, no mail, no eHall, no browser, no
filesystem, no shell, no HTTP, no model, no sampling, no prompts.

## Decision

1. MCP is a local integration surface, not an autonomous runtime.
2. V1 MCP transport is stdio only.
3. No Streamable HTTP, SSE or network-listening MCP transport exists.
4. The MCP host launches the server process.
5. MCP stdout is protocol-only; application logging goes to stderr.
6. MCP is disabled by default.
7. The default MCP capability mode is read-only.
8. Local write capability requires explicit server-side configuration.
9. Client/tool confirmation UI is defence in depth, not the authorization boundary.
10. MCP never exposes Action approval.
11. MCP never exposes Action execution.
12. MCP never exposes SMTP or eHall execution.
13. MCP cannot create `ActionRequest`s.
14. MCP cannot confirm or reject `FactCandidate`s.
15. MCP cannot create, promote, reject or retire `Playbook`s.
16. MCP cannot create `ApprovalChallenge`s or `Approval`s.
17. MCP cannot access browser or eHall sessions or credentials.
18. MCP cannot access mail credentials, raw mail or browser profiles.
19. MCP exposes no filesystem read or write tool.
20. MCP exposes no shell or subprocess tool.
21. MCP exposes no generic HTTP or browser tool.
22. MCP exposes no sampling, elicitation or model-invocation capability.
23. MCP does not use workspace roots for filesystem access.
24. Task writes, if explicitly enabled, use the existing `TaskService`.
25. Task writes preserve existing planner, scheduler and revision semantics.
26. Knowledge search is local, read-only and independently opt-in.
27. Knowledge search never invokes `GroundedAnswerService` or another model.
28. No mail body, draft, fact or playbook content is exposed by the default MCP surface.
29. Every tool and resource has an explicit bounded output shape.
30. VS Code configuration is generated as a snippet; the assistant does not silently edit editor
    configuration.

Additional frozen details:

- **Three configuration keys, all conservative.** `enabled` (default false), `write_scope` (default
  `none`, the only other value is `tasks`) and `expose_knowledge` (default false). There is no
  transport key, no port, no host, no trusted-client list and no capability list; unknown keys are
  refused. The surface is decided by code and only *narrowed* by configuration.
- **The SDK lives in exactly one package.** `mcp` is imported under `adapters/mcp/` and nowhere
  else. `application/mcp_facade.py` is the whole application surface and contains no protocol type,
  decorator or SDK import at all — an architecture test enforces both.
- **The server is a separate process with its own entry point.** `growing-assistant-mcp` is a
  console script that loads host configuration, requires `[mcp] enabled = true`, composes the
  facade, registers the surface and runs stdio. It is not hosted by `assistantd`, not hosted by the
  mobile web service, opens no socket and works with the daemon stopped: SQLite's own transactional
  semantics are the concurrency story, exactly as they are for two CLI invocations.
- **stdout carries protocol frames and nothing else.** Logging is configured onto stderr at the
  entry point, the module never prints, a disabled server writes its one-line explanation to stderr
  and exits non-zero, and an unrecognised argument is an error rather than something to ignore — so
  `--transport http` fails loudly instead of quietly serving stdio.
- **Four resources, all bounded JSON.** `assistant://status` (version, transport, write scope,
  knowledge flag, three counts and the capability list), `assistant://tasks/open` (≤50 tasks with
  id/title/priority/status/estimate/deadline), `assistant://cases/open` (≤50 cases with their
  lifecycle fields — never their actions) and `assistant://plan/current` (the current week's blocks,
  ≤100, or `configured=false` when planning is unconfigured). There is no URI template and no
  parameter, so a client cannot ask for a different document.
- **Reading the plan never makes a plan.** The window comes from the existing weekly convention and
  the blocks come from the existing read model; no proposal is generated, applied or superseded.
- **Two read tools always exist.** `assistant_get_task` and `assistant_get_case` take one `id` and
  accept a full UUID or a unique prefix, like every other command in the project. They are
  annotated `readOnlyHint = true` and `openWorldHint = false`, and they return the entity's own
  fields — no mail, no knowledge excerpt, no model output, no unrelated metadata.
- **Knowledge is a fourth tool behind an explicit opt-in.** `assistant_search_knowledge` exists only
  when `expose_knowledge = true`, and it calls the deterministic local full-text search — never
  `GroundedAnswerService`, never a provider. Its inputs are a non-blank query of at most 1000
  characters, an optional `root_id` and a limit of 1–8; its hits carry a logical URI, a source span
  and an excerpt capped at 1200 characters with a 6000-character total. No physical path, mount
  path, rowid or full document is returned, and the tool's description says plainly that excerpts
  from the user's local index go to the connected MCP client.
- **Task writes are a separate, deliberate scope.** With `write_scope = "tasks"`,
  `assistant_create_task` and `assistant_complete_task` are registered; with `write_scope = "none"`
  they are **not registered at all**, so a client that asks for one by name gets "Unknown tool"
  rather than a refusal it can retry. Creation takes a title, an optional priority, an optional
  estimate and an optional timezone-aware ISO 8601 deadline, and goes through `TaskService` — which
  means deadline reminders and the rolling replan request are materialized by the same code path the
  CLI uses. Completion goes through the same terminal transition and is therefore marked
  `destructiveHint = true` and explicitly *not* `idempotentHint`.
- **Refusals are typed and bounded.** A tool returns an explicit `CallToolResult`: success carries
  one compact JSON document, and a refusal carries `isError = true` with
  `{"error": {"kind", "message"}}` whose message is written in this project — never a stack trace, a
  SQL fragment or a filesystem path. Anything unrecognised propagates, and the SDK then reports it
  without echoing internals.
- **The absence is structural, not filtered.** The facade's constructor takes task, case, commitment,
  planner-read, notification and (optionally) knowledge services; it does not take an approval
  service, an executor, a mail repository, a learning service or a playbook service, so those
  methods cannot exist. Architecture tests assert the imports and the identifiers of both the
  facade and the adapter, and an integration test snapshots every table before and after a task
  write to show that only `tasks` grows.
- **No prompts, no sampling, no elicitation, no roots.** The server answers `resources/list`,
  `resources/read`, `tools/list` and `tools/call`; it registers no prompt, asks the client for
  nothing, never requests a model completion and never reads a workspace root. Tests assert the
  identifier set and the registered surface.
- **The editor configuration is the user's.** `pw mcp vscode-config` prints a `servers` snippet with
  `type: stdio`, `command: uv`, `args: ["run", "growing-assistant-mcp"]` and a `cwd`, and writes
  nothing: no `.vscode/mcp.json`, no user settings, no secrets. The snippet carries no API key, no
  SMTP or IMAP password and no token, because the server needs none — it reads the same host
  configuration as every other command.

## Alternatives Considered

- **MCP over public HTTP or Streamable HTTP.** It would need binding, authentication and a threat
  model for a listening socket, for a server whose entire value is local. Rejected; stdio only.
- **Hosting MCP inside `assistantd`.** Two consumers of one process, an editor session coupled to a
  background service, and a reason to keep the daemon running. Rejected; a separate console script.
- **A generic SQLite query tool.** It would expose every table — mail, drafts, facts, playbooks,
  sessions, token hashes — through one parameter. Rejected.
- **A generic filesystem tool.** The project deliberately keeps a person's documents behind the
  knowledge index, and the editor already has its own file tools. Rejected.
- **A generic shell or subprocess tool.** It is execution by another name. Rejected.
- **A generic HTTP or browser tool.** The watcher phase spent its effort making network access fixed
  and public; a generic fetch would undo it. Rejected.
- **An approval tool.** The client's model would become the approver, and the fingerprint would be
  confirmed by something that never saw the payload. Rejected; approvals stay in the CLI.
- **An action-execute tool.** Same argument, with real external effects. Rejected.
- **A fact-confirmation tool.** Phase 7A's whole boundary is that a person reads a candidate's
  sentence and decides. Rejected.
- **A playbook-promotion tool.** Promotion needs the exact payload and the dry-run result in front of
  a person. Rejected.
- **Mail send and eHall submit tools.** Both are approval-gated external side effects. Rejected.
- **MCP sampling, so the server could ask the client's model for help.** It would give the assistant
  a second model path outside its own provider boundary, configuration and cost controls. Rejected.
- **Exposing knowledge by default, because "the editor is the user".** The editor agent is not
  necessarily the user, and connecting a client should not silently hand it personal documents.
  Rejected; `expose_knowledge = true` is an explicit opt-in the tool description repeats.
- **Trusting VS Code's confirmation dialog as the write boundary.** It is client-side, it depends on
  annotations the server itself declares, and it is a UX affordance. Rejected; the boundary is
  `write_scope` on the server.
- **Writing `.vscode/mcp.json` for the user.** A silent edit of editor configuration is exactly the
  kind of helpfulness this project avoids. Rejected; the snippet is printed.

## Consequences

- Working in the editor, a person (or the agent behind it) can see the open tasks, the open cases,
  the current week's plan and the status mode — and nothing else unless the host was explicitly
  configured to allow more.
- The safety story is inspectable: three configuration keys, four resources with fixed URIs, two to
  five tools whose names are pinned by tests, one facade with no side-effect imports, and one
  adapter that imports the SDK and nothing dangerous. A reader can check the whole surface in a few
  minutes.
- Costs and limits: an editor agent cannot complete a task unless the user changes the host
  configuration and restarts the server; knowledge search returns excerpts, not documents, so an
  agent that needs more must ask the user; the surface has no way to start a plan or create a case,
  which means the interesting work still happens in the CLI; and the server is a separate process
  per editor session, which is the price of keeping it out of the daemon.
