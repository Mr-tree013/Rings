# ADR-0029 — Durable Web and Manual Observation

## Title

Configured public pages and text a person pastes in become durable, versioned observations that feed
a bounded analysis — without a generic HTTP client, without a browser and without creating any work.

## Status

Accepted

## Context

Everything the assistant can act on so far arrives through mail. But a student's world also includes
a course page that quietly changes its deadline, an exam schedule that appears, and a notice
forwarded through a chat client. Phase 8A is about noticing those, and about doing it without
turning the project into a crawler.

The dangerous version of this feature is easy to describe:

- **"fetch this URL for me" is a capability, not a feature.** A model that can name a URL can ask the
  process to connect to `http://127.0.0.1:8080` on its own machine, or to a metadata endpoint, or to
  whatever the DNS answer of the moment points at. The capability has to be absent, not forbidden;
- **redirects move the origin.** A watcher that follows a `302` can be steered from a page the user
  configured to one they never saw, and the *content* is then quoted to a model as if it were the
  configured page;
- **an unauthenticated page is not a document you control.** Watched content is written by someone
  else, can change between two polls, and can contain instructions aimed at whatever reads it next;
- **unbounded fetching is unbounded cost and disk.** A page that streams forever would fill the
  runtime directory, and one that changes on every request (a build stamp, a nonce) would produce an
  event per poll and a model call per event;
- **"tell me when it changes" has a first-run trap.** Turning a watcher on must not announce the
  entire current page as news, or the analysis queue fills with the site's whole history;
- **manual input is forwarded text.** `pw ingest text "..."` is often a paste of something somebody
  else wrote in a chat client — including, plausibly, instructions.

Phase 8A therefore treats a watcher as a *fixed, configured, public, read-only observer*, and manual
input as *durable quoted text*. Both feed one bounded analysis that can classify and extract
candidates, and that cannot create anything.

## Decision

1. Web watchers are explicitly configured fixed public HTTPS targets.
2. The model never chooses a watcher URL.
3. V1 web watchers support public unauthenticated `text/html`, `text/plain` and `application/json`
   resources only.
4. HTTP authentication, cookies, browser sessions and JavaScript rendering are absent.
5. Watchers never use the eHall Playwright capability.
6. Redirects are not followed automatically.
7. Hostnames resolving to loopback, private, link-local, reserved or otherwise non-public addresses
   are rejected.
8. Web source content is untrusted data, never instructions.
9. The initial successful fetch establishes a baseline and does not emit a change event.
10. Subsequent normalized-content hash changes create durable `WebObservation`s.
11. Unchanged content does not create a new logical change event.
12. `ETag` and `Last-Modified` are optimizations, not correctness authority.
13. Periodic unconditional full fetches re-establish correctness.
14. A changed `WebObservation` is bridged idempotently to exactly one `InboundEvent`.
15. `InboundEvent` stores source identity only, not full page content.
16. Manual input is stored durably before it is bridged to `EventInbox`.
17. Manual text is treated as untrusted quoted input because it may be forwarded content.
18. Web and manual model analysis produces analysis and candidates only.
19. Analysis cannot mutate Tasks, Cases, Planner, Facts, Playbooks, Actions or external systems.
20. Analysis is durable and idempotent, and is reused across `EventWorker` retries.
21. No personal knowledge context is automatically attached to watcher or manual analysis.
22. No model-generated URL, HTTP request or tool call exists.
23. One watcher failure does not stop other watchers or daemon services.
24. Without usable model configuration, observations and events remain durable and unprocessed rather
    than being discarded.

Additional frozen details:

- **The URL is configuration, validated at parse time.** A target is an id matching
  `^[a-z][a-z0-9-]{0,62}$` and an HTTPS URL with no credentials, no IP-literal host and no explicit
  port; duplicate ids are refused. There is no header, no cookie, no method and no
  "follow redirects" switch to configure, because a watcher is one fixed public page.
- **The address, not the name, decides.** Before a request the adapter resolves the hostname and
  refuses the fetch unless *every* resolved address is `is_global` — which excludes loopback,
  private, link-local, multicast, reserved, unspecified and shared carrier-grade space. A name that
  merely looks public is not evidence; `X-Forwarded-For` never appears here at all.
- **A redirect is a refusal.** `301`, `302`, `303`, `307`, `308` and `300` raise
  `WebWatchRedirectNotAllowed` and nothing is requested from the target; `304` is the one 3xx the
  adapter accepts, because it is a cache answer rather than a direction. The environment is not
  consulted either (`trust_env=False`), so no ambient proxy can move a watcher.
- **The response is bounded while it streams.** The body is read in chunks and abandoned the moment
  it exceeds `max_response_bytes`, and an abandoned fetch never writes a snapshot — so an endless
  response cannot fill the disk and cannot move a baseline. Only `text/html`, `text/plain` and
  `application/json` are accepted; a PDF, an image or a zip is a refusal rather than a download.
- **Extraction is deterministic and line-oriented.** HTML is parsed with the standard library
  parser, `script`, `style` and `noscript` content is dropped, and nothing is fetched while parsing.
  Decoding never fails (invalid UTF-8 becomes a replacement character), `CRLF` becomes `LF`, trailing
  whitespace per line is removed, leading and trailing blank lines are dropped and runs of blank
  lines collapse to two. Line structure survives on purpose: it is what the change diff is built
  from.
- **Content identity is a hash of the normalized text.** `content_sha256` is SHA-256 over the
  normalized UTF-8, and the normalized text itself lives in the content-addressed store at
  `<runtime>/web/snapshots/<prefix>/<sha256>.txt`. Writes are temp-file, flush, `fsync`, atomic
  rename, and an existing object is verified and reused; the database stores only the relative
  storage key.
- **The first fetch is a baseline.** It writes a snapshot, an observation marked `is_baseline = 1`
  and the target state, and emits **no** event. Only a later hash change produces a change
  observation that names the observation it replaced. A configured URL that changed starts over:
  the old baseline and the old validators are not inherited.
- **Validators are an optimization and the full fetch is the rule.** When
  `checks_since_full < full_fetch_every` the request may carry `If-None-Match` and
  `If-Modified-Since` and a `304` is "unchanged"; once the counter reaches the threshold the next
  fetch is unconditional and the counter restarts. A server that answers `304` forever cannot hide a
  change, which a test proves.
- **The event carries identity, never content.** `web.page.changed` is ingested as
  `source = "web:<target-id>"`, `external_id = "observation:<uuid>"` and a payload of exactly
  `{observation_id, target_id}`. `manual.input.received` is the same shape with
  `{manual_input_id, source}`. The page and the pasted text stay in their own tables, and a bridge
  test asserts the content never appears in the event row.
- **Both bridges are crash-safe by construction.** The observation (or input) is committed first,
  the event is ingested second and the link is written third, and both operations are idempotent:
  an observation with no event produces one on the next round, an event with no link is recognised
  as a duplicate and linked. The repair runs every round, bounded, including rounds with no change
  at all.
- **Manual input is durable before anything else happens to it.** `pw ingest text` writes the row,
  then queues the event, and prints what may happen next — it does not call a model. The source is a
  closed set of names (`manual`, `qq-forward`, `other`), not a channel with executable semantics.
- **One bounded analysis, with the schema as the boundary.** The prompt sees a *change document*
  (`target_id`, previous and current hashes, `added_text`, `removed_text`, `current_excerpt`, built
  with `difflib` and capped at 3000/3000/6000 with a 12000 total) or a manual document (`source`,
  `input_source`, `text` capped at 12000). No knowledge base, no facts, no tasks, no mail, no
  playbooks and no URL are attached. The closed output schema has exactly `category`, `summary` and
  `action_candidates`, and each candidate has exactly `text`, `temporal_kind`, `time_text` and
  `interpreted_at` — nowhere to put a command, a task, a case, an action request, an approval, a URL
  or a tool call.
- **`DEADLINE` and `EVENT_START` stay separate.** A candidate that claims a time must carry the text
  it was read from or the instant it names, and an instant without an explicit offset is refused
  rather than guessed at.
- **Analysis is durable and idempotent.** `observation_analyses` is keyed by `inbound_event_id
  UNIQUE` and stores the analyzer version and a fingerprint over the prompt/schema versions, the
  event identity, the source identities and the exact bounded context. A retried event finds its
  analysis and does not call the provider again; a test proves the provider request count stays at
  one across a simulated crash and reclaim.
- **Nothing here can create work.** The handler imports no task, case, planner, scheduler, work,
  fact, playbook, action, approval or draft service, and names no such type; the only durable write
  it performs is the analysis row. An architecture test asserts both, and an integration test
  snapshots every table before and after to prove that only `observation_analyses` grows.
- **One more supervised service, and a wider worker precondition.** `web-watch` is added to the
  daemon only when at least one target is enabled, runs an immediate sync and then the poll
  interval, and isolates a failing target as that target's result. Because a manual input and a
  watched page produce events without any mail account, the `event-worker` now starts whenever a
  usable model exists — and without one, observations and events simply stay durable and unprocessed
  instead of being discarded.

## Alternatives Considered

- **A generic "fetch this URL" command or tool.** It is the shortest path to a useful feature and
  the shortest path to an SSRF primitive a model can aim. Rejected; the URL is configuration.
- **Follow redirects, at least within the same host.** Origin drift is exactly what a watcher must
  not have, and "same host" is decided by a `Location` header the server chooses. Rejected; the user
  configures the final URL.
- **Allow `http://` for convenience.** Plaintext watchers can be rewritten in transit, and the
  content is then quoted to a model as if it were the page. Rejected.
- **Render the page with the eHall Playwright capability.** It would handle JavaScript-heavy pages
  and would also hand a configured URL into a browser session that exists to fill in university
  forms. Rejected; the watcher path has no browser at all.
- **Trust the hostname's text rather than its addresses.** A name is a lookup, not a fact, and
  `localhost` is a name. Rejected; every resolved address is checked.
- **Keep a copy of each page version in the database.** It would make diffs easy and would duplicate
  content into the same store that answers questions about the user. Rejected; snapshots are
  content-addressed files and the database keeps the key.
- **Emit a change event for the first fetch.** It is what "watch" seems to mean, and it would flood
  the queue with a site's entire current content. Rejected; the first fetch is a baseline.
- **Trust `ETag`/`Last-Modified` and skip fetches entirely while the validator matches.** A server
  that always says "not modified" would then hide every change. Rejected; the periodic unconditional
  fetch is the correctness rule.
- **Let the model fetch the page to decide what changed.** It would make the model the observer and
  give it a network capability by the back door. Rejected; the diff is computed locally and the URL
  is never sent.
- **Send the whole page to the model when it changes.** Unbounded cost, and most of a page is not
  the change. Rejected; the context is a bounded diff plus a bounded excerpt.
- **Reuse mail analysis instead of a new event type.** A page change and a message are different
  observations with different contexts and different provenance. Rejected; one handler accepts
  exactly two new types and keeps the mail path untouched.
- **Let analysis create a task or a case when the content clearly asks for one.** It is the feature
  everyone wants next, and it would let a stranger's page write into the user's commitments.
  Rejected; this phase produces candidates, and a later phase can argue for a reviewed promotion.
- **Attach the personal knowledge index or confirmed facts to the context.** The page did not ask
  for the user's documents, and a forwarded message is not a reason to read them. Rejected.
- **Retry or drop an observation when the provider is not configured.** Observations are durable
  either way; the worker simply does not start, and the analysis happens after a provider is
  configured. Rejected; nothing is discarded.

## Consequences

- The assistant can notice that a page it was told to watch has changed, and can be handed text by a
  person, without either of them becoming a way to make it fetch or do something.
- The safety story is inspectable: one adapter opens connections and checks addresses, one extractor
  normalizes text, one store addresses it by content, one table records versions, one event type
  names the change, and one closed schema bounds what a model may say about it.
- Costs and limits: a watcher cannot follow a redirect, cannot log in, cannot render JavaScript and
  cannot watch a non-default port — a page that needs any of those has to be configured elsewhere or
  handled by hand; a page that changes on every fetch produces a change per poll, which the
  `full_fetch_every` interval bounds but does not eliminate; the diff is line-based, so a page that
  reformats itself looks like a change; and analysis still requires a configured provider, without
  which the events simply wait.
