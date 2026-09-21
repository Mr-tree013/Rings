# Mobile: the trusted-LAN control plane

## What it does

The phone page lets you look at what is waiting — open tasks, drafts, prepared actions and
notifications — and approve **one exact action**. It is a control plane for review and approval, not
an execution surface and not an Internet service.

## Enable / configure

```toml
[mobile]
enabled = true
bind = "lan"      # loopback | lan
port = 8765
```

`bind = "loopback"` serves only this machine; `bind = "lan"` listens on `0.0.0.0` and relies on the
peer check described below. There is no public hostname, no proxy trust, no CORS list, no TLS
exception and no cloud relay.

```bash
uv run assistantd         # hosts the mobile-web service alongside the others
uv run pw mobile pair     # prints a one-time pairing code, once
```

Open `http://<LAN-IP>:8765/pair` on the phone and paste the pairing code by hand.

### Tree Chat (`/chat`)

The same host also serves the conversational surface at `http://<host>:<port>/chat`, under the same
pairing, session and CSRF rules. It is a chat page rather than a dashboard: conversation history,
thread switching, a queue for messages submitted while Tree is working, coarse activity updates over
server-sent events, and confirmation cards that settle the same decisions the terminal phrases do.
The review-and-approval page above is unchanged and still exists; `/chat` is an addition, not a
replacement. `uv run rings --web` opens it if it is reachable.

Some things to know while using it:

* a message submitted while another one is running is queued, shown as 排队中, and becomes its own
  message — it is never merged with the one before it;
* queued messages survive a page reload and a daemon restart, and a message that was already being
  processed when the process died is marked interrupted rather than replayed;
* "Stop" is offered only while stopping is safe (queued, or before any write has happened); after
  that the server says it cannot stop safely instead of pretending;
* activity lines are coarse product stages — understanding, reading local state, planning, looking
  things up, preparing mail, saving changes, executing a confirmed action — never reasoning;
* confirmation cards carry a version: if the underlying state changed, the click is refused and the
  page reloads the truth, so a stale card can never send, apply or remember anything.

## Common workflow

```bash
uv run pw mobile status
uv run pw mobile pair
uv run pw mobile sessions
uv run pw mobile revoke <SESSION>
uv run pw mobile approval-link <ACTION>
```

On the phone you can: read the dashboard, create and complete tasks, edit a draft, read
notifications, and review a prepared action in full before approving it.

## Commands

```text
pw mobile status
pw mobile pair
pw mobile sessions
pw mobile revoke <SESSION>
pw mobile approval-link <ACTION>
```

The phone mirrors the host-side approval chain (`pw action show`, `pw action challenge`,
`pw action approve`) but never executes.

## Safety behavior

- **Trusted LAN only, HTTP, not public.** The real gate is a private-client check on the socket peer:
  loopback, private and link-local addresses pass, anything else is refused. `X-Forwarded-For`,
  `Forwarded` and `X-Real-IP` are never trusted, so a spoofed header cannot get in.
- There is no TLS in v1, so the session cookie does not pretend to be `Secure`. Treat the LAN as
  trusted and do not expose the port to the Internet.
- **Pairing tokens are single-use and stored as hashes.** The code is printed once by `pw mobile
  pair` and is never placed in a URL. Sessions last 30 days and can be revoked.
- **Mobile may approve but cannot execute.** The approval link carries its token in the URL fragment,
  which the page strips from the address bar as soon as it loads. There is no execute, send, submit,
  retry or resend route, and no generic action-creation endpoint.
- Mutations require an authenticated session cookie plus a matching CSRF header and cookie. The page
  is self-contained — no CDN and no third-party script — and all user content is written with
  `textContent`.
- Access logging is off by default: tokens, cookies, mail bodies and action payloads are not written
  to logs.

## Troubleshooting / limitations

| Symptom | What it usually means |
| --- | --- |
| The phone gets `403` | The request did not arrive from a private address. Check the LAN, not the phone's Wi-Fi captive portal. |
| Pairing code rejected | Codes are single-use and expire after 10 minutes; mint a new one with `pw mobile pair`. |
| A session stopped working after a restore | A restore revokes every mobile session by design; pair again. |

- There is no mobile push notification (APNs / FCM / Web Push) in v1.
- The phone never executes an action: `pw action execute` runs on the host.

## Implementation notes

- Same-LAN mobile control plane: [ADR-0026](../adr/0026-lan-mobile-control-plane.md)
