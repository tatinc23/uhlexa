# Inkbox phone-call voice: what the docs settle and what they don't (2026-10-22)

Sources: Inkbox docs assistant answers to `docs/prompts/2026-10-22-inkbox-docs-ai-questions.md` (pasted by Ken), with
the pages it cited: https://inkbox.ai/docs/api/phone/media-stream, https://inkbox.ai/docs/api/phone/calls,
https://inkbox.ai/docs/signing-keys, https://inkbox.ai/docs/api/phone/webhooks, https://inkbox.ai/docs/api/tunnels,
https://inkbox.ai/docs/capabilities/imessage. Spot-checked here: the signing-keys page, and `inkbox` 0.7.20's
`signing_keys.verify_webhook` source. "Not documented" means the docs don't say. It does not mean the feature is
missing.

## Settled by the docs

| Topic | Fact |
|---|---|
| Our setup | Client WebSocket with Inkbox STT+TTS: handshake headers are only `X-Use-Inkbox-Text-To-Speech`, `X-Use-Inkbox-Speech-To-Text`, `X-Inkbox-Audio-Format`; the `text` event has only `delta`, `done`, `turn_id` |
| Voice choice | The `voice` setting and voice catalog are documented **only for Voice AI** (`hosted_agent`) |
| Barge-in | `barge_in` carries `tts_interrupted`; we must stop sending the old response, then send `clear` |
| Outbound | Inkbox "dials the `to_number` and opens a WebSocket"; call status goes `ringing` → `answered`; `started_at` = answer time; rings up to 60 s |
| Voicemail (client-driven) | `on_voicemail="hang_up"` (default) ends the call on the beep; `ignore` treats whatever answers as a person; `leave_message` needs `hosted_agent` |
| Caller identity | Call record `remote_phone_number` = the other party; `local_phone_number` is `null` on a shared iMessage line |
| Signing | Each identity's signing key signs its webhooks **and WebSocket upgrades** (unsigned until a key exists). Upgrade headers: `X-Inkbox-Request-ID`, `X-Inkbox-Timestamp`, `X-Inkbox-Signature`; verify against the exact `X-Call-Context` value. Webhooks: HMAC-SHA256 over `{request_id}.{timestamp}.{raw_body}`, `sha256=<hex>`, 300 s window. SDK `verify_webhook` implements exactly that |
| URL | `client_websocket_url` must be `wss://` |
| Tunnels | Route inbound WebSocket traffic; `edge` (TLS at Inkbox) or `passthrough` (TLS in our process) |
| Voice AI | "Fixed built-in toolset" (no MCP or our webhook tools are mentioned) |
| Limits | Outbound concurrency 3 per org (or per dedicated iMessage line), then `429 concurrent_calls_limit`; max call 10 min Free, 60 min Developer/Startup/Enterprise, ends with `hangup_reason: "max_duration"` |
| Shared iMessage | A person texts `connect @handle` to the router first; inbound calls follow the incoming-call config |
| Webhooks | Event `id` is stable across retries; missed deliveries can be replayed |

## Not documented

- Choosing the TTS voice on our calls, and whether the identity's Voice AI voice applies to them.
- TTS buffering (first delta or sentence end) and delta→audio latency; pronunciation control (SSML, phonemes).
- STT keywords, language, endpointing, and final-transcript delay.
- Whether Inkbox stops playback on barge-in by itself, and the event order.
- When the outbound WebSocket opens (at dial or at answer) and who should speak first.
- Which party `X-Call-Context.phone_number` names.
- The exact string signed on the WebSocket upgrade and its replay window.
- Certificate, keepalive, idle-timeout, message-size and reconnect rules; tunnel time limits for the call socket.
- Inbound call concurrency; what happens to an active call at a limit.
- Any automatic recording or AI-disclosure notice.
- How a person dials an agent by voice on a shared iMessage line.
- The webhook retry schedule.

## What this changes in the design

1. **Phone voice = Inkbox's default TTS voice.** We can't pick it, so it won't match the on-screen Uhlexa voice
   (Cartesia via LiveKit). Accepted for the deadline. A matching voice later means `X-Use-Inkbox-Text-To-Speech: false`
   and sending our own audio (`pcm_s16le_16000`) from the same TTS the voice worker uses.
2. **Disclosure is ours.** Nothing says Inkbox announces AI or transcription, so the call session must speak the
   disclosure itself (addendum §21.5 already requires this).
3. **Outbound opening:** the socket may open before the callee answers. Speak only after the first final
   `transcript` from the callee (usually "Hello?"), with a short fallback if nothing arrives, rather than right at
   `start`. Set `on_voicemail="hang_up"` explicitly.
4. **Signature check is plausible but unproven.** `app/providers/inkbox/{webhook,calls}.py` verify
   HMAC-SHA256(`{request_id}.{timestamp}.` + exact `X-Call-Context`, key without `whsec_`), 300 s window. That matches
   the SDK's `verify_webhook` and the docs' "verify against the exact `X-Call-Context` value". The first real call
   confirms it. If every call closes with `invalid_signature`, this string is the suspect; never fall back to unsigned.
5. **Identify the caller from the call record's `remote_phone_number`**, as the addendum already does, not from
   `X-Call-Context.phone_number`.
6. **Barge-in:** keep the addendum order (stop the stream, then send `clear`); don't assume Inkbox cut the audio.
7. **Limits for the demo:** stay under 3 concurrent outbound calls; keep calls well under 10 minutes unless the org is
   on a paid plan.

## Round 2 (Inkbox agent, docs-only) and the decision

The Inkbox agent answered from the docs only and marked items 1-14 of
`docs/prompts/2026-10-22-inkbox-agent-followup-questions.md` [unknown] beyond what is above. **Decision (2026-10-22): no
support ticket.** One live test call, once the call route is deployed, settles what the demo needs:
- the signature passes (or not);
- who speaks first on outbound (we wait for "Hello?" either way);
- whether Inkbox plays its own notice (avoid a double disclosure).

**Fallback if the signature check fails on that call:** for the demo, accept the upgrade only on an unguessable
per-identity secret in the WebSocket path (`/ws/phone/{handle}/{secret}`, secret in `.env.secrets.local`), log the
mismatch, and keep the HMAC check behind a flag until the signed string is known. Never accept signed-looking
traffic that failed verification as if it passed.
