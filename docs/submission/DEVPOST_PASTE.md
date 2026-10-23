# Devpost submission: paste-ready text

Each block below is the final text for one form field, in form order. Internal notes are stripped.
Guidance (image gallery, file upload, video rules) stays in `DEVPOST_FORM.md`. Saved drafts can be edited until
**Fri Oct 23, 12:00 PM PT**.

## Checked on the final build (2026-10-23)

Verified here, in-process against the final tree (no browser, no live providers): resumed-thread marker, approval flow
(Max cannot see or approve), `/mcp` negotiates protocol 2025-11-25 and lists 12 tools, test suite 1404 passed / 8 skipped.
Not verified here: a fresh `git clone` + `uv sync`, any live call, text or email, the browser provider (off by default),
real-microphone latency. Cut any bullet your recording does not show.

---
## 1. About the project (required; Markdown)

```markdown
## Inspiration

A household is not one user. Alex plans the week, Katie owns the grocery list, Max is a kid who should be able to ask for things but not spend money. Today's voice assistants treat each request as a new, single-turn conversation: ask for milk and eggs and the assistant has no idea Katie already started a "Sunday groceries" list on Tuesday.

We wanted to prove one idea: **each family member has a personal agent, and conversations with people or with other agents are persistent, thread-backed context that another authorized agent can resume.** That is the Alexa+ track's "context-aware add-on that maintains state across sessions", built as a real agentic workflow instead of a Q&A bot.

## What it does

Uhlexa is a family assistant with two halves: a real MCP server that any agent can use, and a simulated Alexa+ experience (Echo speaker, phone app, and an "Echo Show"-style hub) that people use. Only Alexa itself is simulated. The language model, the voice, the text messages, the email, the phone call and the browser are real services.

- **Cross-agent persistent context.** Alex says "Ask Uhlexa to add milk and eggs to Sunday groceries." Alex's agent does not start a new conversation. It finds the thread Katie's agent already opened, loads its saved state, records the request and continues it. The thread shows who resumed it.
- **Tasks with a human in the loop.** The request becomes a task. The next step uses a cloud browser, so a policy engine requires approval. Adults and guardians approve with a tap on the app, the hub or the Family Board. **A model can never approve its own request**: the approval tools refuse agent callers. Children see "Waiting for a grown-up".
- **A browser step that stops honestly.** After approval, a cloud browser computer pinned to the approving user can open a known-safe list page, add the items and check the result. If it meets a sign-in wall, CAPTCHA or two-factor prompt it stops and asks a person to take over, and it never claims a success it did not verify. The adapter is built and contract-tested, and one live run added milk and eggs to a list and verified both (`docs/research/2026-10-22-maritime-facts.md`). It is off by default; when it is off, the task shows "unavailable" instead of a fake result.
- **Voice, with a setting for the wake word.** The simulated Echo, app and hub talk to a live voice session. Three wake modes, saved per person: say "Uhlexa" every time, **follow-up** (default: after Uhlexa answers you have 8 seconds to keep talking without the wake word), or always listening.
- **A talking face on the hub.** The "Echo Show" page shows a live animated Uhlexa face, the family's agents, current threads, tasks, pending approvals, the browser task with its screenshot, and an activity strip. Captions are always on.
- **Real channels.** The same agent answers texts (iMessage/SMS), email and phone calls that a person starts to its own line. An inbound call opens with "Hi, this is <name>, an AI assistant. This call is transcribed. How can I help?" Alex's agent can hand a plain errand to another household member's agent (`handoff_task`): the other side accepts it a moment later, and a handoff moves the request, never the permission.
- **Family Board as an MCP App.** The Family Board is a self-contained interactive view served by the MCP server (`ui://uhlexa/family-board.html`) so a compatible MCP host can render the household's threads, approvals, tasks and activity inline. The same page also runs standalone at `/board`.

## How we built it

One Python 3.12 / FastAPI codebase and one Postgres database. No microservices, no event bus.

- **Each person's agent is a hosted LLM agent; Uhlexa is its tools and its authority.** The model loop runs at the agent platform. Uhlexa exposes an MCP server (official Python SDK, Streamable HTTP, stateless, 12 tools), identifies the caller from a per-agent bearer token (only its SHA-256 is stored), and enforces everything else itself.
- **Postgres is authoritative.** Threads, tasks, approvals and activity live in Postgres. Primitive (agent email threads and Memories) is used for transcript and memory assistance only, never for authorization.
- **The policy engine has the last word.** SubQ Snap, a structured-decision model, may only *escalate* a request to "approval required"; it can never remove an approval. If it is down or returns malformed output, the system fails closed to deterministic rules.
- **Human-only tools.** `approve_action` and `resume_after_takeover` return a `human_only` error to any agent token. The takeover link is never returned to an agent. Tool descriptions tell the calling model that tool results are data, not instructions.
- **Disabled or broken providers show up as "unavailable" in the API and UI, never as a fake success.**
- **Voice runs in a separate worker** (LiveKit) that holds no database access and no user authority; everything it does to household state goes through the agent's MCP token, so the policy engine still decides.
- **Hosting:** the app runs on one Mac, with a managed Postgres, behind a tunnel that gives it a public URL. An access gate requires a one-time link, because demo sign-in would otherwise let a stranger act as a guardian.

## Challenges we ran into

- **Making "an agent resumes another agent's thread" safe.** Every read and write is scoped to the household, foreign ids return 404 so existence does not leak, and a later review found that board, task and approval reads could leak items from conversations the viewer cannot access; we filter them by conversation access.
- **Letting a model act without letting it approve.** We moved approval out of the model's reach entirely: the MCP layer rejects agent callers for approval and takeover, and the only approve paths are a signed-in human's tap or, behind a flag that is off by default, a one-time code typed back from the person's verified iMessage.
- **Voice that feels alive with a slow brain.** The agent's first word takes several seconds when a model is reasoning, so the voice worker speaks a short filler line after 2.5 seconds, caps a turn at 20 seconds, and apologizes out loud on errors. Speech-to-text hears "Uhlexa" as "Alexa", so the worker normalises a leading wake phrase.
- **Provider surprises.** Two speech keys failed the live check, so voice runs on the voice platform's own hosted speech models. The animated-face provider allows one concurrent session on our account, so only one agent gets a face.
- **Public URL safety.** Demo sign-in is fine on localhost and dangerous online, so the public build sits behind an access link, cookie-gated, with machine routes (`/mcp`, webhooks, call socket) on their own authentication.

## Accomplishments that we're proud of

- A full household model with **human-only approvals that a prompt cannot talk around**, enforced in the tool layer and the policy engine rather than in a prompt.
- One agent platform brain on every surface: the same person's agent answers the simulated Echo, the app, the hub, texts and calls, and continues the same Uhlexa thread on all of them.
- Honest degradation everywhere: every provider has a visible enabled / unavailable state, and the browser step hands over to a person instead of faking a result.
- A real MCP server and a simulated Alexa+ experience in one repo, with setup that works with no paid keys (providers off, seeded fictional household).

## What we learned

- Cross-session context is mostly an **identity and authorization** problem, not a memory problem. Once every thread has an owner, participants and an access rule, "resume" is easy.
- Keep the model on the outside of the trust boundary. Putting the LLM at a separate platform forced us to treat every tool call as untrusted input, which made the security story simpler.
- Voice latency is dominated by the model, so design the pause (filler line, state ring, captions) as carefully as the answer.
- Read provider terms before building: every integration was checked against its vendor's current terms and docs before code was written.

## What's next

- **Real homes, not a demo family.** Per-member consent, proper sign-in instead of demo auth, per-conversation credentials so a shared agent cannot lend one person's authority to another, and a revocable access list for who may message each agent.
- **Care and accessibility use cases:** an adult child coordinating groceries, appointments and reminders for a parent, with every spend or booking approved by a named person and a readable activity log.
- **More channels and devices:** the Echo Show form factor is already a web page; faces for the other agents (blocked today by a one-session limit on our animated-face account), spoken approvals, and smart-home and media connectors behind the same policy engine.
- **Alexa+ integration:** the MCP server is already client-agnostic, so the same tools and Family Board can be offered to Alexa+ itself once a toolkit is available to developers.
```

## 2. Built with (required; up to 25 tags)

```text
Python, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Neon, Docker, MCP, MCP Apps, Streamable HTTP, httpx, pytest, uv, LiveKit, Runway Characters, Keiki, Inkbox, Maritime, SubQ, Primitive, HTML, CSS, JavaScript, Claude Code
```

## 3. "Try it out" links

```text
https://github.com/tatinc23/uhlexa
```

## 5. Upload a file (optional; zip up to 35 MB)

```text
(no file; skip this field unless you assemble the optional PDF/zip)
```

## 6. Submitter type, organization, country, province

**Submitter Type:** Individual  
**Organization Name:**
```text
N/A
```
**Country of Residence:** United States  
**Canada province:**
```text
N/A
```
(Individual was chosen because you had no preference: the prize and eligibility go to you. To enter as TAT Inc instead, choose Organization and type `TAT Inc`.)

## 7. Primary track

```text
Alexa+
```

## 8. GitHub repository URL

```text
https://github.com/tatinc23/uhlexa
```

## 9. New or existing prior to Aug 31 2026

```text
New
```

## 10. AWS Builder Mini Challenge

```text
N/A — not entering the AWS Builder mini challenge; the project uses no AWS services.
```

## 11. Open Source Mini Challenge

```text
No
```
Leave its four fields (contribution URL, project repo URL, GitHub username, description) empty.

## 12. [Optional] Feature requests

```text
1. Keiki: read plan, credit balance and tool availability by API (/api/v1/billing is 404 today). Why: we could not check whether a plan-less org strips tools before a demo. Priority: important.
2. Keiki: read another agent's conversation or transcript with an API key, with a documented way for agent B to resume agent A's thread. Why: only 14-day traces are readable, so cross-agent resume lives in our own database. Priority: important.
3. Inkbox: close the call API gaps: a voice option on client-WebSocket calls, get_call(id), an idempotency key on place_call, and a published upgrade-signature test vector. Why: voices should match the product, and a retried call must not ring twice. Priority: important.
4. Inkbox SDK: make verify_webhook reject stale timestamps by default (300 s as documented). Why: otherwise every user re-implements replay protection. Priority: important.
5. Maritime: a per-key tool allowlist and tool annotations, so a Computers key cannot reach run_shell or file tools. Why: least privilege is enforced only by client code today. Priority: important.
6. SubQ Snap: document how confidence is computed and publish rate limits and timeouts. Why: approval routing depends on a threshold with no stated meaning. Priority: nice-to-have.
```

## 13. [Optional] Friction log (a URL)

```text
https://github.com/tatinc23/uhlexa/blob/main/docs/submission/FRICTION_LOG.md
```

## 14. [Optional] Project testing link

```text
(leave empty)
```

## 15. Testing instructions or credentials

```text
Judges can judge from the video and text alone. To try it, run it locally: no keys, no paid accounts, fictional household.

1. Install Python 3.12 and uv (https://docs.astral.sh/uv/), then:
   git clone https://github.com/tatinc23/uhlexa uhlexa && cd uhlexa
   uv sync
   scripts/voice_preview.sh 8765
   This migrates a local SQLite file, seeds the Parker family and serves the app.
2. Open http://127.0.0.1:8765/simulator/ and choose Alex in the user selector.
3. Press the quick prompt "Ask Uhlexa to add milk and eggs to Sunday groceries." You should see the request resume Katie's seeded "Sunday groceries" thread, a task, and an approval. Approve it as Alex. The task becomes approved and the activity log shows the whole chain; the browser step reads "unavailable" because that provider is off by default.
4. Switch to Max (the child): Max's board shows none of Alex's task or approval, and Max cannot approve anything.
5. Family Board: http://127.0.0.1:8765/board. Voice pages /voice/hub, /voice/echo, /voice/app show as offline without voice keys.
6. Reset any time with "Reset demo" (development only).

Docker alternative: docker compose up --build (http://127.0.0.1:8000, with Postgres).
MCP client: URL http://127.0.0.1:8765/mcp (Streamable HTTP), header X-Uhlexa-User: alex (local demo auth only). Call get_agent_profile, then get_family_board.
Demo users (all fictional): alex (guardian), katie (adult), max (child).
Tests: .venv/bin/python -m pytest (SQLite, no network): 1404 passed, 8 skipped.

Credentials: none needed for the local route. The full live experience (voice, face, texting, phone call, browser agent) needs the builder's provider keys and phone lines, which are not shared; it is shown in the demo video.

Live demo, kept online Nov 9-20 for judging: https://uhlexa.inkboxwire.com/access?token=<TOKEN>&next=/voice/hub.html (Ken pastes the real token here; this field is private, and the repo and public text never hold it). The link sets a 7-day cookie, then opens Uhlexa's hub with her live face. Allow the microphone and say "Uhlexa, ...". Also try /voice/echo (speaker), /voice/app (phone), /board (Family Board) and /simulator/. You sign in as the fictional Parker family (pick Alex, the guardian). Real texts and phone calls go to the builder's own lines and are not open to judges; they are shown in the video. If a page does not load, the builder's machine is restarting: try again in a minute, and the video shows everything.
```

## 16. Feedback Q1: Which developer tools, APIs, and SDKs did you use and for what?

```text
- Keiki: managed agent per family member, the LLM brain, called through its OpenAI-compatible gateway.
- Inkbox (SDK 0.7.20): iMessage, SMS, email and phone-call channels, plus the public tunnel.
- Primitive: agent email threads and a signed webhook (offline tests only; no live call yet).
- Maritime Computers: cloud browser tasks with human takeover, over MCP.
- SubQ Snap (Decisions API): bounded structured decisions and a screenshot check.
- LiveKit Agents 1.5.8 and LiveKit Inference: voice worker, speech-to-text (Deepgram nova-3) and text-to-speech (Cartesia sonic-3).
- Runway Characters: the talking Uhlexa face.
- Neon: hosted Postgres 17.
- MCP Python SDK 2.x and the MCP spec: Uhlexa's own /mcp server (Streamable HTTP, MCP Apps) and a Maritime client.
- FastAPI, SQLAlchemy, Alembic: API, ORM, migrations.
- Claude Code: wrote the spec, plan and nearly all code.
```

## 17. Feedback Q2: What worked well?

```text
- Keiki: a spoken end-to-end turn worked once the API gateway was on (one measured turn, so no reliability claim). The config API did what the docs said. Docs ship as Markdown and llms-full.txt, which made research fast.
- Inkbox: the tunnel connected in about 2 s, /health answered in 0.63 s, and a WebSocket stayed open after 20 s idle. Docs (llms-full.txt, webhook catalog, OpenAPI) were complete enough to design from; the call protocol is clearly specified.
- Primitive: all 12 published signing vectors matched our HMAC. A credential-free demo area and unauthenticated health/discovery let us probe without an account. Typed OpenAPI file.
- Maritime: pinned-user URL worked; a fresh computer starts with Chromium open; one live run (33 computer calls, a takeover request) verified both items. A Computers-only key is refused on other endpoints (403).
- SubQ Snap: typed choices, clear 422 messages, 0.37-0.52 s per call, image input worked as documented.
- LiveKit: Inference needed no extra provider key; TTS first audio 0.62-0.66 s; llm_node overrides let a Keiki agent be the brain.
- Runway Characters: LiveKit mode keeps our own brain and voice with no 5-minute cap; four tests cost 22 credits; max_duration guards cost.
- Neon: docs give exact limits and explain the pooled/direct host split.
- MCP SDK: MCP Apps support in core, ctx.headers, an in-process test client, stateless mode.
- FastAPI/SQLAlchemy/Alembic: 1085 passing tests at the last merge.
- Claude Code: parallel builders on disjoint files, a persistent task/decision trail, security review found real issues.
```

## 18. Feedback Q3: What needs work?

```text
- Keiki: docs contradict themselves on Keiki-Signature; billing for external-harness mode is not stated; no API for credit balance or for reading transcripts (14-day traces only); no Python SDK (@onkeiki/agents-plugins is named in docs but 404 on npm); about 5.9 s to first word, 6-8 s on the default model, too slow for voice.
- Inkbox: SDK verify_webhook ignores the documented 300 s window; place_call has no idempotency key and there is no get-call-by-id; the TTS voice on WebSocket calls cannot be chosen; webhooks go unsigned until a signing key exists; the plan reads as 100 identities but gives 9 VoIP numbers and 1 dedicated iMessage number.
- Primitive: SDK default base URL is legacy (404 on health); llms-full.txt and OpenAPI disagree (/webhooks vs /endpoints); no reply-by-thread-id endpoint; memories are org-wide.
- Maritime: the Computers key also exposes run_shell and file tools, with no tool annotations; takeover expiry is applied lazily (about a minute late); result formats are undocumented JSON in a text block.
- SubQ Snap: confidence has no formula; no documented rate limits or timeouts; preview status.
- LiveKit: STT hears "Uhlexa" as "Alexa"; the nova-3 keyterm did not change it.
- Runway Characters: 1 concurrent session on our account; how to raise it is not documented.
- Neon: two pages disagree on wake time.
- MCP SDK 2.x: renamed APIs (FastMCP is now MCPServer); Client(url) cannot carry an Authorization header; no auth example.
- Claude Code: usage limits cut a task mid-build; sandbox blocked test runs and panicked a package manager.
```

## 19. Feedback Q4: Onboarding experience (zero to hello world)

```text
- Keiki: the first probe showed the key was valid but the model list was empty until a person switched on "API gateway" in the dashboard. We did not time it. A Keiki agent calling Uhlexa's own tools is not yet recorded.
- Inkbox: SDK install had no recorded problems. Both demo agents have numbers now (2026-10-23); texts and the inbound-call path are built and tested, but a live call into the app is not yet verified. Shared iMessage needs each human to text "connect @handle".
- Primitive: slow for an agent. Account creation needs terms acceptance and an emailed code, so a human did it. No live authenticated call is recorded; all client code was tested against mocks and the OpenAPI file.
- Maritime: fast once a key and seat plan existed; about 12 minutes from first get_computer to a verified list.
- SubQ Snap: the key worked on the first request; hello world took three calls.
- LiveKit: we reused an existing project, so account setup is ungraded. The worker registered and signed dispatch worked on the first live check.
- Runway Characters: image, Character, session: three calls, about 22 credits. The audio path used synthesized speech, not a real microphone.
- Neon: project creation through the Neon MCP was quick; a live Neon run is not recorded.
- MCP SDK: no tutorial; we read the installed wheel, then smoke-tested against a live server.
- Claude Code: not applicable; the project began in a worktree with specs written first.
```

## 20. Feedback Q5: Would you build with these again? Yes/No and why

```text
- Keiki: Yes, with reservations. A managed agent behind a gateway saved us from running a model loop; first-word latency for voice and undocumented plan and billing edges are the reservations. Keiki calling our MCP tools is not yet proven.
- Inkbox: Yes. One vendor covered four real channels plus the tunnel; gaps are small and mostly around calls.
- Primitive: Not yet decided. The design is sound for agent email, but we have no live evidence either way.
- Maritime Computers: Yes. The takeover model matches what a family product needs.
- SubQ Snap: Yes, as an advisory step behind a policy engine, not as an authority.
- LiveKit: Yes.
- Runway Characters: Yes.
- Neon: Yes (our inference: nothing in the record counts against it).
- MCP Python SDK and spec: Yes.
- FastAPI, SQLAlchemy, Alembic: Yes.
- Claude Code: Yes, with reservations about usage limits and sandbox limits.
```

## 21. Eligibility checkboxes

Tick these yourself, only if true: age of majority · eligible jurisdiction · not an employee or agent of the sponsor, administrator or affiliates.

## 22. Which AI tools have you leveraged while working on this project? (required)

```text
Claude Code (Anthropic) was the AI coding tool: it planned the project, wrote the spec and nearly all code, tests, research notes and documentation, using parallel subagents for independent builds and reviews, including security reviews. Perplexity was used for research and delegated tasks, and Google Gemini (AI Mode) for quick research and mockups. At runtime, the Keiki-managed agent model is the LLM brain of each household agent, voice speech-to-text and text-to-speech run through LiveKit Inference, and Runway Characters provides the animated Uhlexa face.
```

## 23. Dropdowns

Pick these two dropdowns yourself: **level of learning**, **AI value for your career**.

## 24. Video URL

```text
<KEN ADDS: public YouTube or Vimeo URL>
```
