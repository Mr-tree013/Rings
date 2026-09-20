# Watchers and manual input

## What it does

Rings can watch a small number of fixed public pages, and it can accept text you paste or forward.
Both paths produce durable observations that are analyzed into candidates; neither creates tasks or
actions on its own.

## Enable / configure

The URL comes from configuration, never from the model and never from a command argument:

```toml
[watchers]
poll_interval_seconds = 300
timeout_seconds = 20
max_response_bytes = 2097152
full_fetch_every = 24

[[watchers.web]]
id = "course-notices"
url = "https://example.edu/notices"
enabled = true
```

A target must be `https://` with no userinfo, no IP-literal host and no explicit port, and every
resolved address must be public. There is no `pw watch fetch <url>` command.

## Common workflow

```bash
uv run pw watch targets
uv run pw watch sync
uv run pw watch sync --target course-notices
uv run pw watch status
uv run pw watch observations
uv run pw watch observation show <OBSERVATION>
```

Text you paste or forward travels the same bounded analysis path:

```bash
uv run pw ingest text "Forwarded notice..." --source qq-forward
uv run pw ingest list
uv run pw ingest show <INPUT>
```

`--source` records where the text came from (`manual`, `qq-forward` or `other`) and appears in the
event identity, so the same text from two sources is two observations.

## Commands

```text
pw watch targets
pw watch sync
pw watch sync --target course-notices
pw watch status
pw watch observations
pw watch observation show <OBSERVATION>
pw ingest text "..." --source qq-forward
pw ingest list
pw ingest show <INPUT>
```

## Safety behavior

- **Public, unauthenticated HTTPS only.** No credentials, no cookies, no login wall, no non-default
  port, no private or loopback address.
- **No redirects.** Any 3xx (except `304`) is an error rather than something to follow; configure
  the final URL instead.
- **No JavaScript.** Pages are parsed as text; no browser is launched, and no sub-resources are
  downloaded. Responses are size-limited and unsupported content types are not stored.
- **No automatic task creation.** A changed page or a pasted note produces a durable observation and
  an event; the analysis writes candidates only. Nothing becomes a `Task`, `Case`, `Action`,
  `Approval`, fact or playbook by itself.
- Page content and pasted text are untrusted quoted data. They are stored and quoted, never rendered
  or executed, and they are never used to trigger personal-knowledge retrieval.
- The first fetch of a target is only a baseline, so enabling a watcher does not treat the whole site
  as new. A periodic full fetch makes sure a server that always answers `304` cannot hide a change.

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| `WebWatchRedirectNotAllowed` | The URL redirects. Find the final URL and configure that. |
| A target never reports a change | The normalized content hash did not change, or the page is JavaScript-rendered and therefore invisible to a text fetch. |
| A vault or page is offline | Offline sources are reported as offline and are never copied into host storage as a fallback. |

- Sites that require authentication or JavaScript rendering are out of scope in v1.
- There is no QQ protocol client: `--source qq-forward` records that **you** forwarded the text.

## Implementation notes

- Durable web and manual observation: [ADR-0029](../adr/0029-durable-web-and-manual-observation.md)
- URL validation and resolver rules live in that ADR and in the adapter, not in this guide.
