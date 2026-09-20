# ADR-0026 — A Same-LAN Mobile Control Plane

## Title

A phone on the same network can read progress, create and finish tasks, edit a reply draft and
approve one exact action — and it cannot send, submit or execute anything.

## Status

Accepted

## Context

The assistant's work happens on a desktop. Its decisions, though, need a person: a deadline
reminder arrives while the user is away from the machine, a draft is ready for review at the bus
stop, and an action prepared in the morning waits for an approval nobody is sitting in front of.
The obvious answer is a phone.

The obvious answer is also where most of this project's risk has come from so far.

- **approval is the security boundary.** Phase 6A exists so that no external side effect can happen
  without a human binding a fingerprint. A phone that could execute would be a second, weaker
  approver — and a phone that could execute *from a link* would be an approval anyone who reads the
  message can replay;
- **a network-facing service is a new class of exposure.** Everything so far has been a local
  process reading local state. The moment a socket listens on a LAN interface, "who may talk to
  it" becomes a real question, and the answer cannot be a header the client supplies;
- **a phone browser is a hostile execution environment.** Cookies can be sent cross-site, links can
  be forwarded, a page can be opened by anyone who reaches the port, and a screenshot of a pairing
  code is a screenshot of a credential;
- **HTTP over a LAN is not HTTPS.** Any design that pretends otherwise — claiming `Secure`, trusting
  a proxy, or hiding a bearer token somewhere "safe" — is worse than acknowledging the limit,
  because it removes the user's ability to reason about what the network can see;
- **convenience endpoints metastasise.** "Run the action from the phone" is one line of code away
  from "retry", "resend" and "submit", and each of those quietly undoes a Phase 6A rule.

Phase 6D therefore builds a *control plane*, not a client. It reaches the same application services
the CLI reaches, with one deliberate exception: the executor is not among them.

## Decision

1. The V1 mobile UI is a same-LAN web control plane.
2. There is no cloud relay, public deployment, VPN integration or third-party auth.
3. The mobile web surface is disabled by default.
4. LAN mode binds locally but rejects non-private and non-loopback clients.
5. `X-Forwarded-For` and other proxy headers are not trusted.
6. Pairing uses a random ≥256-bit one-time token.
7. Pairing tokens are stored only as hashes, and are short-lived and single-use.
8. A successful pairing creates a revocable, hashed web session.
9. Session cookies are `HttpOnly` and `SameSite=Strict`.
10. Mutations require CSRF protection.
11. No user content is embedded unsafely as HTML.
12. The UI loads no third-party JS, CSS, fonts or CDNs.
13. Action approval reuses `ApprovalService`; there is no web-only approval model.
14. Approval remains bound to the exact `ActionRequest` fingerprint.
15. Mobile approval does not execute the action.
16. There is no web endpoint for `ActionExecutionService`.
17. There is no generic action-creation endpoint.
18. There is no generic browser, eHall or SMTP endpoint.
19. Draft edits use the existing optimistic concurrency.
20. Task mutations use the existing `TaskService`.
21. Mobile routes do not call `ModelPort`.
22. The daemon may host the web server as an isolated supervised service.
23. Browser sessions and pairing state are runtime data, not Git state.
24. HTTP-on-LAN is an explicit V1 limitation; secrets and tokens never appear in server logs or URL
    query strings.

Additional frozen details:

- **One package owns the framework.** `fastapi`, `starlette` and `uvicorn` are imported only under
  `adapters/web/`, and an architecture test walks every source file to prove it. The application,
  the domain and the ports stay framework-free, so the UI is a *view* over services that already
  existed rather than a second implementation of them.
- **Bind is not the boundary.** `[mobile] bind` chooses `127.0.0.1` (`loopback`) or `0.0.0.0`
  (`lan`), and nothing else — a public hostname, a proxy-trust switch, a CORS list and a TLS bypass
  are rejected by the parser as unknown keys. `0.0.0.0` listens everywhere; the private-client
  middleware is what keeps the control plane on the trusted network.
- **The peer address is the only evidence of locality.** The decision uses `ipaddress` against the
  socket peer: loopback, private or link-local passes, everything else is refused with `403`. A
  request claiming `X-Forwarded-For: 192.168.1.5` from a public address is refused all the same, and
  a test asserts exactly that.
- **The database never holds a usable secret.** A pairing code, a session token and a CSRF token are
  each stored as a 64-character SHA-256 with a `CHECK` constraint behind it, so the plaintext exists
  only in the response that created it (or, for a pairing code, on the terminal that minted it). A
  pairing code is one-time and lives ten minutes, consumed by a compare-and-set; a session lives
  thirty days and is revocable, which is how a lost phone is cut off.
- **A mutation carries three things.** The session cookie *and* the `X-CSRF-Token` header *and* the
  CSRF cookie, with the header and cookie equal to each other and matching the session's stored
  hash. A cross-site form can carry a cookie but cannot read one, so it cannot produce the header.
  The CSRF cookie is deliberately readable by the page's own script; the session cookie is not.
- **`Secure` is not claimed.** V1 is HTTP on a trusted LAN, so the cookie attributes say exactly
  what is true — `HttpOnly` and `SameSite=Strict`. Claiming transport protection the deployment does
  not have would be the most expensive kind of lie.
- **Every response is locked down.** A CSP of `default-src 'self'; script-src 'self'; style-src
  'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'self'`, plus `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Cache-Control: no-store`. `/docs`, `/redoc` and `/openapi.json` are
  disabled, so the served surface is exactly the reviewed route list.
- **The UI is three static pages and one small script.** No framework, no build step, no CDN, not
  one `http://` or `https://` reference in an HTML, CSS or JS asset. Every piece of user-controlled
  text — a task title, a mail subject, an action payload, a notification — reaches the page through
  `textContent`; `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` and `new Function` appear
  nowhere in the static assets, and a test fails if they do.
- **The API is a fixed list.** Reads: `/api/me`, `/api/dashboard`, `/api/tasks`, `/api/cases`,
  `/api/notifications`, `/api/mail/drafts[/{id}]`, `/api/actions[/{id}]`. Mutations: `/api/pair`,
  `/api/logout`, `/api/tasks`, `/api/tasks/{id}/complete`, `/api/notifications/{id}/read`,
  `PATCH /api/mail/drafts/{id}`, `/api/actions/{id}/challenge`, `/api/actions/{id}/approve`, and
  the two sessionless approval-link routes. A test pins that list exactly: adding a route is a
  reviewed change, not a side effect of a refactor.
- **Task creation is fields, not language.** `POST /api/tasks` accepts a title, a priority, an
  optional estimate and an optional *timezone-aware* ISO deadline, and hands them to `TaskService`.
  There is no interpreter on this path, no model and no natural-language field.
- **Draft editing is the Phase 5C edit.** `subject`, `body` and `expected_version`, calling
  `MailDraftService.edit_draft`. A stale version is `409` with the current version, the recipient
  and the provenance cannot be changed from the phone, and editing a draft that has unresolved
  questions still resets its acknowledgement.
- **Review shows the exact action.** `/api/actions/{id}` returns the id, case, type, status,
  fingerprint, canonical payload, approval state and execution state — the same facts
  `pw action show` prints, with no model summary in between.
- **Approval is the Phase 6A approval.** The challenge flow returns the plaintext token once to the
  browser, which keeps it in memory, shows the fingerprint and the payload, and asks for a second
  confirmation before posting it to `/api/actions/{id}/approve`, which calls
  `ApprovalService.approve`. The response says `executed: false`, and the web adapter cannot import
  `ActionExecutionService` — a test reads the module and asserts it.
- **A link can approve one action, and only that.** `pw mobile approval-link ACTION` mints an
  ordinary challenge and prints `http://<lan-ip>:<port>/approve/<ACTION-ID>#token=<PLAINTEXT>`. The
  token is in the URL *fragment*, which never reaches the server; the page strips it from the
  address bar with `history.replaceState` as soon as it loads, and the token travels only in a JSON
  body. The two sessionless endpoints behind it can do exactly two things — preview one action and
  approve it — and they are still subject to the private-client check.
- **No execution endpoint exists.** Not `/execute`, not `/send`, not `/submit`, not `/retry`, not
  `/resend`: the route table is pinned by a test, and an architecture test asserts the web adapter
  cannot import or name the executor, `smtplib`, the eHall executor, Playwright or `subprocess`. A
  phone reviews and approves. The host runs the action, deliberately, by hand.
- **No model on the path.** `domain/mobile.py`, `application/mobile_auth.py`,
  `store/mobile_sessions.py` and the whole web adapter contain no `ModelPort`, no `StructuredModel`,
  no `InterpreterService` and no `GroundedAnswerService`, and no environment or `secrets` reads —
  the token factory is injected at the composition root, exactly as the approval token is.
- **The routes log nothing.** The server runs with `access_log=False` at `warning` level, so a URL
  with a token cannot end up in a log file, and a test drives a full authenticated session and
  asserts that no pairing code, session token, CSRF token or user-supplied title appears in the
  captured log output.
- **The server is one more supervised service.** When `[mobile] enabled = true`, `assistantd`
  supervises a `mobile-web` service next to `index-sync` and `scheduler`. It implements the daemon's
  `AsyncService`, runs Uvicorn as a task, and shuts it down on the stop event: the task is always
  awaited, so a shutdown never leaves a dangling server, and a crashed web server is retried on the
  usual backoff without touching its siblings.

## Alternatives Considered

- **A public deployment with real authentication.** It would need account recovery, TLS
  certificates, rate limiting and a threat model for the open internet, and it would put personal
  mail behind a hostname. Rejected; V1 is a trusted LAN and says so.
- **Trusting `X-Forwarded-For` so a reverse proxy can be used.** The header is trivially forged by
  anyone who can reach the port. Rejected; the socket peer is the only evidence.
- **Serving over HTTPS with a self-signed certificate.** The name would have to resolve, the
  certificate would have to be installed on the phone, and none of that makes the *client* more
  trustworthy. Rejected as a V1 scope increase; the limitation is documented instead.
- **Storing the pairing code or session token in plaintext for easier debugging.** A database copy,
  a backup or a crash dump would then be a working credential. Rejected; hashes only.
- **A JSON Web Token signed with a server secret.** A stored, revocable session row is simpler,
  works with the existing SQLite store, and makes "revoke this phone" a single `UPDATE`. Rejected.
- **Letting the phone execute an approved action.** It is the point of this phase that it cannot:
  the phone is a convenient place to *decide*, and the host is where a decision becomes an effect.
  Rejected, and there is no route, no import and no service for it.
- **A web approval model separate from `ApprovalService`.** Two approval paths would mean two
  fingerprints and two meanings for "approved". Rejected; the web calls the same service.
- **An `innerHTML` template for the dashboard "because the data is ours".** The data is a mail
  subject written by a stranger. Rejected; `textContent` everywhere.
- **Push notifications through APNs, FCM or Web Push.** Each adds a third party, a credential and a
  delivery path with no approval semantics. Rejected; the dashboard already shows unread
  notifications, and V1 adds no push.
- **A small frontend framework with a build step.** A build step is a second toolchain and a second
  supply chain for five screens. Rejected; static files and a small vanilla script.

## Consequences

- Decisions can be made away from the desk. Reading progress, creating and finishing a task, editing
  a draft and approving one exact action all work from a phone on the same network — and the
  approval still means what it meant when it was typed on a keyboard: this fingerprint, once.
- The security story is inspectable. There are three stored column types and all three are hashes,
  one middleware decides locality from the peer address, one middleware adds the headers, one
  helper requires the three-way CSRF evidence, and the route table is pinned by a test. A reader can
  check the whole surface in a few minutes.
- Costs and limits: HTTP on a LAN means anyone who can read the network traffic can read the
  session cookie, so the control plane belongs on a trusted network only; the pairing code is
  printed to a terminal and must be typed by hand; a phone cannot run an action, so an approved
  action still waits for `pw action execute` on the host; and there is no push notification, so
  "check the dashboard" is the mechanism.
