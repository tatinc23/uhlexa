# Uhlexa voice + avatar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Talk to Uhlexa out loud on simulated Echo / app / hub pages, with a Runway face on the hub, the Keiki agent as the brain, and a "no wake word every time" setting.

**Architecture:** The app mints a LiveKit token that also dispatches the `uhlexa-voice` worker with server-built metadata. The worker (LiveKit Agents 1.5, run on Ken's Mac) does VAD → STT → wake gate → `KeikiGateway.stream_text` → TTS, and adds a Runway `AvatarSession` for the hub. Pages are plain HTML/JS under `web/voice/`, polling the existing `/api/board`.

**Tech Stack:** Python 3.12, FastAPI, stdlib `hmac` JWT, `livekit-agents[runway,silero,turn-detector,groq,openai]~=1.5` (worker venv only), `livekit-client` UMD (vendored), Runway Characters.

**Spec:** `docs/superpowers/specs/2026-10-22-uhlexa-voice-avatar-design.md` (§22). Also read addendum §21.3 (gateway) and §21.5 (phone; unchanged).

## Global Constraints

- No new dependency in `pyproject.toml` / `uv.lock`; worker extras live in `requirements-voice.txt` → `.venv-voice/`.
- Only the shared-file edits in spec §22.10 (main.py line, gateway `CHANNELS`, `.env.example`, `.gitignore`).
- Brand "Uhlexa"; never Alexa/Amazon in UI copy (the wake-phrase list may contain "alexa" as an STT variant only).
- No secret in any response, page, log line or commit. Browser gets one LiveKit token: 15 min, mic + data only.
- Worker is DB-free; the user handle comes only from token metadata built by the server.
- Voice never approves anything. Approve/Deny are taps calling the existing approvals API.
- Pages: no CDN or third-party request at runtime except the LiveKit Cloud connection itself; escape all text.
- Tests run with `.venv/bin/python -m pytest` (uv panics inside the Claude sandbox); CI never calls LiveKit, Runway, Keiki or STT/TTS.

## Review Focus

- Kid or unbound user opens a voice page → clear 409 message, no token, no worker dispatched (Task 3 `test_session_refuses_user_without_keiki_agent`).
- Request body tries to pick another user (`{"handle":"jordan"}` while signed in as alex) → ignored; metadata says alex (Task 3 `test_session_metadata_comes_from_principal`).
- STT renders the wake phrase as "Alexa," / "Hey, Uhlexa!" / "uh lexa" → still wakes and the phrase is stripped (Task 1 `test_wake_variants`).
- Keiki slow or failing mid-stream → one filler, then one short spoken apology; never silence, never a stack trace (Task 4 `test_slow_first_delta_speaks_filler`, `test_gateway_error_becomes_apology`).
- Avatar fails to start (no credits / bad id) → hub keeps voice, shows the ring and a notice (Task 5 manual check M3; worker sets attribute `uhlexa.avatar` = `failed`).

---

### Task 1: Voice config and wake gate

**Files:**
- Create: `app/voice/__init__.py`, `app/voice/config.py`, `app/voice/wake.py`
- Test: `tests/unit/test_voice_wake.py`, `tests/unit/test_voice_config.py`

**Interfaces:**
- Produces: `WakeMode = Literal["wake_word","follow_up","always"]`; `WAKE_MODES: tuple[str, ...]`;
  `class WakeGate(mode: WakeMode, follow_up_s: float = 8.0, clock: Callable[[], float] = time.monotonic)` with
  `decide(transcript: str) -> WakeDecision`, `open_window() -> None`, `reply_finished() -> None`, `set_mode(mode) -> None`, property `awake: bool`;
  `@dataclass(frozen=True) WakeDecision(respond: bool, text: str, bare_wake: bool)`.
- Produces: `@dataclass(frozen=True) VoiceConfig` with fields per spec §22.8 (secrets `repr=False`) and `VoiceConfig.from_env(env: Mapping[str,str] = os.environ)`,
  `status() -> dict[str,str]` (`voice`, `avatar` each `{status, detail}`; `ok` only if flag on and creds present), `keiki_config() -> KeikiConfig`
  (from `ENABLE_KEIKI`, `KEIKI_API_BASE_URL`, `KEIKI_GATEWAY_KEY`, `KEIKI_AGENT_IDS` JSON, `KEIKI_GATEWAY_TIMEOUT_S`, `KEIKI_CALL_REASONING`), `agent_id_for(handle) -> str | None`.

- [ ] **Step 1: Write failing tests** — `test_wake_word_mode_requires_phrase_each_time`, `test_follow_up_window_opens_after_reply_and_expires` (fake clock: respond at +7.9 s, not at +8.1 s), `test_always_mode_responds_without_phrase`, `test_wake_variants` (each of `"Uhlexa, add milk"`, `"hey uhlexa add milk"`, `"Alexa, add milk"`, `"uh lexa add milk"`, `"Hey, Uhlexa! add milk"` → respond, text `"add milk"`), `test_bare_wake_phrase_opens_window` (`"Uhlexa."` → `respond=True, bare_wake=True, text=""`), `test_open_window_from_tap`, `test_phrase_not_at_start_does_not_wake` (`"tell alexa hi"` in wake_word mode → no), `test_status_never_contains_secret_values`, `test_status_misconfigured_when_livekit_missing`, `test_keiki_agent_ids_json`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/test_voice_wake.py tests/unit/test_voice_config.py -q` → FAIL (import errors).
- [ ] **Step 3: Implement.** Normalize: lowercase, replace non-alphanumerics with spaces, collapse spaces; match longest phrase first from spec §22.5 list at the start of the normalized text; return the remainder of the **original** text after the same number of words, stripped of leading punctuation. Window: `open_window()` sets `open_until = now + follow_up_s`; `reply_finished()` does the same; `decide` in `follow_up` responds if phrase or `now <= open_until`.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(voice): wake gate and voice config`.

### Task 2: LiveKit token minting

**Files:** Create `app/voice/tokens.py`; Test `tests/unit/test_voice_tokens.py`

**Interfaces:**
- Produces: `mint_token(*, api_key: str, api_secret: str, room: str, identity: str, name: str, agent_name: str, metadata: dict, ttl_s: int = 900, now: float | None = None) -> str`.
- Claims (verified against `livekit-api` 1.x `AccessToken`): `{"sub": identity, "iss": api_key, "nbf", "exp", "name": name, "video": {"roomJoin": true, "room": room, "canPublish": true, "canSubscribe": true, "canPublishData": true, "canPublishSources": ["microphone"]}, "roomConfig": {"agents": [{"agentName": agent_name, "metadata": json.dumps(metadata, separators=(",",":"))}]}}`, header `{"alg":"HS256","typ":"JWT"}`, base64url without padding.

- [ ] **Step 1: Tests** — `test_token_signature_verifies` (recompute HMAC), `test_token_grants_are_minimal` (no `roomAdmin`, `roomCreate`, `roomRecord`; sources == `["microphone"]`), `test_token_dispatches_worker_with_metadata`, `test_token_expires_after_ttl` (`exp - nbf == 900`), `test_claims_match_livekit_api_when_installed` (`pytest.importorskip("livekit.api")`; build the same token with `AccessToken`, compare decoded claim keys except `nbf/exp`).
- [ ] **Step 2–4:** FAIL → implement with `hmac`, `hashlib.sha256`, `base64.urlsafe_b64encode`, `json` → PASS. **Step 5: Commit** `feat(voice): LiveKit token minting with agent dispatch`.

### Task 3: Voice API and static pages mount

**Files:** Create `app/api/voice.py`; Modify `app/main.py` (one `app.include_router(voice.router)` + `voice.mount_pages(app)`), `.env.example` (append §22.8 block), `.gitignore` (`.venv-voice/`); Test `tests/integration/test_voice_api.py`

**Interfaces:**
- Consumes: `get_principal` (`app/auth/dependencies.py`), `Principal.agent.config`, `VoiceConfig.from_env()`, `mint_token`.
- Produces (pages rely on these exact shapes):
  - `GET /api/voice/status` → `{"voice": {"status","detail"}, "avatar": {"status","detail"}}` (no auth needed).
  - `POST /api/voice/session` body `{"surface": "echo"|"app"|"hub"}` → `{"url": LIVEKIT_URL, "token": str, "room": str, "identity": str, "surface": str, "wake_mode": str, "avatar": bool}`.
    Errors: 401 (no principal), 503 `voice_unavailable` (status not ok), 409 `voice_not_set_up` ("Voice isn't set up for <Name>") when `agent_id_for(handle)` is None, 422 bad surface.
    Room `uhlexa-{surface}-{handle}-{secrets.token_hex(4)}`, identity `human-{handle}-{secrets.token_hex(3)}`, name = display name,
    metadata `{"handle", "user_id", "display_name", "surface", "wake_mode", "avatar": surface == "hub" and avatar status ok}`.
  - `GET /api/voice/settings` → `{"wake_mode": str, "modes": [...]}`; `PUT /api/voice/settings` `{"wake_mode": ...}` → same; writes `principal.agent.config["voice"]["wake_mode"]` (reassign the dict so the JSON column is flagged dirty), commits.
  - `mount_pages(app)`: `app.mount("/voice", StaticFiles(directory="web/voice", html=True))` and `/voice-vendor` → `web/vendor`, only if the directories exist.
- [ ] **Step 1: Tests** (use `client`, `seeded`, `as_user`, monkeypatched env: `ENABLE_VOICE=true`, `LIVEKIT_*`, `ENABLE_KEIKI=true`, `KEIKI_GATEWAY_KEY=k`, `KEIKI_AGENT_IDS={"alex":"a1","jordan":"a2"}`): `test_status_has_no_secrets`, `test_session_requires_principal` (demo auth off → 401), `test_session_503_when_voice_disabled`, `test_session_refuses_user_without_keiki_agent` (max → 409), `test_session_metadata_comes_from_principal` (alex + body handle jordan → metadata handle alex), `test_session_avatar_only_for_hub`, `test_settings_roundtrip_and_validation` (bad mode → 422; jordan's agent unchanged after alex's PUT).
- [ ] **Step 2–4:** FAIL → implement → PASS (also run the whole suite once: no regressions). **Step 5: Commit** `feat(voice): voice session, status and settings API`.

### Task 4: Brain (Keiki voice turns)

**Files:** Create `app/voice/brain.py`; Modify `app/providers/keiki/gateway.py` (`CHANNELS` += `"voice"`); Test `tests/unit/test_voice_brain.py`

**Interfaces:**
- Consumes: `KeikiGateway.stream_text`, `conversation_id_for(user_id, "voice")` (user id from the token metadata, so the prefix matches the person's other channels).
- Produces: `VOICE_SYSTEM_LINE` (spec §22.3, exact), `FILLER = "One moment."`, `TIMEOUT_APOLOGY = "Sorry, that took too long. Please try again."`, `ERROR_APOLOGY = "Sorry, I couldn't reach my brain just now."`, `CAP_MESSAGE = "I've hit today's voice limit."`;
  `class VoiceBrain(gateway: KeikiGateway, agent_id: str, user_id: str, reasoning: str, filler_after_s=2.5, cap_s=20.0, daily_cap=150, clock=time.monotonic)` with `async def reply(text: str) -> AsyncIterator[str]` (yields text pieces to speak).
- [ ] **Step 1: Tests** with a fake gateway (async generator with programmable delays/errors): `test_streams_deltas_in_order`, `test_slow_first_delta_speaks_filler` (first delta at 3 s → yields `FILLER` first, once), `test_total_cap_yields_timeout_apology`, `test_gateway_error_becomes_apology` (raise `KeikiTurnFailed` after one delta → yields delta then `ERROR_APOLOGY`), `test_daily_cap`, `test_sends_voice_system_line_and_conversation_id`, `test_no_key_in_logs` (caplog).
- [ ] **Step 2–4:** FAIL → implement (`asyncio.wait_for` on the first `__anext__` with `filler_after_s`, then continue awaiting the same pending task; overall deadline `cap_s`) → PASS. **Step 5: Commit** `feat(voice): Keiki voice brain with filler and caps`.

### Task 5: LiveKit worker

**Files:** Create `app/voice/worker.py`, `requirements-voice.txt`, `scripts/voice_worker.sh`

**Interfaces:**
- Consumes: `VoiceConfig`, `WakeGate`, `VoiceBrain`, LiveKit Agents 1.5 (`AgentServer`, `AgentSession`, `Agent`, `JobContext`, `cli`, `StopResponse`, `llm.ChatContext`), `livekit.plugins.silero`, `turn_detector`, STT/TTS plugin chosen by `VOICE_STT_PROVIDER/VOICE_TTS_PROVIDER`, `livekit.plugins.runway.AvatarSession(avatar_id=… or preset_id=…, max_duration=AVATAR_MAX_DURATION_S)`.
- Produces: worker registered as `agent_name="uhlexa-voice"`; participant attributes `uhlexa.voice.awake` (`true|false`), `uhlexa.avatar` (`off|starting|on|failed`); handles data messages `{"type":"open"}` and `{"type":"wake_mode","value":…}` from the human participant only.
- [ ] **Step 1:** `requirements-voice.txt` pins the versions tatclaw runs (`livekit-agents[runway,silero,turn-detector,groq,openai]==1.5.8` and matching plugin versions resolved at install) plus `-e .`. `scripts/voice_worker.sh`: create `.venv-voice` with python3.12 if missing, `pip install -r requirements-voice.txt`, source `.env` + `.env.secrets.local` if present, `exec .venv-voice/bin/python -m app.voice.worker "${1:-dev}"`.
- [ ] **Step 2:** Implement `UhlexaVoiceAgent(Agent)`: `on_user_turn_completed(turn_ctx, new_message)` → `gate.decide(new_message.text_content)`; not respond → set attribute asleep, `raise StopResponse()`; bare wake → `session.say("Yes?")` then `raise StopResponse()`; else replace message content with stripped text. `llm_node(chat_ctx, tools, model_settings)` → async generator over `brain.reply(last user text)`; after speech ends (`agent_state_changed` → `listening` from `speaking`) call `gate.reply_finished()`. Entrypoint: parse `ctx.job.metadata` JSON (reject if handle missing or not in `KEIKI_AGENT_IDS`: log and `ctx.shutdown()`), build session (VAD silero, turn detection multilingual model, STT, TTS), start avatar first when `metadata.avatar` and module-level `_avatar_active` is False (try/except → attribute `failed`), then `session.start(agent, room=ctx.room)`; inactivity close of the avatar after `VOICE_IDLE_CLOSE_S`; shutdown callback closes the avatar.
- [ ] **Step 3: Verify** `bash scripts/voice_worker.sh download-files` (fetches VAD/turn models) then `bash scripts/voice_worker.sh dev` registers with LiveKit (log line `registered worker`). Manual checks: M1 Echo turn from Ken's mic (see Task 7), M2 follow-up without phrase, M3 avatar failure path (bad `RUNWAY_AVATAR_ID` → ring + notice).
- [ ] **Step 4: Commit** `feat(voice): LiveKit voice worker with Runway face`.

### Task 6: Echo, app and hub pages

**Files:** Create `web/voice/echo.html`, `web/voice/app.html`, `web/voice/hub.html`, `web/voice/voice.js`, `web/voice/voice.css`, `web/vendor/livekit-client.umd.min.js` (pinned version from npm, recorded in a header comment)

**Interfaces:**
- Consumes: Task 3 endpoints exactly; `/demo/users`, `/demo/login`, `/api/board` (shape from the build session's board builder; render defensively: missing keys → empty sections), `/api/approvals/{id}/approve|deny`; LiveKit participant attributes `lk.agent.state`, `uhlexa.voice.awake`, `uhlexa.avatar`; transcription events from the room (`RoomEvent.TranscriptionReceived`) for captions.
- Produces: `voice.js` exports `UhlexaVoice.start({surface, onState, onCaption, onBoard})`, `computeState(agentState, awake, board, viewerHandle) -> state` (spec §22.6 precedence), `setWakeMode(mode)`, `tapToTalk()`, `leave()`.
- [ ] **Step 1:** Shared `voice.js`: user picker (from `/demo/users`) → `/demo/login` → `POST /api/voice/session` → `new LivekitClient.Room()` connect, enable mic, subscribe agent audio (and video on hub); poll `/api/board` every 2 s; state machine per spec §22.6; captions from transcriptions; data messages for tap and wake mode; Leave button; offline badge from `/api/voice/status`.
- [ ] **Step 2:** Echo: ring with CSS animations per state (coral→violet palette), caption line, tap button, mode toggle. App: phone frame, bubbles, mic button, state pill, approval cards with Approve/Deny. Hub: port tatclaw `ui/vibegenmeet` layout (left agent tiles with idle/thinking/speaking, main stage with the avatar `<video>` or ring fallback + state ring, card canvas for thread/tasks/approvals/browser task, activity strip, captions). All text via `textContent`.
- [ ] **Step 3: Verify** in the Browser pane against a local app (`ENABLE_VOICE=true`): pages load with no console errors, offline badge when LiveKit env missing, 375 px layout works for app/echo, hub works at 1280 px. **Step 4: Commit** `feat(voice): Echo, app and hub voice pages`.

### Task 7: Uhlexa face, live smoke, docs

**Files:** Modify `TASKS.md` (voice section + session log), `BLOCKERS.md` (voice blockers with fixes); Create `docs/VOICE.md` (run + record instructions, recording tips from spec §22.5)

- [ ] **Step 1:** Runway: generate one stylized Uhlexa image (`rw-generate-image`), create the custom Character (`create_avatar` or the REST equivalent per current docs), poll to `READY`, record the avatar id (not secret) in `.env.example` comment and `docs/VOICE.md`.
- [ ] **Step 2:** Live checks (≤ 3 billed): Keiki `stream_text` one voice turn (record latency to first delta); Runway `AvatarSession` 60 s in a LiveKit room (record start time); STT/TTS key validity (list models / 1-sentence TTS).
- [ ] **Step 3:** Write results, unknowns and Ken's steps (LiveKit keys into env, mute real Echo, run commands) into `docs/VOICE.md`, `TASKS.md`, `BLOCKERS.md`. **Step 4: Commit** `docs(voice): run guide, live smoke results, blockers`.
