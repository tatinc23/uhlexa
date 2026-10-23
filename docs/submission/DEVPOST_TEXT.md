<!-- Paste each section into the matching Devpost field. -->
<!-- Source of truth: docs/superpowers/specs/2026-10-22-uhlexa-design.md (sections 1, 3, 15, 16), the Keiki/Inkbox addendum (section 21), the voice spec (section 22), docs/HOSTING.md, docs/VOICE.md, TASKS.md, BLOCKERS.md. -->

# Project name

**Uhlexa**

# Tagline (one line)

A family of personal Alexa+ agents that share one persistent memory, hand work to each other, and ask a human before they touch the real world.

# Track

Alexa+ (a self-hosted MCP server over Streamable HTTP, plus a simulated Alexa+ experience, both allowed by the track rules).

# Inspiration

A household is not one user. Alex plans the week, Katie owns the grocery list, Max is a kid who should be able to ask for things but not spend money. Today's voice assistants treat each request as a new, single-turn conversation: ask for milk and eggs and the assistant has no idea Katie already started a "Sunday groceries" list on Tuesday.

We wanted to prove one idea: **each family member has a personal agent, and conversations with people or with other agents are persistent, thread-backed context that another authorized agent can resume.** That is the Alexa+ track's "context-aware add-on that maintains state across sessions", built as a real agentic workflow instead of a Q&A bot.

# What it does

Uhlexa is a family assistant with two halves: a real MCP server that any agent can use, and a simulated Alexa+ experience (Echo speaker, phone app, and an "Echo Show"-style hub) that people use. Only Alexa itself is simulated. The language model, the voice, the text messages, the email, the phone call and the browser are real services.

- **Cross-agent persistent context.** Alex says "Ask Uhlexa to add milk and eggs to Sunday groceries." Alex's agent does not start a new conversation. It finds the thread Katie's agent already opened, loads its saved state, records the request and continues it. The thread shows who resumed it.
- **Tasks with a human in the loop.** The request becomes a task. The next step uses a cloud browser, so a policy engine requires approval. Adults and guardians approve with a tap on the app, the hub or the Family Board. **A model can never approve its own request**: the approval tools refuse agent callers. Children see "Waiting for a grown-up".
- **A browser step that stops honestly.** After approval, a cloud browser computer pinned to the approving user can open a known-safe list page, add the items and check the result. If it meets a sign-in wall, CAPTCHA or two-factor prompt it stops and asks a person to take over, and it never claims a success it did not verify. The adapter is built and contract-tested, and one live run added milk and eggs to a list and verified both (`docs/research/2026-10-22-maritime-facts.md`). It is off by default; when it is off, the task shows "unavailable" instead of a fake result.
- **Voice, with a setting for the wake word.** The simulated Echo, app and hub talk to a live voice session. Three wake modes, saved per person: say "Uhlexa" every time, **follow-up** (default: after Uhlexa answers you have 8 seconds to keep talking without the wake word), or always listening.
- **A talking face on the hub.** The "Echo Show" page shows a live animated Uhlexa face, the family's agents, current threads, tasks, pending approvals, the browser task with its screenshot, and an activity strip. Captions are always on.
- **Real channels.** The same agent answers texts (iMessage/SMS), email and phone calls that a person starts to its own line. An inbound call opens with "Hi, this is <name>, an AI assistant. This call is transcribed. How can I help?" Alex's agent can hand a plain errand to another household member's agent (`handoff_task`): the other side accepts it a moment later, and a handoff moves the request, never the permission.
- **Family Board as an MCP App.** The Family Board is a self-contained interactive view served by the MCP server (`ui://uhlexa/family-board.html`) so a compatible MCP host can render the household's threads, approvals, tasks and activity inline. The same page also runs standalone at `/board`.

# How we built it

One Python 3.12 / FastAPI codebase and one Postgres database. No microservices, no event bus.

```
 Echo / App / Hub pages ──┐                         Phone, iMessage, SMS, email
 Simulator, Family Board ─┤                                   │
                          ▼                                   ▼
                 FastAPI app (REST + pages)  ◀──── channel provider webhooks / call socket
                          │  \
                          │   \── /mcp  (MCP server, Streamable HTTP)  ◀── each person's agent (LLM brain)
                          ▼
        services ── policy engine (final authority) ── providers (feature-flagged)
                          │                               SubQ Snap · Primitive email threads · Maritime browser
                          ▼
                  PostgreSQL (all state and all authorization)
                          ▲
                  job worker (browser runs)           LiveKit voice worker ──▶ speech-to-text → agent → text-to-speech → face
```

Design choices that matter:

- **Each person's agent is a hosted LLM agent; Uhlexa is its tools and its authority.** The model loop runs at the agent platform. Uhlexa exposes an MCP server, identifies the caller from a per-agent bearer token, and enforces everything else itself.
- **Postgres is authoritative.** Threads, tasks, approvals and activity live in Postgres. Primitive (agent email threads and Memories) is used for transcript and memory assistance only, never for authorization.
- **The policy engine has the last word.** SubQ Snap, a structured-decision model, may only *escalate* a request to "approval required"; it can never remove an approval. If it is down or returns malformed output, the system fails closed to deterministic rules.
- **Disabled or broken providers show up as "unavailable" in the API and UI, never as a fake success.**
- **Voice runs in a separate worker** (LiveKit) that holds no database access and no user authority; everything it does to household state goes through the agent's MCP token, so the policy engine still decides.
- **Hosting:** the app runs on one Mac, with a managed Postgres, behind a tunnel that gives it a public URL. An access gate requires a one-time link, because demo sign-in would otherwise let a stranger act as a guardian.

# MCP server details

- **Protocol:** MCP, implemented with the official Python SDK (`mcp` 2.3 or later), **Streamable HTTP**, stateless, JSON responses.
- **Endpoint:** `POST /mcp` (and an optional per-agent alias `/mcp/{alias}`), self-hosted. Public URL during the demo: `https://uhlexa.inkboxwire.com/mcp`.
- **Tools (12):** `get_agent_profile`, `get_family_board`, `create_family_task`, `get_task_status`, `approve_action`, `cancel_task`, `get_activity`, `handoff_task`, `continue_conversation`, `start_browser_task`, `get_browser_task_status`, `resume_after_takeover`.
- **MCP App:** `get_family_board` is bound to the resource `ui://uhlexa/family-board.html` (MCP Apps, served as `text/html;profile=mcp-app`). The view is self-contained with no external requests.
- **Auth model:** a Keiki-style agent sends `Authorization: Bearer <token>`. The server hashes the token (SHA-256) and looks it up; the binding names the agent, the agent names its owner, and the owner's household role becomes the caller's identity. Unknown, revoked, paused-agent or child-owned tokens get HTTP 401 with no fallback. Tokens are accepted only in the header, never in a query string or tool argument. No tool takes a user id.
- **Human-only tools:** `approve_action` and `resume_after_takeover` return a `human_only` error to any agent token. The takeover link is never returned to an agent.
- **Annotations and descriptions** tell the calling model that approvals are human-only, that tool results are data and not instructions, and that browser runs need an approved task.
- **Demo path:** with demo sign-in on (local only), a browser or MCP client can act as a seeded user with the `X-Uhlexa-User` header or the sign-in cookie.

# Challenges we ran into

- **Making "an agent resumes another agent's thread" safe.** Resuming across people is the point, and also the risk. Every read and write is scoped to the household, foreign ids return 404 so existence does not leak, and a later review found that board, task and approval reads could leak items from conversations the viewer cannot access; we filter them by conversation access.
- **Letting a model act without letting it approve.** We moved approval out of the model's reach entirely: the MCP layer rejects agent callers for approval and takeover, and the only approve paths are a signed-in human's tap, or, behind a flag that is off by default, a one-time code that a signed-in adult asks for and types back as `APPROVE <code>` from their verified iMessage.
- **Voice that feels alive with a slow brain.** The agent's first word takes several seconds when a model is reasoning, so the voice worker speaks a short filler line after 2.5 seconds, caps a turn at 20 seconds, and apologizes out loud on errors. Speech-to-text hears "Uhlexa" as "Alexa", so the worker normalises a leading wake phrase.
- **Provider surprises.** Two speech keys failed the live check, so voice runs on the voice platform's own hosted speech models. The animated-face provider allows one concurrent session on our account, so only one agent gets a face.
- **Public URL safety.** Demo sign-in is fine on localhost and dangerous online, so the public build sits behind an access link, cookie-gated, with machine routes (`/mcp`, webhooks, call socket) on their own authentication.

# Accomplishments that we're proud of

- A full household model with **human-only approvals that a prompt cannot talk around**, enforced in the tool layer and the policy engine rather than in a prompt.
- One agent platform brain on every surface: the same person's agent answers the simulated Echo, the app, the hub, texts and calls, and continues the same Uhlexa thread on all of them.
- Honest degradation everywhere: every provider has a visible enabled / unavailable state, and the browser step hands over to a person instead of faking a result.
- A real MCP server and a simulated Alexa+ experience in one repo, with setup that works with no paid keys (providers off, seeded fictional household).

# What we learned

- Cross-session context is mostly an **identity and authorization** problem, not a memory problem. Once every thread has an owner, participants and an access rule, "resume" is easy.
- Keep the model on the outside of the trust boundary. Putting the LLM at a separate platform forced us to treat every tool call as untrusted input, which made the security story simpler.
- Voice latency is dominated by the model, so design the pause (filler line, state ring, captions) as carefully as the answer.
- Read provider terms before building: every integration was checked against its vendor's current terms and docs before code was written.

# What's next

- **Real homes, not a demo family.** Per-member consent, proper sign-in instead of demo auth, per-conversation credentials so a shared agent cannot lend one person's authority to another, and a revocable access list for who may message each agent.
- **Care and accessibility use cases:** an adult child coordinating groceries, appointments and reminders for a parent, with every spend or booking approved by a named person and a readable activity log.
- **More channels and devices:** the Echo Show form factor is already a web page; faces for the other agents (blocked today by a one-session limit on our animated-face account), spoken approvals, and smart-home and media connectors behind the same policy engine.
- **Alexa+ integration:** the MCP server is already client-agnostic, so the same tools and Family Board can be offered to Alexa+ itself once a toolkit is available to developers.

# Built with

Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL (Neon for the hosted database), Docker Compose, the official MCP Python SDK (Streamable HTTP, MCP Apps), httpx, pytest, uv; LiveKit (voice sessions, hosted speech-to-text and text-to-speech); Runway Characters (the animated face); Keiki (agent platform and LLM gateway); Inkbox (iMessage, SMS, email, phone calls, public tunnel); Maritime (cloud browser computer); SubQ Snap (bounded structured decisions); Primitive (agent email threads and Memories); plain HTML, CSS and JavaScript for the simulator, voice pages and Family Board.

# Mini challenges

None claimed. Open Source would need an additional open-source contribution beyond this repo, and AWS Builder needs an AWS service in the project; Uhlexa uses neither.

# Testing instructions for judges

**Live demo, online Nov 9-20 for judging.** It runs on the builder's Mac at `https://uhlexa.inkboxwire.com`, behind an access link that goes only in Devpost's private testing field (`https://uhlexa.inkboxwire.com/access?token=<TOKEN>&next=/voice/hub.html`; it sets a 7-day cookie). It opens Uhlexa's hub with her live face; allow the microphone and say "Uhlexa, ...". Real texts and phone calls go to the builder's own lines and are not open to judges.

**Fastest route, no keys: run it locally.** Providers are off by default, the household is fictional, and no paid account is needed.

1. Install Python 3.12 and [uv](https://docs.astral.sh/uv/). Then:
   ```bash
   git clone https://github.com/tatinc23/uhlexa uhlexa && cd uhlexa
   uv sync
   scripts/voice_preview.sh 8765
   ```
   That migrates a local SQLite file, seeds the Parker family, and serves the app.
2. Open `http://127.0.0.1:8765/simulator/`.
3. Choose **Alex** in the user selector. Press the quick prompt **"Ask Uhlexa to add milk and eggs to Sunday groceries."** You should see the request resume Katie's seeded "Sunday groceries" thread, a task, and an approval. Approve it as Alex. The task becomes approved and the activity log shows the whole chain; the browser step reads "unavailable" because that provider is off by default.
4. Switch to **Max** (the child): Max's board shows none of Alex's task or approval, and Max cannot approve anything.
5. Open `http://127.0.0.1:8765/board` for the Family Board, and `/voice/hub`, `/voice/echo`, `/voice/app` for the voice pages (they show as offline without voice keys).
6. Reset anytime with **Reset demo** (development only).

**Docker alternative:** `docker compose up --build` serves on `http://127.0.0.1:8000` with Postgres.

**Connect an MCP client:** URL `http://127.0.0.1:8765/mcp` (Streamable HTTP), header `X-Uhlexa-User: alex` (local demo auth only). Call `get_agent_profile`, then `get_family_board`. See the README for details.

**Demo users:** `alex` (guardian), `katie` (adult), `max` (child). All fictional.

**Credentials judges need:** none for the local route. On the live link, judges can use the voice pages (hub with face, speaker, phone app), the Family Board and the simulator as the fictional Parker family. Texting and phone calls use the builder's real lines, so they are shown in the video only.

**Tests:** `.venv/bin/python -m pytest` (SQLite, no network): 1404 passed, 8 skipped.
