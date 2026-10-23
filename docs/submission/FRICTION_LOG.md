# Friction Log: Uhlexa (Alexa+ track)

Optional "Friction Log Entries" for the Devpost rules (up to 10% judging bonus). 15 entries, ordered by severity, then
feature requests. Each entry gives task attempted, steps taken, expected vs actual, severity, workaround and an
actionable suggestion. All dates are 2026-10-22 unless stated. "Source" points to a file in the project repo
(`docs/research/` unless a path is given). Our reading is marked **(inference)**.

> **Names:** every tool is named publicly by Ken's decision (2026-10-22): they are public products used under their terms. No vendor logos are shown.

Severity: **Blocker** needed a human; **High** changed the design or blocks a demo path; **Medium** needed a
workaround; **Low** annoyance.

## 1. Primitive: getting an API key (Blocker)
- **Task attempted:** make the first authenticated Primitive call (read a thread, put a memory).
- **Steps taken:** probed unauthenticated routes (`/v1/health`, `/v1/discovery`, demo emails, MCP `tools/list`); read
  the account-creation paths.
- **Expected:** an agent can obtain a usable key for a developer account.
- **Actual:** account creation needs terms acceptance and an emailed 6-digit code, so an agent may not do it. The
  zero-touch agent path gives a reply-only "emailless" plan (10 sends/hour, 120 requests/minute); the docs do not say
  whether that plan can create webhook endpoints. Every protected route returned 401 until Ken created the account.
- **Severity:** Blocker (until a human acted).
- **Workaround:** Ken signed up and stored the key in 1Password; we built the verifier and client offline against the
  12 published signing vectors and the OpenAPI file. The live smoke test is still open in `TASKS.md`.
- **Suggestion:** offer a human-approved key request (a link the user clicks) from the agent signup flow, and state
  what the emailless plan can and cannot do.
- **Source:** `2026-10-22-primitive-facts.md` §6 Blockers; `BLOCKERS.md` (Resolved, Primitive API key).

## 2. Inkbox: admin key, phone numbers and plan shape (High)
- **Task attempted:** exercise SMS and voice for the family agents from the build session.
- **Steps taken:** used the only available agent-scoped key to read channel status; read the plan and billing docs;
  later read the org with an admin key Ken stored in 1Password.
- **Expected:** create identities and numbers by API, and see the plan's shape from the docs.
- **Actual:** admin keys can be minted only by a human in the Console; the agent-scoped key had no number
  (`sms_available:false`, `calling_available:false`). "100 identities with phone plans" reads as the Startup plan but the
  published plan has 9 VoIP numbers and 1 dedicated iMessage number. With the admin key we saw no dedicated phone
  numbers in the org; `uhlexa` had a dedicated iMessage line and its incoming-call action was `hosted_agent`. The docs
  say a dedicated line must be "voice-enabled" and "verified" or `place_call` answers 422, but the identity API does not
  show those flags.
- **Severity:** High (SMS and calls unproven for the demo path).
- **Workaround:** designed SMS and voice as feature-flagged; Ken is to provide numbers; "the first test call settles it".
- **Suggestion:** show `voice_enabled` and `verified` on the identity or line object, and document how plan limits
  (numbers vs identities) apply.
- **Source:** `2026-10-22-inkbox-facts.md` Headline 4-6, Blockers 1-4; `docs/HOSTING.md` (Lines); `TASKS.md` (Hosting).

## 3. Keiki: attaching Uhlexa's MCP tools to a managed agent (High)
- **Task attempted:** give each Keiki agent Uhlexa's `/mcp` tools with a per-agent bearer token.
- **Steps taken:** read the MCP connections docs; wrote the dashboard click path and the `deployments` API body; added a
  path alias per agent.
- **Expected:** a clear rule for sharing one endpoint across agents, and clear plan effects.
- **Actual:** the docs state "one connection per endpoint" in the per-sender section, and we could not tell whether it
  covers two agents pointing at the same URL. A plan-less org "has its tools stripped and its model forced" (stated for
  Eval Studio; live turns unconfirmed). Dashboard labels for the Global MCP page are not confirmed in the docs. The agent
  needs both `mcp` and `sandbox` features or the request is a 400, and Keiki connects at request time (502 if
  unreachable). At the time of the record the "Uhlexa" agent had no Uhlexa connection and still offered generic skills.
- **Severity:** High (the core Keiki-plus-Uhlexa loop is unproven end to end).
- **Workaround:** `/mcp` and `/mcp/katie` aliases in case sharing is refused; Ken to check Settings, Billing first.
- **Suggestion:** say on the MCP connections page whether agents may share an endpoint, and whether the plan-less
  "tools stripped" rule applies to live turns.
- **Source:** addendum `docs/superpowers/specs/2026-10-22-uhlexa-keiki-inkbox-addendum.md` §21.1;
  `docs/handoffs/2026-10-22-note-to-build-session.md` §3-4; `BLOCKERS.md` (Open, first item).

## 4. Inkbox: our own brain on a phone call (High)
- **Task attempted:** let the Keiki agent answer and place calls, with a voice that fits the product.
- **Steps taken:** read the two tiers; chose the client-WebSocket tier; sent questions to the Inkbox docs assistant and
  a second round of 14 follow-ups.
- **Expected:** a choosable voice, a documented WebSocket upgrade signature, and latency figures.
- **Actual:** hosted Voice AI cannot call custom tools, so it was not an option. On the WebSocket tier the TTS voice is
  not choosable (the catalog is documented only for Voice AI). Not documented: TTS buffering and delta-to-audio
  latency, STT endpointing, barge-in event order, when an outbound socket opens, which party `X-Call-Context.phone_number`
  names, the exact string signed on the upgrade and its replay window, and any AI-disclosure notice. Round 2 answered
  none of them.
- **Severity:** High (design settled by assumption; no real call has been placed).
- **Workaround:** speak only after the callee's first final transcript; verify HMAC over the exact `X-Call-Context`
  with a 300 s window; fall back to an unguessable per-identity secret in the path if the signature check fails; Ken
  decided to treat the different phone voice as a feature.
- **Suggestion:** add `voice` to client-WebSocket calls, publish the upgrade-signature test vector, and document the
  items above.
- **Source:** `2026-10-22-inkbox-call-voice-facts.md` (Not documented; Round 2); `DECISIONS.md`; `docs/VOICE.md`.

## 5. Keiki: external-harness docs contradict each other (High)
- **Task attempted:** build a verifier and billing plan for Keiki "external harness" dispatch.
- **Steps taken:** read the external-harness and harnesses docs, the `@onkeiki/channels` source and the vendor's
  `tunnel.sh`; self-generated test vectors.
- **Expected:** one rule for the signature headers, a test vector, and a statement on who pays for model tokens.
- **Actual:** the docs say every dispatch carries `Keiki-Signature`, and also that an agent with no signing secret
  gets only the Ed25519 header. No vendor test vector exists. Inbox mode needs a "harness API key" that is not defined
  (our org-wide `*` key returned `agents:0` from `harness/ping`). The external-harness page says nothing about billing;
  the harnesses page says model tokens served by your own provider are between you and that provider (**inference**:
  that applies to `external`). Relay mode "is not shipped yet".
- **Severity:** High (we spent a design pass, addendum v1, on external mode before Ken chose Keiki-managed agents).
- **Workaround:** self-generated vectors; then Keiki-managed agents through the gateway, which bills Keiki credits.
- **Suggestion:** one authoritative header table, a published signature test vector, and a billing line per harness mode.
- **Source:** `2026-10-22-keiki-deep-facts.md` §1.1-1.3, §7.8, Blockers; commits `5bfbc94`, `11e60bd`; `DECISIONS.md`.

## 6. Keiki: first-word latency for voice (Medium)
- **Task attempted:** talk to the Keiki agent through the gateway on a voice page.
- **Steps taken:** synthesized speech into a mic track; timed each stage; set `reasoning_effort=none`.
- **Expected:** a conversational first word (no target is documented).
- **Actual:** about 5.9 s to the first word with no tool calls; the dashboard agent's model
  (`nvidia/nemotron-3.5-super-vl`) took about 6-8 s. End to end: filler at +3.2 s, answer done at +10.0 s.
- **Severity:** Medium.
- **Workaround:** a spoken filler after 2.5 s, a 20 s cap, low reasoning effort, short voice replies; a faster model is
  still to be picked.
- **Suggestion:** show typical first-token latency per model in the model dialog; offer a "voice" preset.
- **Source:** `docs/VOICE.md` (Live check results); `BLOCKERS.md` (Open, Keiki agent tools and model).

## 7. Inkbox SDK: gaps in safe automation (Medium)
- **Task attempted:** verify webhooks and place outbound calls without double-acting.
- **Steps taken:** read `inkbox` 0.7.20 wheel source; built the adapter.
- **Expected:** `verify_webhook` enforces the documented 300 s window; `place_call` accepts an idempotency key; a call
  can be fetched by id.
- **Actual:** `verify_webhook` checks only prefix and HMAC, not the timestamp. `place_call` has no idempotency key
  (sends do). There is `list_calls` but no get-by-id. Webhooks and WebSocket upgrades are sent unsigned until an identity
  has a signing key.
- **Severity:** Medium.
- **Workaround:** our own timestamp check; callers must not blind-retry a call; `call_remote_number` scans the latest 50
  calls and returns `None` for an older id; create the signing key before the first subscription.
- **Suggestion:** enforce the tolerance in the SDK (or add a `tolerance=` parameter), add `idempotency_key` to
  `place_call`, and add `get_call(id)`.
- **Source:** `2026-10-22-inkbox-facts.md` §2; `app/providers/inkbox/service.py` (comments at `place_call` and call lookup);
  commit `7298269`.

## 8. Primitive: docs and SDK drift, no thread-id reply (Medium)
- **Task attempted:** reply to a thread and register the webhook.
- **Steps taken:** read `llms-full.txt`, the live OpenAPI file, the SDK repo's OpenAPI copy and the Python SDK source.
- **Expected:** consistent routes and a way to reply by thread id.
- **Actual:** the Python SDK's default base URL is the legacy `https://www.primitive.dev/api/v1` (health 404). `llms-full.txt`
  lists `/webhooks` but the OpenAPI file has `/endpoints`. The SDK repo's OpenAPI copy (129 paths) lacks fields the live one
  (165 paths) has, such as `latest_inbound_id`. Replying to a thread has no endpoint: look up the thread, then reply to
  the latest inbound email. The `if_latest_message_id` guard compares with the newest message overall, so using the latest
  inbound id would make every second reply a 409 (from the docs, not tested live).
- **Severity:** Medium.
- **Workaround:** plain `httpx` with an explicit base URL; trust the OpenAPI file; derive ids from `messages[]`; send a
  deterministic idempotency key.
- **Suggestion:** fix the SDK default URL, align `llms-full.txt` with the OpenAPI file, and add a reply-by-thread-id route.
- **Source:** `2026-10-22-primitive-facts.md` §2 (webhooks, SDKs), §7, §8.

## 9. Maritime: takeover expiry is lazy (Medium)
- **Task attempted:** request a human takeover and resume the agent when the person finishes.
- **Steps taken:** `request_takeover(wait=false, timeout=45)`; polled `takeover_status`; made `computer` calls meanwhile.
- **Expected:** `mode` returns to `agent` at `expires_at`, and `agent` means control is back and the step is done.
- **Actual:** nobody opened the link; `mode` was still `human` 21 s after `expires_at` and only became `agent`
  (with `last_takeover_result: "expired"`) at a poll about 48 s later. `computer` calls during that time returned
  `human_in_control`. So `mode == "agent"` does not mean the person finished.
- **Severity:** Medium.
- **Workaround:** the adapter reads `last_takeover_result` too: `expired` or `failed` means the task failed and is not
  retried; only `success` continues.
- **Suggestion:** apply expiry on time or emit an event, and document the lazy window.
- **Source:** `2026-10-22-maritime-facts.md` §8.5; `app/providers/maritime/` (`MaritimeService.takeover_state`).

## 10. Maritime: powerful tools on a scoped key, undocumented result formats (Medium)
- **Task attempted:** run browser tasks with no shell or file access.
- **Steps taken:** `tools/list` on the Computers-scoped key; read results from a live run.
- **Expected:** a Computers-only key limited to desktop actions; documented result shapes.
- **Actual:** the same key exposes `run_shell`, `read_file`, `write_file` and `android`; tools carry no annotations. Result
  text is JSON inside a text block with many null fields; our first parser matched `"error":null` as an error.
- **Severity:** Medium.
- **Workaround:** a client-side allowlist (four tools plus `close_computer`, eight `computer` actions); formats pinned
  from the live run; a regression test for the `null` case.
- **Suggestion:** let a key carry a tool allowlist, and publish the result schema (`structuredContent` or an
  `outputSchema`).
- **Source:** `2026-10-22-maritime-facts.md` §1.2, §5.4, §8.4; commit `793788b`.

## 11. SubQ Snap: confidence has no definition or cutoff guidance (Medium)
- **Task attempted:** pick a confidence cutoff for routing and screenshot checks.
- **Steps taken:** read the Decisions API docs; made 7 live calls (one a deliberate 422).
- **Expected:** a defined `confidence`, documented rate limits and timeouts.
- **Actual:** the formula is not documented. After one sample we guessed top-1 minus top-2, but four later samples
  refuted that (it is always a little above). The docs' own score example does not match either. No rate limits,
  timeouts, determinism guarantee or request id are documented; no `Retry-After` headers appeared.
- **Severity:** Medium.
- **Workaround:** treat it as a vendor score; the cutoff is a setting (`SUBQ_MIN_CONFIDENCE`, default 0.6, the docs'
  sample value); any low or malformed result falls back to the deterministic rule; Snap may only escalate approvals.
- **Suggestion:** document the confidence formula and publish rate limits.
- **Source:** `2026-10-22-subq-facts.md` (Limits; "Live run - Task 9 client"); `DECISIONS.md`.

## 12. LiveKit Inference (Deepgram): "Uhlexa" is heard as "Alexa" (Medium)
- **Task attempted:** wake the voice agent by saying "Uhlexa".
- **Steps taken:** passed the keyterm "Uhlexa" to `deepgram/nova-3`; ran a spoken end-to-end test.
- **Expected:** the keyterm biases the transcript toward "Uhlexa".
- **Actual:** the final transcript read "Alexa,"; the keyterm did not change it.
- **Severity:** Medium.
- **Workaround:** the worker's `stt_node` rewrites a leading wake phrase to "Uhlexa," and the gate accepts a list of
  variants; the recording tip is to mute any real Echo in the room.
- **Suggestion:** document how keyterms interact with common words, or expose phrase boosting for custom words.
- **Source:** `docs/VOICE.md`; spec `docs/superpowers/specs/2026-10-22-uhlexa-voice-avatar-design.md` §22.11a, §22.5.

## 13. Runway Characters: one concurrent session (Medium)
- **Task attempted:** give each of the three family agents its own talking face.
- **Steps taken:** created the "Uhlexa" Character and ran face sessions; read the account limit.
- **Expected:** enough concurrent sessions for three faces.
- **Actual:** `gwm1_avatars` allows 1 concurrent session on our account. A second hub tab gets voice plus a ring.
- **Severity:** Medium.
- **Workaround:** one face at a time (a lock in the worker), idle close after 120 s; faces for the other agents became a
  stretch goal that is blocked.
- **Suggestion:** state limits per plan on the Characters page and say how to raise them.
- **Source:** `docs/VOICE.md` (Live check results); `TASKS.md` (Voice + face, Stretch).

## 14. MCP Python SDK 2.x: renamed APIs, no obvious auth path (Low)
- **Task attempted:** connect to Maritime's MCP server and host our own.
- **Steps taken:** read the installed 2.x wheel; built a smoke test with `mcp==2.3.0`.
- **Expected:** v1 names and a one-line client with a bearer header.
- **Actual:** `FastMCP` is gone (now `MCPServer`); the client imports `httpx2`; `Client(url)` alone cannot carry an
  Authorization header; sandboxed TLS through `httpx2` failed (`OSStatus -26276`) until given a CA file.
- **Severity:** Low.
- **Workaround:** `streamable_http_client(url, http_client=httpx2.AsyncClient(headers=...))`; read the wheel for names.
- **Suggestion:** add an authenticated-client example to the 2.x migration notes.
- **Source:** `2026-10-22-maritime-facts.md` §2 item 8; spec `2026-10-22-uhlexa-design.md` §4 (MCP SDK 2.x facts).

## 15. Local tooling and dead provider keys (Low)
- **Task attempted:** read secrets, build and test, and add speech providers.
- **Steps taken:** `op item list`; ran builds with Homebrew `uv`; tried the OpenAI and Groq keys for STT/TTS.
- **Expected:** these just work.
- **Actual:** 1Password `op` hung for over 60 s inside and outside the sandbox (a macOS permission popup is the likely
  cause); Homebrew `uv` 0.8.22 panics inside the agent sandbox; the OpenAI key returned 429 "no credits remaining" and the
  Groq key 401. These were our keys and setup, not vendor defects.
- **Severity:** Low.
- **Workaround:** keys copied from another repo's gitignored env file; builds through `.venv/bin/python` (uv 0.13.4);
  STT/TTS moved to LiveKit Inference, billed to the LiveKit project.
- **Suggestion:** none for a vendor; for Anthropic/Claude Code, make sandbox-safe `uv` and TLS stores work by default.
- **Source:** `BLOCKERS.md` (Open); `DECISIONS.md` (Local tooling); `docs/VOICE.md`.

---

Not entered: Manufact Cloud was researched and dropped, not used. Its doc gaps (PORT injection, health-check target,
WebSocket and non-`/mcp` pass-through) are in `2026-10-22-keiki-neon-manufact-facts.md` §3.7; the files do not say they
caused the hosting change.

## Feature requests

1. **Keiki: read credit balance and plan state by API.** Plan, credit balance and whether tools are available on this
   plan (`/api/v1/billing` is 404 today). Why: we could not check whether a plan-less org strips tools before a demo.
   Priority: **Important**.
2. **Keiki: read another agent's conversation or a transcript with an API key.** List and get conversations, and a
   documented way for agent B to resume agent A's thread. Why: only 14-day traces are readable, so "another agent resumes
   the thread" lives in our own database. Priority: **Important**.
3. **Inkbox: close the call API gaps.** `voice` on client-WebSocket calls, `get_call(id)`, `idempotency_key` on
   `place_call`, a published upgrade-signature test vector. Why: voices should match the product and a retried call must
   not ring twice. Priority: **Important**.
4. **Inkbox SDK: enforce the webhook timestamp tolerance.** `verify_webhook` rejects stale timestamps by default
   (300 s as documented). Why: otherwise every user re-implements replay protection. Priority: **Important**.
5. **Maritime: per-key tool allowlist and tool annotations.** Limit a Computers key to named tools (no `run_shell` or
   file tools). Why: least privilege is enforced only by client code today. Priority: **Important**.
6. **SubQ Snap: document `confidence` and publish rate limits.** A definition for `confidence`, plus rate limits and
   timeouts. Why: approval routing depends on a threshold with no stated meaning. Priority: **Nice-to-have**.
