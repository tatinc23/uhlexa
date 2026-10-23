# Live setup: real texts and calls to the family agents (Ken, 2026-10-23)

## Status (2026-10-23): live setup is DONE on Ken's Mac

`scripts/live_setup.py --apply --reseed` already ran. Do not run `--reseed` again unless you want the demo database reset.
- Two Inkbox agents: **`uhlexa`** (Alex's, named "Uhlexa", iMessage line) and **`jordy`** (Katie's, named "Jordy", VoIP number).
  Webhooks, auto-accept calls and signing keys are set; Alex and Katie are bound to their agents and phones.
- Both Keiki agents have the Uhlexa MCP connection at `https://uhlexa.inkboxwire.com/mcp` (static bearer header, tokens
  minted, personas + `reasoning_effort: none` applied).
- Inbound only. Not built: the agent calling or texting someone on its own, and approval notifications.
- Remaining by hand: Katie's Keiki agent id goes into `KEIKI_AGENT_IDS` (and `scripts/keiki_tokens.py mint katie --apply`) only
  if you want to talk to Jordy by voice; restart `scripts/voice_worker.sh dev` (one worker only) so it loads
  `VOICE_TTS_VOICES`; before recording, click **Reset demo** twice in the simulator top bar.
- The steps below are the reference for redoing the setup; names in `<...>` are placeholders.

Do these once, in order, before recording. Each step says exactly where to click or what to paste. Everything runs on
your Mac (`scripts/run_public.sh`, public at `https://uhlexa.inkboxwire.com`). Placeholders look like `<THIS>`.

## Fast path (does steps 1, 2, 4 and 5 for you)

```bash
.venv/bin/python scripts/live_setup.py --phone <your number, +1...>
```

That is a dry run: it reads Inkbox and the database and prints what it would do. When it looks right, run the same
command with `--apply --reseed` (`--reseed` resets the demo database to the Katie household once). It creates the
webhooks for `uhlexa` and `jordy`, sets auto-accept calls, saves the signing keys and channel flags to
`.env.secrets.local`, binds alex/katie and your phone, and mints Alex's Keiki token. Then do only the Keiki dashboard
paste it prints, restart `scripts/run_public.sh`, and go to step 6. The manual steps below stay as the reference.

## 1. Keys into `.env.secrets.local` (gitignored, in the repo folder)

Add these lines (values from 1Password team vault / the Keiki and Inkbox dashboards):

```
ENABLE_KEIKI=true
KEIKI_GATEWAY_KEY=<Keiki key with the agents:chat scope>
KEIKI_AGENT_IDS={"alex":"<Alex's Keiki agent id>","katie":"<helper's Keiki agent id>"}
ENABLE_INKBOX=true
ENABLE_INKBOX_IMESSAGE=true
ENABLE_INKBOX_SMS=true
ENABLE_INKBOX_CALLS=true
INKBOX_API_KEY=<Inkbox admin key: 1Password "Admin-Uhlexa-Key">
INKBOX_SIGNING_KEYS={"<alex's inkbox handle>":"<its signing key>","<helper's inkbox handle>":"<its signing key>"}
```

`alex` is you and `katie` is your helper in the seeded Parker household (fictional names on screen; the real channel
is your own phone). Each Inkbox handle's signing key is shown once when its webhook subscription is created (step 2).

## 2. Inkbox Console, once per agent identity

1. Open the identity (your agent's handle) → **Webhooks** → **Add subscription**.
2. URL: `https://uhlexa.inkboxwire.com/webhooks/inkbox/<handle>` · events: `imessage.received`, `text.received`.
3. Copy the **signing key** it shows into `INKBOX_SIGNING_KEYS` above (one entry per handle).
4. **Phone** settings → incoming calls: **Auto accept**, WebSocket URL `wss://uhlexa.inkboxwire.com/ws/phone/<handle>`.

## 3. Start the app

```bash
scripts/run_public.sh
```

It prints the access link and the Keiki MCP URL. Leave it running.

## 4. Bind people to their agents and their own phones (in a second terminal, repo folder)

Dry run first (prints what it will do), then the same with `--apply`:

```bash
.venv/bin/python scripts/channels_bind.py identity alex <alex's inkbox handle>
```

```bash
.venv/bin/python scripts/channels_bind.py identity alex <alex's inkbox handle> --apply
```

```bash
.venv/bin/python scripts/channels_bind.py phone alex <your phone, +1...> --consent "Ken, 2026-10-23, own phone" --apply
```

Repeat both for `katie` with the helper's handle and the helper's phone (their consent in `--consent`). Check:

```bash
.venv/bin/python scripts/channels_bind.py list
```

Only bound numbers get answers; every other sender is ignored and logged.

## 5. Give each Keiki agent its Uhlexa tools

```bash
.venv/bin/python scripts/keiki_tokens.py mint alex --apply
```

The token lands in `.env.secrets.local` as `KEIKI_MCP_TOKEN_ALEX` (copy it to 1Password). In the Keiki dashboard →
the agent → **MCP connections** → add `https://uhlexa.inkboxwire.com/mcp` with header
`Authorization: Bearer <that token>`. Repeat for `katie`.

## 5b. Email to your agent through Primitive

The webhook is already registered in Primitive (endpoint `d6cff3a9`, 2026-10-23): it sends `email.received` to
`https://uhlexa.inkboxwire.com/webhooks/primitive`. The signing secret is in 1Password (`<password-manager item>`,
team vault); the command below reads the same secret from Primitive's API so nothing is pasted by hand.

1. Turn Primitive on and add the secret (repo folder; prints only the secret's length):

```bash
K=$(grep -E '^PRIMITIVE_API_KEY=' .env.secrets.local | cut -d= -f2- | tr -d "\"'"); S=$(curl -s https://api.primitive.dev/v1/account/webhook-secret -H "Authorization: Bearer $K" | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["secret"])'); printf '\nENABLE_PRIMITIVE=true\nPRIMITIVE_WEBHOOK_SECRET=%s\n' "$S" >> .env.secrets.local; echo "added, secret length ${#S}"
```

2. Restart `scripts/run_public.sh` (Ctrl-C, run it again).
3. Bind your agent's Primitive address and your own email address (dry run first, then `--apply`):

```bash
.venv/bin/python scripts/channels_bind.py email-inbox alex alex@agentmail.tatinc.us --apply
```

```bash
.venv/bin/python scripts/channels_bind.py email alex <your email> --consent "Ken, 2026-10-23, own address" --apply
```

Agent addresses use TAT's verified Primitive domain `agentmail.tatinc.us` (MX `mx1.primitive.dev`); Katie's would be
`katie@agentmail.tatinc.us`. Only email that Primitive marks as a **verified** sender (`from_verified`) and that comes from the bound address is
answered; anything else is ignored and logged. Each email thread becomes a private conversation on the board.

## 6. Smoke test before recording

1. From your phone, iMessage your agent's line: "What's on the Sunday groceries list?" → a reply within ~10 s.
2. Call your agent's line, say the same → it answers by voice.
3. Email `alex@agentmail.tatinc.us` from your bound address: "What's on the Sunday groceries list?" → a reply on
   the same email thread within ~30 s.
4. If nothing comes back: the `scripts/run_public.sh` terminal shows the request; `Unknown sender` means step 4 (or 5b).

Not built yet (honest): the agent calling or texting someone else on its own (outbound), and notifications after an
approval. Email to the agent's Inkbox address is refused on purpose (Inkbox can't verify the sender); use the Primitive address (5b).
