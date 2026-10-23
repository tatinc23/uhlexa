# Uhlexa voice + face — run and record guide

Spec: `docs/superpowers/specs/2026-10-22-uhlexa-voice-avatar-design.md` (§22). Plan: `docs/superpowers/plans/2026-10-22-uhlexa-voice-avatar.md`.

Three pages talk to the same LiveKit voice worker. The worker turns speech into text (STT), sends it to the person's
**Keiki agent** (the brain, which calls Uhlexa's MCP tools), and speaks the reply (TTS). The hub adds the **Runway
Uhlexa face**. Real phone calls are separate (addendum §21.5, Inkbox).

| Page | URL | What it is |
|---|---|---|
| Uhlexa speaker | `/voice/echo` | round speaker, light ring, captions, tap-to-talk, wake-mode toggle |
| Uhlexa app | `/voice/app` | phone chat with captions, approvals (tap), browser-task card |
| Uhlexa hub | `/voice/hub` | VibeGen-style "Echo Show": live Uhlexa face, family agents, cards, activity, captions |

## Run it on this Mac (two terminals)

```bash
scripts/voice_worker.sh download-files
```

```bash
scripts/voice_worker.sh dev
```

```bash
scripts/voice_preview.sh 8765
```

Then open http://localhost:8765/voice/echo (or `/voice/app`, `/voice/hub`) in Chrome, pick Alex, press **Start**, allow
the microphone.

Both scripts read `.env` and the gitignored `.env.secrets.local`. The voice session wrote that file on 2026-10-22 with
LiveKit + Runway keys (copied from `tatclaw/agent/.env.local`, the setup Ken offered) and the Keiki key (from the build
worktree's `.env.secrets.local`). JSON values must be quoted there, e.g. `KEIKI_AGENT_IDS='{"alex":"<agent id>"}'`.

## Before the recording (Ken)

1. **Keiki: turn the API gateway on** for Ken's agent (dashboard → Agents → the agent → API gateway → Turn on). The live
   check on 2026-10-22 found **no agent with the gateway on**, so every voice turn would answer "Sorry, I couldn't reach
   my brain just now." Add Katie's agent id to `KEIKI_AGENT_IDS` when it exists.
1. **Keiki agents:** each person's agent needs the API gateway on (dashboard → Agents → the agent → API gateway) and an
   entry in `KEIKI_AGENT_IDS` (`alex` = Uhlexa, `katie` = Jordy; both done 2026-10-23). After editing
   `.env.secrets.local`, restart the voice worker, which reads it at start.
2. **Mute any real Echo** in the room and say **"Uhlexa"**. (STT hears it as "Alexa,"; the worker accepts that and shows
   "Uhlexa," in captions.)
3. Wake modes (toggle on every page, saved per person): *Say Uhlexa every time*, *Follow-up* (default: after Uhlexa
   answers, you have 8 s to keep talking without the wake word), *Always listening*.
4. Approvals are **taps** on the app or hub. Voice never approves.
5. Only one Runway face can run at a time (account limit, see below). Close other hub tabs before recording.

## Live check results (2026-10-22, voice session)

| Check | Result |
|---|---|
| LiveKit project (tatclaw's) | API OK; worker registers as `uhlexa-voice`; explicit dispatch via the signed token works |
| Runway account | 37,140 credits at start, 37,118 after all tests (22 credits: face image, Character, two face sessions). `gwm1_avatars` **max 1 concurrent session** |
| Uhlexa Character | created from a generated stylized image: avatar id `aaaf93f6-908d-4d44-b53a-698a4b030545` (READY). Face joined ~4 s and streamed video ~8.5 s after dispatch |
| STT/TTS | OpenAI key: **no credits** (429). Groq key: **rejected** (401). Switched to **LiveKit Inference** (billed to the LiveKit project): Cartesia `sonic-3` TTS first audio 0.62–0.66 s; Deepgram `nova-3` STT final in ~1 s, hears "Uhlexa" as "Alexa," |
| Keiki gateway | key valid (`agents:chat`); at first 0 agents with the gateway on, then Ken turned it on: agent **"Uhlexa"** `1cafe1d6-…` |
| Pages | Echo, app, hub verified in the in-app browser against the live worker: join, agent state, tap-to-talk wake, 8 s follow-up expiry, hub face video, clean leave (the in-app browser blocks the mic) |
| **Spoken end-to-end turn** | synthesized "Uhlexa, what can you help my family with today?" played into Alex's mic track (no real mic): final transcript **+0.33 s** after end of speech (caption "Uhlexa, …"), thinking +0.94 s, Uhlexa speaking (filler) **+3.2 s**, full answer done +10.0 s, follow-up window open +10.4 s |
| Keiki turn latency | ~5.9 s to the first word on a turn with no tool calls (`reasoning_effort=none`); the filler covers the gap. The model on the dashboard agent decides most of this |
| 1Password | `op` hung (sandboxed and unsandboxed), likely a pending macOS permission popup; keys were taken from the files above instead |

### Live results: production mode `scripts/voice_worker.sh start` (2026-10-23, helper session)

**Pass.** Ran `scripts/voice_worker.sh start` (not `dev`) in the background, with `VOICE_AGENT_NAME=uhlexa-voice-prodtest`
so the dispatch could not land on the older `dev` worker that was still running under `uhlexa-voice`. Nothing else
differs from production.
- Log showed `registered worker` (agent `uhlexa-voice-prodtest`, LiveKit region US West C).
- A small script minted a token with `app.voice.tokens.mint_token` (handle `alex`, a UUID `user_id`, avatar off) and
  joined a fresh room with it. The token's signed `roomConfig` dispatched the worker: `received job request`, an agent
  participant joined **1.2 s** after connect, the session started, and it closed cleanly
  (`participant disconnect`). No `ERROR` or `WARNING` lines in the worker log. Worker stopped afterwards.
- Not tested in this run: a spoken turn, Keiki, and the Runway face (avatar off, so no credits spent).
- Gotchas: (1) `start` binds the health server on port 8081, which a sandboxed shell refuses (`PermissionError ... port
  8081`, then LiveKit's own `AssertionError` while shutting down); run it outside the sandbox. (2) A `dev` worker and a
  `start` worker under the same agent name both take jobs, so run only one on recording day.
- Test script (not committed): mint the token, `rtc.Room().connect(url, token)`, wait for a participant of kind AGENT.

Still to do before recording:
- ~~Replace the Keiki agent's "younger sister of Alexa" prompt~~ done 2026-10-22; both agents got a persona prompt and
  `reasoning_effort: none` on 2026-10-23.
- ~~Attach Uhlexa's MCP server to each Keiki agent~~ done 2026-10-23 (static `Authorization: Bearer` header; both tokens
  verified against `/mcp`). A turn that needs the family board is slow (27-34 s, mostly the board's 50 SQL statements to
  Neon; BLOCKERS.md), so the spoken tool turn is not yet recording-ready.
- Pick a fast model for the recording; both agents still run `meta/muse-spark-open`.
- One real-mic take in Chrome (`/voice/echo`), since the in-app browser can't use the microphone.
