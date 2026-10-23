# Hosting Uhlexa: Ken's Mac + Inkbox tunnel + Neon

Ken's decision (2026-10-22): **drop Manufact, keep Neon.** The app runs on Ken's Mac. The `uhlexa` Inkbox identity's
tunnel makes it public at **https://uhlexa.inkboxwire.com**: plain HTTP and WebSockets over one outbound connection, so
no port forwarding is needed. Judges get the repo and the video; nothing has to stay online after the recording.

| Who needs the public URL | Path |
|---|---|
| Keiki agent's MCP connection (Uhlexa tools) | `https://uhlexa.inkboxwire.com/mcp` |
| Inkbox webhooks | `https://uhlexa.inkboxwire.com/webhooks/inkbox/{handle}` |
| Inkbox call WebSocket (Auto accept / `place_call`) | `wss://uhlexa.inkboxwire.com/ws/phone/{handle}` |
| Primitive webhook | `https://uhlexa.inkboxwire.com/webhooks/primitive` |
| People (simulator, Family Board, voice pages) | open the access link once per browser |

## Run

```bash
scripts/run_public.sh
```

It loads `.env` and `.env.secrets.local`, runs migrations on Neon (`NEON_DATABASE_URL`), and starts the app on
127.0.0.1:8000 (`uvicorn app.hosting.public:app`). Then it connects the tunnel (`python -m app.hosting.tunnel`) and
prints the public URL and the **access link**. Ctrl-C stops both. Rehearse on SQLite instead:
`DATABASE_URL=sqlite:///./voice-preview.db scripts/run_public.sh`. The voice worker is separate:
`scripts/voice_worker.sh dev` (docs/VOICE.md).

Needs in `.env.secrets.local`:
- `INKBOX_API_KEY`: the admin key (1Password "Admin-Uhlexa-Key", `<password-manager item>`) or a key scoped to
  `uhlexa`.
- `NEON_DATABASE_URL`.
- `DEMO_ACCESS_TOKEN`: generated on first run if missing.

Optional: `INKBOX_TUNNEL_HANDLE` (default `uhlexa`; `samantha` is refused). The app also needs `inkbox` in its venv
(`uv add inkbox==0.7.20`, already in the build session's to-do list).

## The access gate (why a link)

Demo auth lets anyone who reaches the app act as any family member. That's fine on localhost and not on a public URL,
where a stranger could approve tasks or trigger real calls and texts. `app.hosting.gate.AccessGate` therefore requires
an access cookie on every browser and API route. Open `/access?token=…` once per browser; the cookie is HttpOnly,
Secure behind the tunnel, and lasts 7 days. Machine routes stay open because they carry their own auth: `/mcp` (Keiki
bearer tokens, addendum §21.4), `/webhooks/*` and `/ws/phone/*` (Inkbox/Primitive signatures), `/health`, `/ready`.
`/hosting/ws-echo` is a gated WebSocket echo for checking the tunnel.

## Inkbox Console settings for calls (Ken)

Incoming call handling has five options: Auto accept, Auto reject, Webhook, Inkbox Voice AI, Forward. The voice,
"Default data access" and system prompt fields configure **Inkbox Voice AI**, Inkbox's own phone AI. For the Uhlexa
demo, the family agents' identities need **Auto accept** with the WebSocket URL
`wss://uhlexa.inkboxwire.com/ws/phone/{handle}` (the build session's call route, addendum §21.5), so the Keiki agent is
the brain. If an identity stays on Voice AI, prefer contact-scoped data access over "YOLO — full identity access".

**Lines (admin key check 2026-10-22; phone numbers corrected 2026-10-23 per Ken):**
- Both demo agents have phone numbers: `uhlexa` (Alex's agent) and `jordy` (Katie's agent). The numbers are in the Inkbox
  Console, kept out of this public repo. (The 2026-10-22 check had reported no dedicated phone numbers.)
- `uhlexa` has a **dedicated iMessage line** (`imessage_number`: `dedicated_outbound`, `personal`, `active`; the number is
  in the Inkbox Console, kept out of this public repo) and the mailbox `uhlexa@inkboxmail.com`. Its incoming call action
  was `hosted_agent` (Inkbox Voice AI).
- The docs say iMessage lines can place and receive voice calls, and a dedicated line must be "voice-enabled" and
  "verified" or `place_call` answers 422. The identity API doesn't show those flags, so the first test call settles it.
- The friend's / mom's agents for the call still need their own identities and lines.
**Demo agents' lines (checked 2026-10-23 via the admin key; numbers live in the Inkbox Console, not in this public repo):**

| Identity | Who | Line | Incoming calls today |
|---|---|---|---|
| `uhlexa` | Alex's Uhlexa (Alex = Ken in the video) | dedicated iMessage line (`dedicated_outbound`, active); mailbox `uhlexa@inkboxmail.com` | auto-accept to the call WebSocket (set 2026-10-23 by `scripts/live_setup.py`) |
| `jordy` | Jordy, **Katie's** Uhlexa (Katie is a person in Alex's household, so handoffs stay in-household) | local VoIP number (created 2026-10-23); mailbox `jordy@…` | auto-accept to the call WebSocket (same script) |

(The 2026-10-23 morning version of this table named `grammy-uhlexa` as the other agent; that identity no longer exists
in the org, `jordy` replaced it.) Text and call replies need the sender bound to the **owner** of the addressed agent
(`channel_principal.resolve`), and one number can be bound to one person, so Ken's phone reaches `uhlexa` only. Katie's
own phone is bound to `katie` (consent recorded). Whether a voice call to `uhlexa`'s iMessage line reaches the app is
**not verified** (`place_call` answers 422 if a line isn't voice-enabled; the identity API doesn't show that flag).

## Judging window (Ken: the live URL stays up Nov 9-20)

Leave the Mac on and run, in one terminal:

```bash
caffeinate -dims scripts/keep_alive.sh
```

`scripts/keep_alive.sh` runs the public app + tunnel (`run_public.sh`) and the voice worker (`voice_worker.sh start`),
restarts either 5 s after it exits, and writes `logs/public.log` and `logs/voice.log`.

Before Nov 9, check:
- [ ] Mac: Settings → Battery/Lock Screen: never sleep on power; plugged in; same network; the 1Password and Docker
      apps are not needed.
- [ ] `https://uhlexa.inkboxwire.com/health` answers from a phone on cellular.
- [ ] The access link works in a private window, then the hub shows Uhlexa's face and answers one spoken turn.
- [ ] Keys still valid: LiveKit, Runway (37k credits, ~1 credit per few seconds of face), Keiki credits, Neon.
- [ ] The Devpost private testing field holds the access link with the real token (never the repo or public text).
- [ ] Only one hub open at a time (Runway allows one live face); each judge's session ends the face after 2 idle minutes.
- [ ] To rotate the link: change `DEMO_ACCESS_TOKEN` in `.env.secrets.local`, restart, and update Devpost.

## Verified 2026-10-22

Tunnel `uhlexa` connected from this Mac in ~2 s (edge TLS). Checked from the public side:
- `/health`: 200 in 0.63 s.
- No access cookie: API 401 `access_required`, page 401, WebSocket refused (403).
- Wrong token: 403. Right token: 303 with an HttpOnly + Secure cookie (the tunnel forwards `X-Forwarded-Proto: https`).
- With the cookie: pages and API 200.
- **WebSocket** `wss://uhlexa.inkboxwire.com/hosting/ws-echo`: echo in 0.71 s including connect, and still open after
  20 s idle.

Not yet checked (needs the build session's routes): `/mcp` from Keiki, Inkbox webhooks, and a real call socket.
