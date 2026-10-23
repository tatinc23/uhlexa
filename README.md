# Uhlexa

Uhlexa is a family assistant built for the Alexa+ track: each family member has a personal agent, and the agents share persistent, thread-backed context, so Alex's agent can resume a "Sunday groceries" conversation that Katie's agent started. It is a real, self-hosted MCP server (Streamable HTTP) plus a simulated Alexa+ experience: an Echo speaker page, a phone app page and an "Echo Show"-style hub with a talking face. Only Alexa itself is simulated; the language model, voice, texts, email and inbound phone calls are real services (the cloud-browser provider is optional and off by default), and a person approves anything that touches the real world.

- **Demo video:** `VIDEO-LINK-HERE`
- **Devpost text:** [`docs/submission/DEVPOST_TEXT.md`](docs/submission/DEVPOST_TEXT.md) · **Video script:** [`docs/submission/VIDEO_SCRIPT.md`](docs/submission/VIDEO_SCRIPT.md)
- **Track:** Alexa+ · **License:** MIT ([LICENSE](LICENSE))


## What it does

1. **Shared context across agents.** "Ask Uhlexa to add milk and eggs to Sunday groceries" resumes the thread Katie's agent already opened, loads its saved state and continues it.
2. **Human-only approvals.** Tasks that would drive a real browser need a person's tap. Agent tokens cannot approve, and a child sees "Waiting for a grown-up".
3. **Browser tasks that stop honestly.** With the Maritime provider enabled, a cloud browser pinned to the approving user acts, verifies, or asks a person to take over at a sign-in wall. With it off, the task reads "unavailable".
4. **Voice with a wake-word setting.** Say "Uhlexa" every time, use **follow-up** (default: 8 seconds of no-wake-word conversation after each answer), or always listening.
5. **Family Board as an MCP App** at `ui://uhlexa/family-board.html`, also at `/board`.

## Architecture

```
Echo / App / Hub pages, Simulator, Family Board ──▶ FastAPI app ──▶ services ──▶ policy engine (final authority) ──▶ PostgreSQL
Each person's agent (LLM brain) ── MCP over Streamable HTTP ──▶ /mcp ───────────┘                │
Channel webhooks and call socket ──▶ app                                            providers: SubQ Snap, Primitive, Maritime
LiveKit voice worker ◀─▶ speech-to-text, agent, text-to-speech, animated face (separate process, no database access)
```

- One Python 3.12 / FastAPI codebase and one Postgres database. Postgres is authoritative for state and for every authorization decision.
- The model loop runs at the agent platform; Uhlexa supplies its tools (`/mcp`) and enforces identity, household scoping and policy.
- Each provider is behind a feature flag. A provider is active only when its `ENABLE_*` flag is on **and** its credential is set; otherwise the API and UI show it as disabled or unavailable.
- Design docs: [`docs/superpowers/specs/`](docs/superpowers/specs/) (main spec, Keiki/Inkbox addendum, voice spec).

## Setup and run

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/). Docker is optional.

### Option 1: local, no keys (fastest; SQLite, providers off)

```bash
uv sync
scripts/voice_preview.sh 8765
```

`scripts/voice_preview.sh` runs migrations on a SQLite file, seeds the Parker household, and serves the app on port 8765. Open:

- Simulator: http://127.0.0.1:8765/simulator/
- Family Board: http://127.0.0.1:8765/board
- Voice pages: http://127.0.0.1:8765/voice/echo, `/voice/app`, `/voice/hub` (offline until voice is configured)
- API docs: http://127.0.0.1:8765/docs

### Option 2: Docker Compose (Postgres 17, api, worker)

```bash
cp .env.example .env     # optional: providers stay off unless you fill it in
docker compose up --build
```

The api migrates, seeds (`SEED_DEMO_DATA=true`) and serves on http://127.0.0.1:8000 (the simulator is at `/simulator/`). Postgres is published on `127.0.0.1:5433`.

### Option 3: manual

```bash
cp .env.example .env
export DATABASE_URL=sqlite:///./uhlexa.db      # or your Postgres URL
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Environment variables

Copy `.env.example` to `.env` (git-ignored) and fill in only what you need. Everything is off by default:

| Group | Variables |
|---|---|
| Core | `APP_ENV`, `DATABASE_URL`, `APP_SESSION_SECRET` (required when `APP_ENV=production`), `ENABLE_DEMO_AUTH`, `ENABLE_APPROVALS`, `SEED_DEMO_DATA` |
| Agent platform | `ENABLE_KEIKI`, `KEIKI_GATEWAY_KEY`, `KEIKI_AGENT_IDS` (JSON map of handle to agent id) |
| Channels | `ENABLE_INKBOX`, `ENABLE_INKBOX_IMESSAGE`, `_SMS`, `_EMAIL`, `_CALLS`, `INKBOX_API_KEY`, `INKBOX_SIGNING_KEYS` |
| Providers | `ENABLE_PRIMITIVE`, `ENABLE_SUBQ_SNAP`, `ENABLE_MARITIME` and their keys |
| Voice | `ENABLE_VOICE`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `ENABLE_AVATAR`, `RUNWAYML_API_SECRET`, `RUNWAY_AVATAR_ID`, `VOICE_DEFAULT_WAKE_MODE`, `VOICE_FOLLOW_UP_S` |

Secrets go in `.env` or the git-ignored `.env.secrets.local`, never in git. JSON values in `.env.secrets.local` must be quoted, for example `KEIKI_AGENT_IDS='{"alex":"<agent id>"}'`.

### Voice worker (optional, needs voice keys)

The voice worker is a separate process with its own virtualenv (`.venv-voice`, from `requirements-voice.txt`):

```bash
scripts/voice_worker.sh download-files   # first run only
scripts/voice_worker.sh dev
```

Then open `/voice/echo`, `/voice/app` or `/voice/hub` in Chrome, pick Alex, press **Start** and allow the microphone. Details and the live-check results are in [`docs/VOICE.md`](docs/VOICE.md).

### Public URL through a tunnel (optional)

```bash
scripts/run_public.sh
```

It loads `.env` and `.env.secrets.local`, migrates the database named by `NEON_DATABASE_URL`, starts the app on 127.0.0.1:8000 and connects the Inkbox tunnel (`https://uhlexa.inkboxwire.com`). It prints an **access link**; open it once per browser. To rehearse on SQLite: `DATABASE_URL=sqlite:///./voice-preview.db scripts/run_public.sh`. See [`docs/HOSTING.md`](docs/HOSTING.md).

### Tests

```bash
.venv/bin/python -m pytest
```

Runs on in-memory SQLite and needs no network or keys (1404 passed, 8 skipped on the final tree). To run the same suite on Postgres, set `TEST_DATABASE_URL` first.

## MCP endpoint

- **URL:** `/mcp` (Streamable HTTP, stateless, JSON responses). Locally `http://127.0.0.1:8000/mcp`; public `https://uhlexa.inkboxwire.com/mcp`. (MCP Python SDK 2.3.0: latest protocol 2026-07-28, negotiates 2025-11-25 and later.)
- **Tools (12):** `get_agent_profile`, `get_family_board`, `create_family_task`, `get_task_status`, `approve_action`, `cancel_task`, `get_activity`, `handoff_task`, `continue_conversation`, `start_browser_task`, `get_browser_task_status`, `resume_after_takeover`.
- **MCP App:** `get_family_board` is bound to the resource `ui://uhlexa/family-board.html`.
- **Connect an agent:** send `Authorization: Bearer <agent token>`. The server stores only the token's SHA-256 and maps it to one agent, its owner and the owner's household role. Bad tokens get HTTP 401 with no fallback. Mint a token with `.venv/bin/python scripts/keiki_tokens.py mint alex --apply` (dry run without `--apply`; the plaintext lands in the git-ignored `.env.secrets.local`).
- **Connect a client locally (demo auth):** with `ENABLE_DEMO_AUTH=true`, any MCP client that can set headers can use `X-Uhlexa-User: alex` (or `katie`, `max`) with URL `http://127.0.0.1:8000/mcp`, then call `get_agent_profile` and `get_family_board`.
- `approve_action` and `resume_after_takeover` return `human_only` to agent tokens.

## Demo users

Fictional "Parker family": `alex` (guardian), `katie` (adult), `max` (child). Sign in from the simulator's user selector, or `POST /demo/login` with `{"handle": "alex"}`. `POST /demo/reset` reseeds (development only).

## Repo map

| Path | What |
|---|---|
| `app/main.py`, `app/lifespan.py`, `app/settings.py` | FastAPI app, startup, configuration |
| `app/api/` | REST routes: conversations, tasks, approvals, activity, browser tasks, board, voice, webhooks, demo, health |
| `app/mcp/` | MCP server, tools, bearer-token auth, Family Board resource |
| `app/services/`, `app/policy/` | Business logic and the policy engine (final authority) |
| `app/models/`, `app/db/`, `migrations/` | SQLAlchemy models, session, Alembic migrations |
| `app/providers/` | Adapters: Keiki gateway, Inkbox, Primitive, SubQ, Maritime |
| `app/voice/`, `web/voice/` | LiveKit voice worker, wake gate, token minting, Echo / app / hub pages |
| `app/hosting/` | Access gate and tunnel runner for the public URL |
| `app/jobs/` | Job dispatcher and worker (browser runs) |
| `web/simulator/`, `app/templates/` | Simulator UI and Family Board page |
| `scripts/` | `voice_preview.sh`, `voice_worker.sh`, `run_public.sh`, `entrypoint.sh` |
| `tests/` | unit, integration, contract, e2e |
| `docs/` | `HOSTING.md`, `VOICE.md`, `submission/`, specs, research notes |

## Security notes

- **Approvals are human-only.** The MCP layer refuses `approve_action` and `resume_after_takeover` for agent tokens; the policy engine decides what needs approval, and SubQ Snap can only escalate, never remove an approval. Failures fail closed.
- **No secrets in the browser.** Provider keys stay on the server and the voice worker. The browser receives one short-lived (15 minute), microphone-only voice token. The browser-task takeover link is never returned to an agent.
- **Household scoping.** Every query is scoped to the household; foreign ids return 404.
- **Public URL is gated.** Demo auth lets anyone act as any family member, so `scripts/run_public.sh` puts everything behind a one-time access link and a cookie. `/mcp`, `/webhooks/*`, `/ws/phone/*`, `/health` and `/ready` carry their own authentication.
- **Inbound only.** Uhlexa answers texts, email and calls that a person starts, from numbers and addresses bound to a household member; it does not place calls or send texts on its own. An inbound call opens with "Hi, this is <name>, an AI assistant. This call is transcribed. How can I help?". Typed `APPROVE <code>` approvals (`ENABLE_CHANNEL_APPROVALS`) are off by default, and a phone binding can require a spoken PIN.
- The demo household is fictional. Nobody under 18 uses voice, texts or calls.

## License

MIT. See [LICENSE](LICENSE).
