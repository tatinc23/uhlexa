# Uhlexa voice + avatar — design spec (spec section 22)

Status: **self-approved 2026-10-22 under Ken's overnight-autonomy contract** (Ken answered the scope questions himself; the
remaining calls are Claude's recommendations, logged in §22.12). Builds on the base spec
(`2026-10-22-uhlexa-design.md`) and **addendum v2** (`2026-10-22-uhlexa-keiki-inkbox-addendum.md`, spec §21), which
overrides the base spec where they differ. If this file and a research source disagree about a provider, the source wins
and this file gets fixed.

## 22.0 What Ken decided (his answers, 2026-10-22)

1. Voice in the < 3-minute video = **three must-haves**:
   - **Simulated Echo + Alexa-app views** in the web simulator: speak to Uhlexa, it answers out loud. Not a real Echo, not
     a classic Alexa custom skill.
   - **One Uhlexa talking face** (Runway real-time Character) in a VibeGen-style hub (the "Echo Show") that shows the
     family's tasks and info. Faces for the other two agents are a stretch goal.
   - **A real phone call** to a family agent's Inkbox number, answered live by the agent.
2. **A setting so the user does not have to say the wake word before every utterance.**
3. Approach **A**: one LiveKit voice worker; the Keiki agent is the brain on screen too; Runway is the face layer.
4. Overnight: build it, read keys for live checks (≤ 3 small billed tests), push + draft PR, generate a custom Uhlexa face.

Architecture intent that still binds (project memory `uhlexa-day1-scope`, addendum §21.0): each person's agent is a
**Keiki-managed LLM**; **Uhlexa is the agent's MCP tools plus the authority** (Postgres, policy engine, human-only
approvals). Only Alexa is simulated; every channel and provider is real. Brand is "Uhlexa", never Alexa or Amazon.

## 22.1 Ground truth this design relies on (checked 2026-10-22)

| Fact | Source |
|---|---|
| Deadline Fri Oct 23 12:00 pm PDT; video < 3 min; judges may judge from video + text | Devpost rules |
| Runway Characters has a **LiveKit mode**: "you control STT, LLM, and TTS, Runway provides the avatar video layer"; billed while the avatar worker is active; cap with `max_duration`; `AvatarSession.start()` can fail (e.g. no credits) and the app must tell the UI | docs.dev.runwayml.com Characters (LiveKit Agents page) |
| Runway native sessions (their LLM + voice + tool calling) are capped at 5 minutes; Characters are created from one image | same docs (Core Concepts, Create your own) |
| `livekit-plugins-runway` 1.5.8: `AvatarSession(avatar_id=… or preset_id=…, max_duration=…)`, key from `RUNWAYML_API_SECRET` | installed package in `tatclaw/agent/.venv` |
| LiveKit Agents 1.5.8: `Agent.stt_node/llm_node/tts_node` overridable; `on_user_turn_completed` + `StopResponse` to skip a reply; participant attribute `lk.agent.state` ∈ initializing, idle, listening, thinking, speaking; explicit dispatch via `agent_name`, either by API or **inside the user's token** (`roomConfig` claim, `AccessToken.with_room_config`) | installed package |
| tatclaw runs this exact stack (`agent_v2.py`: LiveKit + Runway preset, one-session gate); v1 used Groq Whisper STT + OpenAI TTS + Silero VAD; VibeGen Meet (`ui/vibegenmeet/`) = agent tiles with a state machine + card canvas + activity | tatclaw repo (read-only reference) |
| tatclaw notes a "Runway one-session limit" (`tatclaw/CLAUDE.md:297`) | tatclaw repo; current account limit unverified (§22.11) |
| Keiki gateway client already exists: `KeikiGateway.stream_text(agent_id, conversation_id, text, system=, reasoning_effort=)` yields text deltas | `app/providers/keiki/gateway.py` (addendum Task 14) |
| Phone calls: Inkbox media WebSocket with Inkbox STT+TTS (text in, text out, `barge_in`, `clear`); hosted Voice AI cannot call Uhlexa tools | inkbox.ai/docs media-stream, hosted-call-agent |

## 22.2 Surfaces

| Surface | What the viewer sees | Voice path |
|---|---|---|
| **Echo** (`/voice/echo`) | A round speaker with a light ring, a caption line ("heard" / "said") under it, a tap-to-talk button, a wake-mode toggle | LiveKit worker, no avatar |
| **App** (`/voice/app`) | A phone-sized chat: transcript bubbles, a mic button, a state pill, pending approvals with Approve/Deny | LiveKit worker, no avatar |
| **Hub** (`/voice/hub`) | VibeGen Meet layout: main stage = Uhlexa face (or ring fallback) with a state ring; left rail = agent tiles (Alex's, Jordan's, Max's Uhlexa); centre = cards (thread, tasks, approvals with buttons, browser task with screenshot / verified / takeover link); bottom = activity strip + captions | LiveKit worker + Runway avatar |
| **Phone** (real) | The caller's own phone | Addendum §21.5 unchanged (Inkbox ↔ Keiki gateway). The hub shows it through existing activity events only. Spoken in **Inkbox's default TTS voice**: the voice can't be chosen on client-WebSocket calls (not documented; `docs/research/2026-10-22-inkbox-call-voice-facts.md`), so it won't match the on-screen voice |

The MCP Apps Family Board (`ui://uhlexa/family-board.html`) stays voice-free: MCP hosts sandbox the view and the
base spec forbids external requests there. The hub is the standalone "Echo Show".

## 22.3 Architecture and data flow

```
Echo/App/Hub page ── POST /api/voice/session (demo-auth cookie) ──▶ app mints a LiveKit token:
                       room uhlexa-<surface>-<handle>-<rand>, identity human-<handle>-<rand>, 15 min TTL,
                       mic + data publish only, roomConfig.agents=[{agentName:"uhlexa-voice", metadata}]
                       metadata = {handle, user_id, surface, wake_mode, avatar: surface=="hub"}  (server-side only)
page ── joins room ──▶ LiveKit Cloud ──▶ dispatches the worker (python -m app.voice.worker, on Ken's Mac)

worker: Silero VAD + turn detection ─▶ STT ─▶ wake gate (§22.5) ─▶ llm_node = KeikiGateway.stream_text(
          agent_id = KEIKI_AGENT_IDS[handle], conversation_id = u<8hex>-voice, system = VOICE_SYSTEM_LINE,
          reasoning_effort = KEIKI_CALL_REASONING)  ─▶ the Keiki agent calls Uhlexa /mcp tools as usual
        ─▶ TTS ─▶ audio track (echo/app) or Runway AvatarSession (hub) ─▶ page
pages poll GET /api/board every 2 s (existing builder) for threads, tasks, approvals, browser runs, activity
```

- The worker never touches the database and holds no user authority: the handle comes only from job metadata that the
  app signed into the token. Everything it does to household state goes through the Keiki agent's Uhlexa MCP token,
  i.e. through the policy engine.
- One Keiki conversation per person for on-screen voice (`u<8hex>-voice`, a new `voice` value in the gateway's
  `CHANNELS`). Cross-channel continuity comes from Uhlexa threads, as for every other channel.
- `VOICE_SYSTEM_LINE`: "Voice: answer in one or two short spoken sentences. No lists, links, emoji or markdown."

## 22.4 Components (files)

New files (nothing in the build session's files except the merge points in §22.10):

| File | Purpose |
|---|---|
| `app/voice/__init__.py` | package |
| `app/voice/config.py` | `VoiceConfig` read from env (§22.8), `status()` → `disabled / misconfigured / ok` without secrets; builds a `KeikiConfig` from the addendum's `KEIKI_*` names until Task 13 adds `KeikiConfig.from_settings` |
| `app/voice/wake.py` | pure wake-gate logic (§22.5) |
| `app/voice/tokens.py` | LiveKit JWT minting with stdlib `hmac` (HS256): `video` grant + `roomConfig.agents` dispatch. Claim names copied from `livekit-api`'s `AccessToken` and pinned by a contract test |
| `app/voice/brain.py` | `stream_reply(...)`: wraps `stream_text`, adds the 2.5 s filler, the 20 s cap, a daily turn cap and a spoken apology on errors |
| `app/voice/worker.py` | LiveKit `AgentServer` entrypoint (`agent_name="uhlexa-voice"`): session = VAD + STT + `UhlexaVoiceAgent` (overrides `on_user_turn_completed` for the gate, `llm_node` for the brain) + TTS; Runway `AvatarSession` when `metadata.avatar`; publishes `uhlexa.voice.awake` attribute; one avatar at a time; inactivity close |
| `app/api/voice.py` | `GET /api/voice/status`, `POST /api/voice/session`, `GET/PUT /api/voice/settings`; static mount `/voice` → `web/voice` |
| `web/voice/{echo,app,hub}.html`, `web/voice/voice.js` (shared: session, LiveKit join, state, captions, board polling), `web/voice/voice.css` | the three surfaces; hub layout ported from tatclaw `ui/vibegenmeet` (same owner, copied, not imported) |
| `web/vendor/livekit-client.umd.min.js` | pinned copy of `livekit-client`; no CDN at runtime |
| `requirements-voice.txt` | pinned worker extras (`livekit-agents[runway,silero,turn-detector,groq,openai]`), installed only into `.venv-voice` by `scripts/voice_worker.sh`; the Manufact image and `uv.lock` are untouched |
| `scripts/voice_worker.sh` | create/refresh `.venv-voice` (app + extras), load env, run the worker in dev mode |
| `tests/unit/test_voice_wake.py`, `tests/unit/test_voice_tokens.py`, `tests/unit/test_voice_brain.py`, `tests/integration/test_voice_api.py` | §22.9 |

## 22.5 Wake word and the "no wake word every time" setting

Modes (per person; default `follow_up`):

| Mode | Behaviour |
|---|---|
| `wake_word` | Every utterance must start with the wake phrase, like a classic Echo |
| `follow_up` | The wake phrase (or tap-to-talk) opens a window; after each Uhlexa reply the window stays open for `VOICE_FOLLOW_UP_S` (8 s) from the end of speech, so follow-ups need no wake phrase. Silence closes it |
| `always` | No wake phrase; every final utterance is a turn (conversation mode) |

- Matching is on the **final transcript**, not audio: lowercase, strip punctuation, then accept a leading phrase from
  `{uhlexa, hey uhlexa, ok uhlexa, ulexa, u lexa, uh lexa, ooh lexa, hulexa, alexa, hey alexa}` (STT often hears
  "Uhlexa" as "Alexa"). The phrase is removed before the text goes to Keiki; a bare wake phrase opens the window and
  Uhlexa answers "Yes?".
- The STT is primed with the word "Uhlexa" where the plugin supports a prompt.
- Tap-to-talk works in every mode: the page sends a data message `{"type":"open"}`; the worker opens the window.
- Not woken ⇒ `StopResponse`: no Keiki turn, no cost. The worker sets participant attribute `uhlexa.voice.awake` = `true|false`
  so the ring can show "asleep" vs "listening".
- Storage: `agents.config["voice"]["wake_mode"]` (existing JSON column, no migration). `PUT /api/voice/settings` changes it
  for the principal's own agent; the page also sends `{"type":"wake_mode","value":…}` so a live session switches at once
  (a preference, not a permission).
- Recording tip (DEMO.md): mute any real Echo in the room, and say "Uhlexa", not "Alexa".

## 22.6 Avatar and state display

**The face.** One custom Runway Character "Uhlexa", created from one Runway-generated image: a stylized, friendly,
clearly non-human household assistant that does not resemble Amazon's Echo, Alexa's blue ring or any real person. Its
avatar id goes in `RUNWAY_AVATAR_ID`. Fallback: `RUNWAY_PRESET_ID` (a stock preset), then the ring (no video). In
LiveKit mode the voice is ours (TTS); Runway only lip-syncs it, so no Runway voice is created.

**States** (one function, highest first):

| State | Source | Echo ring / hub overlay | Caption |
|---|---|---|---|
| `offline` | `/api/voice/status` not ok, or the room dropped | grey, still | "Voice is offline" + reason |
| `speaking` | `lk.agent.state == speaking` | bright pulse in time with speech | what Uhlexa is saying |
| `thinking` | `lk.agent.state == thinking` | rotating arc | "One moment…" after 2.5 s |
| `listening` | `lk.agent.state == listening` and awake | steady glow | live "heard" text |
| `needs_your_hands` | board: a browser task in `needs_takeover` for the viewer | amber blink + takeover link (owner only) | "I need you to take over" |
| `waiting_for_you` | board: a pending approval the viewer may decide | amber steady + Approve/Deny | "Waiting for your OK" |
| `working_on_computer` | board: a browser task `starting|running` | violet sweep + screenshot thumbnail | "Working on the family computer" |
| `asleep` | `uhlexa.voice.awake == false` | dim breathing | "Say Uhlexa" |

Palette is Uhlexa's own (coral → violet), not Alexa cyan. Captions are always on, both for accessibility and so the
video reads without sound. Agent tiles on the hub use the VibeGen Meet tile state machine (idle / thinking / speaking),
driven by board activity for the other agents.

## 22.7 Approvals, phone and safety

- **Approvals stay human taps.** Echo shows "approve on the app or hub"; the app and hub show Approve/Deny buttons that
  call the existing `/api/approvals/{id}/approve|deny` with the viewer's session cookie. **Voice never approves**: the
  Keiki model cannot, and a deterministic spoken "approve" is a stretch item, not in tonight's build (§22.13).
- **Phone** is unchanged from addendum §21.5 (inbound and outbound, AI disclosure, transcription notice, adults only).
  This spec adds nothing to the call path.
- Keys (`LIVEKIT_API_SECRET`, `RUNWAYML_API_SECRET`, STT/TTS keys, `KEIKI_GATEWAY_KEY`) stay on the server/worker. The
  browser gets one LiveKit token: one room, 15 min, mic + data only, no room admin, no recording, and an agent dispatch
  it cannot change.
- The mic streams to STT only while a voice page is open and joined; the page shows a mic-on indicator and a Leave
  button. Transcripts are not stored by Uhlexa beyond what Keiki and the tools already record.
- Demo users only (Alex = Ken, Jordan = adult helper). Max has no Keiki agent ⇒ `POST /api/voice/session` answers 409
  "Voice isn't set up for Max". Nobody under 18 uses voice.
- Cost guards: one avatar at a time (a second hub gets voice + ring and a notice); `AVATAR_MAX_DURATION_S` 300;
  avatar closed after `VOICE_IDLE_CLOSE_S` (120) with no speech; `VOICE_DAILY_TURN_CAP` (150) Keiki turns per worker day,
  then a spoken "I've hit today's limit".

## 22.8 Configuration (env; none required for the app to boot)

| Name | Default | Meaning |
|---|---|---|
| `ENABLE_VOICE` | false | voice API + pages on |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | empty | LiveKit Cloud project (wss URL) |
| `VOICE_AGENT_NAME` | `uhlexa-voice` | explicit-dispatch name |
| `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` | `livekit` / `livekit` | LiveKit Inference (billed to the LiveKit project, no extra key); `openai` / `groq` also supported |
| `VOICE_STT_MODEL` / `VOICE_TTS_MODEL` | `deepgram/nova-3` / `cartesia/sonic-3` | LiveKit Inference models (STT gets the keyterm "Uhlexa") |
| `GROQ_API_KEY`, `OPENAI_API_KEY` | empty | only for the `groq` / `openai` providers (worker only) |
| `VOICE_TTS_VOICE` | provider default | TTS voice id |
| `VOICE_DEFAULT_WAKE_MODE` | `follow_up` | when the agent has no saved preference |
| `VOICE_FOLLOW_UP_S` | 8 | follow-up window |
| `ENABLE_AVATAR` | false | Runway face on the hub |
| `RUNWAYML_API_SECRET` | empty | Runway key (worker only) |
| `RUNWAY_AVATAR_ID` / `RUNWAY_PRESET_ID` | empty / empty | custom Character id, else preset |
| `AVATAR_MAX_DURATION_S`, `VOICE_IDLE_CLOSE_S`, `VOICE_DAILY_TURN_CAP` | 300, 120, 150 | cost guards |
| `KEIKI_*` | addendum §21.6 | gateway key, agent id map, reasoning effort |

Status rules match base spec §5: `ok` only when the flag is on and every credential is present.

## 22.9 Testing

- **unit**: wake gate (each mode, every phrase variant, phrase stripping, window open/close timing with an injected
  clock, tap-open, bare phrase ⇒ "Yes?"); token minting (HS256 signature verifies, `video` grant has `roomJoin`, the room,
  `canPublish`, `canPublishSources=["microphone"]`, `canPublishData`, no admin; `roomConfig.agents[0]` carries
  `agentName` + metadata; expiry = 15 min; claim names equal those produced by `livekit-api` when that package is
  installed); brain (filler after 2.5 s with a fake clock, 20 s cap ⇒ apology, Keiki errors ⇒ one short spoken apology,
  daily cap, voice system line and `-voice` conversation id sent, no key in any log line).
- **integration** (FastAPI TestClient): `/api/voice/status` has no secrets in any state; `/api/voice/session` is 401
  without a session, 503 when disabled or misconfigured, 409 for Max, 200 with metadata built from the principal (a
  `handle` in the request body is ignored); `PUT /api/voice/settings` validates the mode and writes only the caller's agent.
- **live smoke** (manual, ≤ 3 billed runs, logged in TASKS.md): one Keiki voice-style turn through `stream_text`; one
  Runway `AvatarSession` capped at 60 s; one end-to-end Echo turn from a browser (Ken's mic). Latency of the first spoken
  word after end of speech is measured and recorded; nothing is claimed until measured.
- CI never calls LiveKit, Runway, Keiki or STT/TTS.

## 22.10 Merge points with the build session (small, listed edits to shared files)

1. `app/main.py`: one line to include the voice router and mount `/voice` (before the MCP mount at `/`).
2. `app/providers/keiki/gateway.py`: add `"voice"` to `CHANNELS`.
3. `.env.example`: append the §22.8 block under "Uhlexa extras".
4. `.gitignore`: add `.venv-voice/`.
5. `docs/DEMO.md` (if present): the recording tips from §22.5.

## 22.11 Unknowns and blockers (tracked in BLOCKERS.md by the build)

- Runway concurrent-session limit on Ken's account (tatclaw says one). Decides the stretch faces.
- LiveKit Cloud project: tatclaw's project URL is in its env; whether it is active and its free-tier minutes are
  unchecked until the live check.
- Keiki turn latency with tool calls (addendum §21.11) — the biggest risk to the voice feel. Mitigation: filler, low
  reasoning effort, short replies, a capable-but-fast model for the recording.
- STT accuracy for "Uhlexa": mitigated by the phrase list and the STT prompt; measured in the smoke test.
- Whether Manufact's gateway passes the voice API is irrelevant for tonight: the worker and the voice pages can run from
  the local app; the token endpoint works wherever the app runs.

## 22.11a Live results (2026-10-22 build, details in `docs/VOICE.md`)

- Runway: `gwm1_avatars` allows **1 concurrent session** (stretch faces blocked). Character "Uhlexa" created:
  `aaaf93f6-908d-4d44-b53a-698a4b030545`. Face joined ~4 s and streamed ~8.5 s after dispatch. 22 credits for all tests.
- STT/TTS: OpenAI key has no credits (429), Groq key rejected (401) ⇒ LiveKit Inference. Cartesia `sonic-3` first audio
  0.62–0.66 s; Deepgram `nova-3` final in ~1 s and hears "Uhlexa" as "Alexa," (keyterm did not change it) ⇒ the worker's
  `stt_node` rewrites a leading wake phrase to "Uhlexa," for captions and the gate.
- Keiki: gateway on (Ken), agent "Uhlexa" `1cafe1d6-…`. Spoken end-to-end turn with synthesized speech: transcript
  +0.33 s after end of speech, filler +3.2 s, answer done +10.0 s; Keiki's first word ~5.9 s with no tool calls.
- Pages verified against the live worker in the in-app browser (mic blocked there): join, states, tap-to-talk, 8 s
  follow-up expiry, hub face, clean leave (fixed: the job now ends when the person leaves).

## 22.11b Ken's demo decisions on the phone call (2026-10-22, his words)

- **Different Uhlexas in the demo:** on the phone call Ken talks to a friend's or his mom's agent, so the different
  (Inkbox default) phone voice is a feature: each household agent sounds like its own Uhlexa.
- **Show the precaution:** the call opens with the AI disclosure and transcription notice, and the video shows it to
  the judges on purpose.
- **Outbound calls wait for "Hello?":** the agent speaks after the callee's first final transcript, not at socket
  open; voicemail hangs up.

## 22.12 Decisions log (Claude's calls under the overnight contract; Ken may reverse any)

- Keiki stays the brain for on-screen voice (`llm_node` → `stream_text`) — matches memory intent; alt: Gemini Realtime (rejected: second brain).
- Runway LiveKit mode, not native sessions — our brain and TTS, no 5-min session cap; alt: native + forwarding server tool.
- Wake gating on final transcripts, not an on-device wake-word model — no extra model, works with any STT; alt: Porcupine-style keyword spotting (licence + setup).
- Default mode `follow_up` — Ken's "don't say it every time" without an always-hot mic by default; alt: `always`.
- Settings stored in `agents.config.voice` — no migration in a shared migration chain; alt: new column.
- Dispatch inside the token, minted with stdlib HMAC — no `livekit-api` in the app image and no `uv.lock` change; alt: add `livekit-api` (lock conflicts with the build session).
- Worker extras in `requirements-voice.txt` + `.venv-voice` — the Manufact image and lock stay untouched; alt: a uv dependency group.
- Worker is DB-free and runs on Ken's Mac for the recording — no Neon polling, no new Manufact service; alt: run it on Manufact (single container, unverified).
- Pages under `web/voice/` and `/voice/*`, separate from `web/simulator/` — no edits to the build session's simulator; alt: tabs inside the simulator.
- Approvals stay taps; voice approval deferred — "a model can't approve" stays simple to prove; alt: deterministic spoken "approve".
- Uhlexa face = custom Runway Character from a generated stylized image (Ken approved generating one) with preset fallback.
- STT/TTS = LiveKit Inference by default — the OpenAI and Groq keys failed the live check; alt: wait for Ken to fund OpenAI.
- Keys for the live checks came from `tatclaw/agent/.env.local` (LiveKit, Runway — the setup Ken offered) and the build
  worktree's `.env.secrets.local` (Keiki) because `op` hung; copied into this worktree's gitignored, 0600
  `.env.secrets.local`; alt: stop and wait for 1Password.
- livekit-client pinned to 2.22.3 (released 2026-09-07), not the 6-day-old 2.23.0.
- Worker jobs run in threads (`JobExecutorType.THREAD`) so the one-face lock and daily cap are shared; preemptive
  generation is off so partial transcripts never start billed Keiki turns.

## 22.13 Scope order if time runs short

1. Worker + Echo page (voice only) — proves "talk to Uhlexa". 2. App page. 3. Hub with the face. 4. Settings UI
(the mode still works from env/default). Stretch, in order: faces for Jordan's and Max's agents (only if Runway allows
three sessions), deterministic spoken approval, Inkbox call card on the hub.

Out of scope: a real Echo or Alexa custom skill; Gemini Realtime; Runway native sessions; voice inside MCP Apps views;
on-device wake-word models; storing audio.
