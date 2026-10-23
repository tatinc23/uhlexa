# Product Feedback: Uhlexa (Alexa+ track)

Answers to the Devpost rule "Product Feedback for each tool, API, or SDK used in your project". Facts are as of
2026-10-22 and come from the project's own research notes, decision logs and commits (cited as `file §`). Statements that
are our reading and not vendor text are marked **(inference)**. Things we did not exercise are said so.

> **Names:** every tool is named publicly by Ken's decision (2026-10-22): they are public products used under their terms. No vendor logos are shown.

## Tools at a glance

| Tool | Used for | Exercised live? | Would build again |
|---|---|---|---|
| Keiki | The LLM brain of each family member's agent (gateway); MCP attach for Uhlexa tools | Gateway turns yes; MCP attach not yet | Yes, with reservations |
| Inkbox | Real iMessage / SMS / email / calls; public tunnel | Tunnel + WebSocket yes; sends and calls not yet | Yes |
| Primitive | Email threads, Memories KV, signed webhook | Offline only (key added late) | Not yet decided |
| Maritime Computers | Browser tasks with human takeover | Yes, one full run | Yes |
| SubQ Snap | Bounded structured decisions, screenshot check | Yes, 7 calls | Yes |
| LiveKit (Agents + Inference) | Voice worker, STT/TTS | Yes | Yes |
| Runway Characters | Talking Uhlexa face | Yes | Yes |
| Neon | Hosted Postgres | Project created; runtime use not recorded | Yes (inference) |
| MCP Python SDK 2.x / spec | Maritime client; Uhlexa's own `/mcp` server (still being built in the record) | Client yes; server not yet | Yes |
| FastAPI / SQLAlchemy / Alembic | App, ORM, migrations | Yes | Yes |
| Claude Code | Built the project | Yes | Yes, with reservations |

"Would build again" answers are drafts drawn from the record. Ken confirms them before submission.

---

## 1. Keiki (onkeiki.com)

1. **What we used it for.** Each person's agent is a Keiki-managed agent that runs the model. Uhlexa calls it through
   the OpenAI-compatible gateway (`POST /api/v1/openai/chat/completions`, stable `x-conversation-id` per person and
   channel) and plans to attach its own `/mcp` server to the agent via `mcp_connections` (addendum §21).
   We also changed the agent's system prompt through `PATCH /api/v1/developer/config` (`BLOCKERS.md`, Resolved).
2. **What worked well.**
   - Once Ken turned the gateway on, a spoken end-to-end turn worked (`docs/VOICE.md`). We have one measured turn, so
     we make no reliability claim.
   - The config API did what the docs said: only `system_prompt` changed; the agent then answered "No, I'm Uhlexa".
   - Docs are published as Markdown and `llms-full.txt`, which made research fast (`keiki-deep-facts.md` Sources).
   - Traces carry per-turn model, tokens and cost (docs "Turn traces", cited in addendum §21.11).
3. **What needs work.**
   - Docs contradict themselves about `Keiki-Signature` (always present vs only when a signing secret exists).
   - Billing for external-harness mode is not stated on the external-harness page (the harness page implies model
     tokens are the host's problem; we read that as applying to `external`, **inference**).
   - The gateway-on-external-agent behaviour, notices, loops and `ask_agent` reaching a harness: not documented.
   - No API to read credit balance (`/api/v1/billing` is 404) or an account whoami; no API-key route to read
     conversations or transcripts (only 14-day traces).
   - Python: no SDK; the SDKs are TypeScript only, and `@onkeiki/agents-plugins` is named in docs but 404 on npm.
   - Latency: about 5.9 s to the first word with no tool calls (`reasoning_effort=none`); the dashboard default model took
     6-8 s (`docs/VOICE.md`, `BLOCKERS.md`).
   - Docs state a plan-less org "has its tools stripped and its model forced" (written for Eval Studio); we could not
     confirm whether it applies to live turns (addendum §21.1).
4. **Onboarding (zero to hello world).** The first probe showed the key was valid (`GET /openai/models` returned 200 with an
   empty list because no agent had the gateway on). An agent already existed from the dashboard. The first real turn
   needed Ken to switch on "API gateway" in the dashboard. We did not time the path. The first Uhlexa-owned tool call from a Keiki agent has not been recorded yet.
5. **Build again?** **Yes, with reservations.** The gateway plus managed agent saved us from running a model loop. The
   reservations are first-word latency for voice and the undocumented plan/billing edges. Not yet proven: Keiki calling
   Uhlexa's MCP tools.

## 2. Inkbox (inkbox.ai)

1. **What we used it for.** One identity per family agent for real iMessage, SMS, email and phone calls; the
   `uhlexa` identity's tunnel as the public URL (`https://uhlexa.inkboxwire.com`) for MCP, webhooks and the call
   WebSocket (`docs/HOSTING.md`). SDK: `inkbox==0.7.20` (sync only). Signed webhooks and the call media WebSocket are
   implemented in `app/providers/inkbox/`.
2. **What worked well.**
   - The tunnel: connected in about 2 s; `/health` 200 in 0.63 s; a WebSocket echo took 0.71 s and stayed open after
     20 s idle (`docs/HOSTING.md`, Verified).
   - Docs are complete enough to design from: `llms-full.txt`, a live webhook catalog, an OpenAPI file, and Markdown
     per page. The call protocol (text in, text out, `barge_in`, `clear`) is clearly specified.
   - A docs assistant answered our call-voice questions quickly (`inkbox-call-voice-facts.md`).
   - Idempotency keys exist on email, SMS and iMessage sends in the SDK.
3. **What needs work.**
   - SDK `verify_webhook` does not enforce the documented 300 s timestamp window (checked in the wheel source);
     we added our own check.
   - `place_call` takes no idempotency key in the SDK, and there is no get-call-by-id (only `list_calls`), so we scan
     the latest 50 calls (`app/providers/inkbox/service.py`).
   - The TTS voice on client-WebSocket calls cannot be chosen; the voice catalog is documented only for Voice AI.
   - Webhooks and WebSocket upgrades are sent unsigned until an identity has a signing key.
   - Docs do not give webhook retry schedule, WebSocket idle limits, call latency, or how a person dials an agent on the
     shared iMessage line.
   - Admin keys can only be minted by a human in the Console; our org had no dedicated phone numbers, so SMS and voice
     could not be exercised from the build session (`docs/HOSTING.md`).
   - The published plan reads as "100 identities" but only 9 VoIP numbers and 1 dedicated iMessage number
     (`inkbox-facts.md` Headline 5).
4. **Onboarding.** The SDK install (`inkbox==0.7.20`, locked in commit `974d17d`) and Ken's `npx skills add` install have no recorded
   problems. Reading identity and channel status with keys worked. Both demo agents have numbers now (2026-10-23); a live call into the app is not yet verified. Recipient-first shared iMessage needs each human to text `connect @handle`, which no API can do.
5. **Build again?** **Yes.** One vendor covered four real channels plus the tunnel. The gaps are small and mostly
   around calls (voice choice, call idempotency, signing-window enforcement).

## 3. Primitive (primitive.dev)

1. **What we used it for.** Email threads (read, search, reply composed from the latest inbound email), Memories KV for
   thread-state mirroring, and a signed `POST /webhooks/primitive` route. Plain `httpx` plus stdlib HMAC; we did not
   use the Python SDK (`DECISIONS.md`).
2. **What worked well.**
   - The signature scheme is simple and fully testable: all 12 published signing vectors matched our local HMAC
     (`primitive-facts.md`, commit `2a4816e`).
   - A free credential-free demo area (`/v1/demo/emails`) and unauthenticated `/v1/health`, `/v1/discovery` and MCP
     `tools/list` let us probe without an account.
   - The OpenAPI file is large and typed (165 paths); idempotency keys and cursor pagination are consistent.
3. **What needs work.**
   - The Python SDK's default base URL is the legacy `https://www.primitive.dev/api/v1`, which returns 404 on health.
   - `llms-full.txt` lists `/webhooks`; the OpenAPI file has `/endpoints`. The older OpenAPI copy in the SDK repo
     (129 paths) drifts from the live one (165).
   - There is no thread-id reply endpoint and no create-thread endpoint; replying to a thread is a composition.
   - Sends are gated by recipient; a free account can only reach addresses that already emailed it.
   - Memories are org-wide, with no per-agent or per-thread scope, and keys have no scopes.
   - Retry schedule and request timeout for webhooks are not documented.
4. **Onboarding.** Slow for an agent: account creation needs terms acceptance and an emailed code, so a human had to
   do it (`BLOCKERS.md`; key added by Ken later the same day). After that no authenticated call is recorded in our files;
   all client shapes were tested against mocks and the OpenAPI file, and a live smoke test is still open (`TASKS.md`).
5. **Build again?** **Not yet decided.** The design is sound for agent email, but we have no live evidence either way.

## 4. Maritime Computers (maritime.sh)

1. **What we used it for.** One persistent Linux desktop per demo user, over MCP at
   `https://mcp.maritime.sh/mcp/u/<user>` (pinned user, Computers-scoped key), driving a fixed screenshot, act,
   verify plan on a public TodoMVC page, with human takeover for sign-in walls.
2. **What worked well.**
   - Setup: the pinned URL worked and `get_computer` needed no `user_id`.
   - A fresh computer starts with Chromium open on `about:blank`, so no launcher step is needed.
   - One live run: 33 `computer` calls, a takeover request, and a close; both "milk" and "eggs" appeared in the list
     (`maritime-facts.md` §8).
   - Computers-only key is refused on other endpoints (403), which is good least privilege.
   - Every action returns the post-action screenshot with a monotonic `frame_id`.
   - Docs say honestly to hand login, CAPTCHA, 2FA and payment to a human.
3. **What needs work.**
   - The Computers key also exposes `run_shell`, `read_file`, `write_file` and `android`; tools have no
     `annotations`, so the "no shell" rule is enforced only by our client allowlist.
   - Takeover expiry is applied lazily, up to about a minute after `expires_at`; `mode: agent` alone does not mean the
     person finished.
   - Result formats are JSON inside a text block and are not documented; we pinned them from the live run.
   - Screenshot retention, staff visibility and egress rules are not documented.
   - A person completing a takeover and a takeover over 5 minutes were not tested.
4. **Onboarding.** Fast once a key and a paid seat plan existed. From first `get_computer` to a verified list took one
   session of roughly 12 minutes (15:00-15:12 UTC); most of it was exploration.
5. **Build again?** **Yes.** The takeover model matches what a family product needs.

## 5. SubQ Snap (subq.ai, Decisions API)

1. **What we used it for.** `POST /v1/systemone` with the `subq-snap-preview` model for bounded choices (thread
   routing, approval-required escalation) and for checking a Maritime screenshot (image input). Snap can only escalate
   to "approval required", never remove an approval (`DECISIONS.md`).
2. **What worked well.**
   - Contract: typed questions with choice options and descriptions; dict `state` accepted; clear 422 messages.
   - Speed and cost: 0.37-0.52 s per call, 166-378 input tokens, output free.
   - Image input worked as documented; the same picture was `verified` for "milk" and `not_verified` for "eggs".
   - Option keys such as `continue:<uuid>` came back verbatim.
3. **What needs work.**
   - No documented rate limits, timeouts, determinism or request id; no `Retry-After` seen.
   - `confidence` has no formula; the docs' own score example does not match top-1 minus top-2, and our four live
     samples refuted that rule too.
   - Docs give a sample cutoff of 0.6 but no guidance for choosing one; ours is a settings knob.
   - Preview status: terms allow discontinuation, so we keep a deterministic fallback.
4. **Onboarding.** The key worked on the first request (`GET /v1/models` then a real decision). Hello world took
   three calls. We did not exercise 401/402/403, 429 or timeouts live.
5. **Build again?** **Yes**, as an advisory step behind a policy engine, not as an authority.

## 6. LiveKit (Agents 1.5.8 + Inference)

1. **What we used it for.** A worker (`uhlexa-voice`) with Silero VAD and turn detection; LiveKit Inference for STT
   (`deepgram/nova-3`) and TTS (`cartesia/sonic-3`); explicit dispatch carried inside a token we mint ourselves.
2. **What worked well.**
   - Inference needed no extra provider key: when our OpenAI key had no credits and our Groq key was rejected, it was
     the one path that worked.
   - TTS first audio 0.62-0.66 s; STT final about 0.33 s after end of speech on a synthesized utterance.
   - `llm_node` / `on_user_turn_completed` overrides let us plug a Keiki agent in as the brain and skip replies cheaply
     (`StopResponse`).
   - Agent state (`lk.agent.state`) is exposed as a participant attribute, which drove our UI states.
3. **What needs work.**
   - STT hears "Uhlexa" as "Alexa,"; the `nova-3` keyterm did not change it (`VOICE.md`), so we rewrite the transcript.
   - Our own cleanup bug (agent lingered after the person left) cost time; session-close behaviour was learned by test
     (commit `ec8abe7`). Whether the docs cover it: not recorded.
   - We kept worker extras in their own venv (`requirements-voice.txt`) to avoid lock conflicts with the build session;
     that was our choice, not a LiveKit defect.
4. **Onboarding.** We reused an existing LiveKit project from another TAT repo, so we cannot grade account setup.
   The worker registered as `uhlexa-voice` and the signed dispatch worked on the first live check.
5. **Build again?** **Yes.**

## 7. Runway Characters

1. **What we used it for.** A custom "Uhlexa" Character (made from one generated image) in LiveKit mode, lip-syncing our
   TTS on the hub page via `livekit-plugins-runway` 1.5.8.
2. **What worked well.**
   - LiveKit mode lets us keep our own brain and voice: no 5-minute native session cap.
   - The face joined about 4 s and streamed video about 8.5 s after dispatch.
   - Cost was visible: 37,140 credits before, 37,118 after four tests (22 credits).
   - `max_duration` and a per-session start error give us cost guards.
3. **What needs work.**
   - `gwm1_avatars` allows 1 concurrent session on our account, which blocked faces for the other two agents.
   - Whether this limit can be raised, and what it depends on, is not documented in the files we have.
4. **Onboarding.** Image, then Character, then session: three calls, about 22 credits. The in-app browser could not test
   a real microphone, so the final audio path was tested with synthesized speech.
5. **Build again?** **Yes.**

## 8. Neon (Postgres)

1. **What we used it for.** Hosted Postgres 17 for the app (pooled host for the app, direct host for migrations); project
   `uhlexa` created through the Neon MCP.
2. **What worked well.**
   - Docs are in Markdown with exact limits (CU-hours, connections, storage), which let us size the design
     (`keiki-neon-manufact-facts.md` §2).
   - The pooled/direct split is clearly explained.
3. **What needs work.**
   - Two pages disagree on wake time ("a few hundred milliseconds" vs "a few seconds").
   - The SQLAlchemy page shows only psycopg2; the `postgresql+psycopg://` form for psycopg 3 came from SQLAlchemy's
     docs (**inference**).
   - A polling worker would keep the free compute awake about 182 CU-hours/month against 100 allowed; we designed around
     it (wake on enqueue plus a 15 min heartbeat).
   - Docs and terms now show Databricks branding on a Neon product.
4. **Onboarding.** Project creation via MCP was quick. Our tests ran on SQLite by default and on compose Postgres 17 once;
   a live Neon run is not recorded in these files.
5. **Build again?** **Yes** **(inference)**: nothing in the record counts against it.

## 9. MCP Python SDK 2.x and the MCP spec

1. **What we used it for.** A client for Maritime (live), and the design of Uhlexa's own `/mcp` server (Streamable HTTP, stateless JSON
   responses, 12 tools, an MCP Apps Family Board resource).
2. **What worked well.** In-core MCP Apps support; `ctx.headers`; an in-process test `Client`; stateless mode matched
   gateways that issue no session id. mcp 2.3.0 negotiated protocol `2025-11-25` with Maritime.
3. **What needs work.** v2 renamed things (`FastMCP` is gone, now `MCPServer`; imports `httpx2`); `Client(url)` alone
   cannot carry an Authorization header; sandboxed TLS through `httpx2` failed until a CA file was passed. We found all of
   this by reading the installed wheel.
4. **Onboarding.** Wheel source reading, then a smoke test against a live server; no hello-world tutorial was used.
5. **Build again?** **Yes.**

## 10. FastAPI, SQLAlchemy, Alembic (brief)

Used for the REST API, ORM and migrations. They worked well: the test suite reached 1085 passing tests at the last merge
(commit `974d17d`). Friction: SQLAlchemy reserves the attribute name `metadata`, so `Task.meta` maps the column; Neon's
PgBouncer needed `prepare_threshold=None` and `pool_pre_ping` (**inference**, from docs). Would build again: Yes.

## 11. Claude Code (development tool)

1. **What we used it for.** Writing the spec, plan and nearly all code, with parallel Sonnet builders in waves, an
   adversarial review, and security reviews that produced fixes (`5bcf5f5`, `afb8030`, `6420d3f`).
2. **What worked well.** Parallel builders on disjoint files; a persistent TASKS/DECISIONS/BLOCKERS trail that let a side
   session and the main session merge cleanly; live research against vendor docs; security review found real issues
   (a hidden-conversation leak, a Primitive reply to outsiders).
3. **What needs work.** Usage limits cut a task mid-build (Task 5) and paused the main session; the sandbox blocked
   test runs for a side session, and Homebrew `uv` 0.8.22 panicked inside it (we used `.venv/bin/python`); two early SubQ attempts failed
   locally with `ConnectError` before any request left the sandbox; `op` hung on a macOS permission prompt
   (`BLOCKERS.md`). An early build also followed the brief's Keiki-off flag without asking Ken, which `DECISIONS.md` logs as Claude's miss.
4. **Onboarding.** Not applicable; the project began in a worktree with specs written first.
5. **Build again?** **Yes, with reservations** about usage limits and sandbox limits.

---

## Evaluated but not used: Manufact Cloud (docs.manufact.com)

We researched Manufact as the host and then Ken dropped it for the Mac plus Inkbox tunnel (`DECISIONS.md`). The files do
not say the gaps below caused that choice. Gaps found in the docs: whether `PORT` is injected, health-check target,
header forwarding, non-`/mcp` route and WebSocket pass-through, and a Free-plan GitHub-org deploy conflict between the
docs and pricing pages (`keiki-neon-manufact-facts.md` §3.7). We did not deploy anything.
