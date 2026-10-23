# Uhlexa — design spec

> **Superseded in part (2026-10-22, Ken):** Keiki is the LLM brain and Uhlexa its MCP tools + authority; Inkbox carries
> real iMessage/SMS/email/calls; Manufact is dropped (app runs on Ken's Mac behind the `uhlexa` Inkbox tunnel, Neon
> kept). Read §21 in `2026-10-22-uhlexa-keiki-inkbox-addendum.md` and §22 (voice/avatar, PR #7) — they override §2,
> §5, §15 and §20 where they differ.

Status: self-approved 2026-10-22 under Ken's overnight-autonomy contract (decisions logged in `DECISIONS.md`).
Source brief: Ken's Uhlexa build brief of 2026-10-22 (pasted into the session; its rules are restated here).
Provider ground truth: `docs/research/2026-10-22-*-facts.md` (live-checked). If this spec and a research file
disagree about a provider's wire format, the research file wins and this spec gets fixed.

## 1. What we are proving

> Each family member has a personal Alexa-like agent, and conversations with people or other agents are organized as
> persistent thread-backed context that another authorized agent can resume.

Hackathon: Amazon Developer hackathon, Alexa+ track. Participants have no Alexa+ toolkit or simulator access, so we
ship our own simulated Alexa+ web app plus a real MCP server (Streamable HTTP). Deadline: working end-to-end demo in
under 36 hours. Optimize for a working demo, not architectural perfection.

**Demo story (the primary vertical slice).** Jordan's agent started a "Sunday groceries" thread earlier in the week
(seeded). Alex opens the simulator and says *"Ask Uhlexa to add milk and eggs to Sunday groceries."* Alex's agent
decides (SubQ Snap, policy-checked) to **resume Jordan's existing thread** rather than start a new one, because Alex
is an authorized adult in the same household. It loads the thread's compact state, records the request, creates a
task, and asks for approval because the next step drives a real browser. Alex approves. A Maritime computer pinned to
Alex opens a known-safe public list page, adds milk and eggs (screenshot → act → verify), and the result (verified
screenshot or an honest takeover request) appears in the simulator and on the Family Board.

## 2. Hard rules (from the brief, binding)

- One Python 3.12 / FastAPI codebase. No microservices. No LangChain, Kafka, Redis or event bus.
- PostgreSQL is authoritative for product state and **all authorization**. Primitive is transcript/state assistance
  only, never authorization.
- SubQ Snap (`subq-snap-preview`) makes bounded structured decisions only. The **policy engine is final authority**.
  Snap unavailable or malformed ⇒ fail closed + deterministic local rules.
- Maritime: per-user MCP URL built on the server from the authenticated user (`{MARITIME_MCP_URL}/u/{user.id}` as the
  brief states, subject to research confirmation). A model never chooses the user id. `MARITIME_COMPUTERS_KEY` never
  leaves the server. No unrestricted shell in the UI. No native-mobile automation claims. Login / CAPTCHA / 2FA /
  payment ⇒ request takeover, show the link, pause until agent control returns.
- Never invent provider endpoints, schemas, tools or CLI commands. Never fake a live success. Disabled or broken
  provider ⇒ clear "unavailable" state in API and UI, never a crash.
- No cross-household reads. Adults/guardians approve sensitive actions. A model cannot approve the action it
  requested (approvals are decided by a human principal only).
- No secrets in the browser or in git. No GitHub Copilot dispatch.
- Out of scope: Alexa+ backend, Ring, Fire TV, Keiki, Inkbox (flags exist, default false, no adapters).

## 3. Architecture

```
Web simulator (/simulator/)  +  Family Board (/board, ui://uhlexa/family-board.html)
                │  REST (demo auth header/cookie)           MCP clients ─ Streamable HTTP /mcp
                ▼                                                     ▼
FastAPI app (app.main) ── api/* routers ── mcp/* (MCPServer 2.3 + Apps) ── auth/* (demo auth)
                │
          services/*  ── policy/* (final authority) ── providers/{subq,primitive,maritime}
                │                                            (feature-flagged adapters, httpx)
          db/* SQLAlchemy 2 ─────▶ PostgreSQL (authoritative)
                │
          jobs/dispatcher ──▶ jobs table ──▶ jobs/worker (compose service; embedded loop on Manufact)
```

- **api**: `uvicorn app.main:app`. **worker**: `python -m app.jobs.worker` (same image). **db**: postgres:17.
- Long-running work (Maritime browser runs, Primitive syncs) goes through the `jobs` table so requests return fast.
  Worker claims jobs with `SELECT … FOR UPDATE SKIP LOCKED` on Postgres; tests call `worker.run_once()` directly.
- Hosting: Manufact Cloud runs one container (api + `EMBEDDED_WORKER=true` background loop), DB on Neon free tier.
  Local: docker compose (db, api, worker).

### Repo layout (exact, from the brief; repo root = `uhlexa/`)

```
README.md AGENTS.md CLAUDE.md TASKS.md DECISIONS.md BLOCKERS.md .env.example .gitignore .dockerignore
Dockerfile compose.yaml pyproject.toml alembic.ini
app/__init__.py main.py settings.py lifespan.py
app/auth/{__init__,demo_auth,dependencies}.py
app/db/{__init__,base,session,migrate}.py
app/models/{__init__,user,household,agent,conversation,task,approval,activity,binding,webhook_event,job,browser_task}.py
app/schemas/{auth,conversations,tasks,approvals,activity,browser_tasks,mcp}.py
app/api/{__init__,health,demo,conversations,tasks,approvals,activity,browser_tasks}.py
app/mcp/{__init__,server,tools,resources}.py
app/policy/{__init__,engine,rules}.py
app/providers/__init__.py
app/providers/primitive/{client,models,service,webhook}.py
app/providers/subq/{client,models,service}.py
app/providers/maritime/{client,models,service}.py
app/services/{conversation_service,thread_state_service,task_service,approval_service,activity_service,browser_task_service}.py
app/jobs/{worker,dispatcher}.py
app/templates/family-board.html
web/simulator/{index.html,app.js,styles.css}
migrations/ (alembic env.py, script.py.mako, versions/)
tests/{unit,integration,contracts,e2e}/
docs/{BUILD,ARCHITECTURE,DEMO,SECURITY,SUBMISSION,COMPLIANCE}.md
```

Additions the brief's tree doesn't list but needs (logged): `app/schemas/__init__.py`, `app/services/__init__.py`,
`app/jobs/__init__.py`, `app/providers/*/__init__.py`, `app/api/providers.py` (provider status),
`app/api/webhooks.py` (Primitive webhook route), `app/seed.py`, `app/errors.py`, `tests/conftest.py`.

## 4. Stack (versions verified live 2026-10-22)

Python 3.12 (`python:3.12-slim`), uv for env/lock. fastapi ≥0.159, uvicorn[standard] ≥0.54, sqlalchemy ≥2.1.6,
alembic ≥1.20, pydantic ≥2.14, pydantic-settings ≥2.16, psycopg[binary] ≥3.3, httpx ≥0.28, **mcp ≥2.3,<3**.
Dev: pytest ≥9.1, ruff. Async tests via the anyio pytest plugin.

MCP SDK 2.x facts (read from the installed wheel): `from mcp.server.mcpserver import MCPServer, Context`
(FastMCP is gone); MCP Apps in-core: `from mcp.server.apps import Apps`, `@apps.tool(resource_uri="ui://…")`,
`apps.add_html_resource(uri, html)` (served as `text/html;profile=mcp-app`), `MCPServer(…, extensions=[apps])`;
`ctx.headers` gives request headers; `server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
json_response=True, host="0.0.0.0")` + `async with server.session_manager.run()` in the lifespan; mount it at `/`
after all other routes so `/mcp` has no trailing-slash redirect; in-process test client `from mcp import Client`.
MCP Apps view handshake (ext-apps spec 2026-01-26): `ui/initialize {appInfo, appCapabilities, protocolVersion:
"2026-01-26"}` → `ui/notifications/initialized` → host sends `ui/notifications/tool-result` (a `CallToolResult`;
read `structuredContent`); view sends `ui/notifications/size-changed`.

## 5. Configuration (`app/settings.py`, pydantic-settings; `.env.example` = the brief's contract verbatim)

```
APP_ENV APP_HOST APP_PORT DATABASE_URL APP_SESSION_SECRET SECRET_ENCRYPTION_KEY
ENABLE_DEMO_AUTH ENABLE_APPROVALS
ENABLE_PRIMITIVE PRIMITIVE_API_KEY PRIMITIVE_API_BASE_URL
ENABLE_SUBQ_SNAP SUBQ_API_KEY SUBQ_DECISIONS_BASE_URL SUBQ_SNAP_MODEL
ENABLE_MARITIME MARITIME_COMPUTERS_KEY MARITIME_MCP_URL
ENABLE_KEIKI ENABLE_INKBOX ENABLE_RING ENABLE_FIRETV   (always false; no adapters)
```

Extra (documented in `.env.example` under "Uhlexa extras"): `PRIMITIVE_WEBHOOK_SECRET`, `EMBEDDED_WORKER` (false),
`SEED_DEMO_DATA` (true in development), `DEMO_DEFAULT_USER` (`alex`), `MARITIME_SAFE_TARGET_URL` (the one
known-safe page), `PUBLIC_BASE_URL` (for webhook registration / takeover return links).

A provider is **active** only when its `ENABLE_*` flag is true **and** its credential is present. Otherwise its status
is `disabled` (flag off) or `misconfigured` (flag on, credential missing). Live errors set `unavailable` with a
reason. `GET /api/providers` returns `{primitive|subq|maritime: {status, enabled, configured, detail}}` with no
secrets; the simulator renders these as badges.

## 6. Data model (14 tables; Postgres authoritative; portable types so tests run on SQLite)

Common: `id` UUID PK, `created_at`, `updated_at` (UTC, tz-aware). Enums stored as strings (`native_enum=False`).

| table | key columns |
|---|---|
| users | handle (unique), display_name, email (nullable, unique), is_demo |
| households | name, slug (unique), timezone, settings JSON (`adult_spend_approval_threshold_cents` 5000) |
| household_memberships | household_id, user_id, role `guardian|adult|child`; unique(household_id, user_id) |
| agents | household_id, owner_user_id, name, persona, is_default, status `active|paused`, config JSON |
| conversations | household_id, title, topic_key (normalized e.g. `sunday-groceries`), visibility `household|private`, status `open|archived`, created_by_user_id, created_by_agent_id, transcript_provider `primitive|local`, primitive_thread_id (nullable, unique), thread_state JSON (authoritative copy of the compact state), thread_state_version int, last_message_at |
| conversation_participants | conversation_id, participant_type `user|agent`, user_id?, agent_id?, joined_at, joined_via `created|resumed|invited`; check exactly one of user_id/agent_id |
| messages | conversation_id, sender_type `user|agent|system`, sender_user_id?, sender_agent_id?, body, task_id?, primitive_message_id (nullable, unique), source `local|primitive` |
| tasks | household_id, conversation_id?, created_by_user_id, requested_by_agent_id?, title, description, kind `general|reminder|chore|shopping_list|purchase|booking|browser`, status (machine below), amount_cents?, metadata JSON, completed_at, cancelled_at |
| approvals | household_id, task_id, requested_by_agent_id?, requested_for_user_id, decided_by_user_id?, status `pending|approved|denied|cancelled`, rule_id, reason, decision_source `policy|policy+snap`, decision_note, decided_at |
| activity_events | household_id, event_type, actor_type `user|agent|system|provider`, actor_user_id?, actor_agent_id?, task_id?, conversation_id?, browser_task_id?, summary, payload JSON; index (household_id, created_at) |
| provider_bindings | household_id, user_id?, agent_id?, provider `primitive|subq|maritime`, binding_type (e.g. `maritime_computer`, `primitive_inbox`), external_id, status `active|inactive|error`, config JSON (non-secret), last_used_at; unique(provider, binding_type, user_id) |
| webhook_events | provider, external_event_id (unique per provider), event_type, signature_valid bool, payload JSON, status `received|processed|ignored|failed`, error, processed_at |
| jobs | kind, payload JSON, status `queued|running|succeeded|failed|cancelled`, attempts, max_attempts (3), run_after, locked_at, locked_by, last_error, household_id? |
| browser_tasks | the brief's fields: household_id, user_id, agent_id, conversation_id?, maritime_computer_id?, task_type, requested_url, status, last_screenshot_ref?, last_error?, requires_takeover bool, takeover_url?; plus task_id, steps JSON (step log: action, ok, note, screenshot_ref), result JSON, verified bool |

`last_screenshot_ref` points at our own `GET /api/browser-tasks/{id}/screenshot` route; the bytes live in
`browser_tasks.result` / a small `screenshots` JSON blob only if the Maritime research shows screenshots come back
inline (decided at build time from the research file; logged).

### Task status machine

```
create ─policy: approval─▶ pending_approval ─approve─▶ approved ─(browser kind)─▶ running ─▶ completed | failed
   │                                         └─deny──▶ denied              └─(other kinds)─▶ completed
   └────no approval──────▶ approved
running ─▶ needs_takeover ─(resume_after_takeover, agent control back)─▶ running
any non-terminal ─▶ cancelled        terminal: completed | failed | denied | cancelled
```

Browser task statuses: `queued → starting → running → (needs_takeover ↔ running) → succeeded | failed | cancelled`,
plus `unavailable` when Maritime is disabled/misconfigured (honest, never "succeeded").

## 7. Seed (development; `SEED_DEMO_DATA=true`; idempotent; `/demo/reset` reseeds)

Household "The Parker Family" (`parker`): **alex** (Alex Parker, guardian), **jordan** (Jordan Parker, adult),
**max** (Max Parker, child). One default agent each ("Alex's Uhlexa", …). Seeded conversation "Sunday groceries"
(`topic_key=sunday-groceries`, visibility household) created by Jordan + Jordan's agent with two local messages
("We need bread and bananas for Sunday" / agent ack) and thread_state
`{summary: "Sunday grocery list for the Parker family.", items: ["bread","bananas"], open_questions: [],
last_actor: "Jordan's Uhlexa"}`. When Primitive is active, seeding also creates/links the real Primitive thread if the
research shows that's possible; otherwise the seeded thread stays `transcript_provider=local` (labeled).

## 8. Auth (demo only, `ENABLE_DEMO_AUTH=true`)

`POST /demo/login {handle}` sets an HMAC-signed cookie (`APP_SESSION_SECRET`, stdlib `hmac`) and the simulator also
sends `X-Uhlexa-User`; either resolves a `Principal(user, membership, household, agent)`. MCP requests resolve from the
`X-Uhlexa-User` header, else `DEMO_DEFAULT_USER`. Demo auth off ⇒ 401 everywhere except health and `/demo/users`.
Every service call takes the Principal and scopes queries by `principal.household.id` (no cross-household reads; a
foreign id returns 404, never 403, to avoid leaking existence). Mark with `# DEMO-AUTH: replace before real users`.

## 9. Policy (`app/policy/`) — pure functions, final authority

`rules.py` holds ordered rules; `engine.py` evaluates and merges model input.

- `evaluate_task(requester_role, kind, amount_cents, will_use_browser, settings) -> PolicyDecision(requires_approval,
  rule_id, reason, approver_roles)`:
  1. `browser_action_needs_human_ok` — any task that will drive a Maritime browser needs a human approval (the
     requester's own approval counts for adults/guardians: a human, not the model, approves).
  2. `child_spend` — child + (purchase or amount > 0) → guardian/adult approval (not the child).
  3. `child_external_action` — child + booking/browser → guardian/adult approval.
  4. `adult_spend_threshold` — amount ≥ threshold → approval by a *different* adult/guardian.
  5. else no approval.
- `merge_model_advice(decision, snap_result)`: Snap may only **escalate** (no-approval → approval with rule
  `model_escalation`); it can never remove an approval. Snap failure ⇒ keep the deterministic decision (fail closed).
- `can_decide(approval, principal)`: approval pending; principal is a human user (MCP calls resolve to a human
  principal; the agent itself has no approval identity); role allowed by the rule; for `adult_spend_threshold` the
  decider ≠ requester; children never decide.
- `can_access_conversation(conversation, principal)`: same household AND (visibility household OR participant);
  children cannot resume adult-created threads unless they are participants. `can_resume` = access + principal's own
  agent becomes a participant (`joined_via=resumed`).
- `ENABLE_APPROVALS=false` ⇒ every decision `requires_approval=False`, reason "Approvals disabled by flag" (still
  logged as activity), except rule 1 stays on for browser actions (fail closed: we never drive a browser unapproved).

## 10. SubQ Snap adapter (`app/providers/subq/`)

`SnapService.decide(question_id: str, state: dict, choices: dict[str, str]) -> DecisionResult`

`DecisionResult(question_id, choice: str | None, confidence: float | None, probabilities: dict, source:
"snap"|"fallback", status: "ok"|"disabled"|"unavailable"|"malformed", detail: str)`.

Wire: one `choice` question named `question_id` in a `POST {base}/systemone` request (exact shape per
`docs/research/2026-10-22-subq-facts.md`). Malformed = non-2xx, non-JSON, missing answer, choice not in `choices`,
probabilities out of range. Any of those ⇒ `status=malformed|unavailable`, `choice=None`; callers then use the
deterministic fallback for that question. Timeouts 8 s. Never log the key or the full state.

Questions Uhlexa asks (each has a deterministic fallback in `services`):

| question_id | choices | fallback |
|---|---|---|
| `thread_routing` | `continue:<conversation_id>` per candidate thread (≤5 authorized, recent) + `new_thread` | best topic-keyword overlap ≥ 0.5 → continue, else new |
| `approval_escalation` | `require_approval`, `no_extra_approval` | `no_extra_approval` (policy rules already decided) |
| `context_sufficiency` | `recent_context_enough`, `need_full_transcript` | `recent_context_enough` when thread_state has items |
| `browser_automation` | `use_maritime_browser`, `no_browser_needed` | keyword rule: list/add/order/book on a web page → browser |
| `verify_screenshot` (Maritime loop, image state if supported) | `verified`, `not_verified`, `needs_human` | `not_verified` (honest) |

Snap's answer is advice; policy and authorization checks always run after it.

## 11. Primitive adapter (`app/providers/primitive/`)

Capabilities behind `ENABLE_PRIMITIVE`: `get_thread`, `get_message`, `reply_to_thread`, `search_messages`,
`get_memory`, `put_memory`, `delete_memory`, plus `webhook.verify_signature(headers, raw_body)`. Real endpoints,
auth and signing scheme come only from `docs/research/2026-10-22-primitive-facts.md`; any capability without a
documented endpoint raises `ProviderUnavailable("not documented")`.

`thread_state_service` keeps the compact thread state: Postgres `conversations.thread_state` is authoritative; when
Primitive is active it is mirrored to Primitive memory (`put_memory`) and read back on resume (`get_memory`, falling
back to Postgres). Conversation transcript: when Primitive is active, replies post to the Primitive thread and are
mirrored into `messages` (`source=primitive`); when not, `messages` is the transcript (`source=local`) and the UI shows
"Local transcript — Primitive not connected". Primitive never decides who can read or resume a thread.

Webhook: `POST /webhooks/primitive` → verify signature (reject 401 on failure, store `signature_valid=false` row) →
upsert `webhook_events` by external id (idempotent) → enqueue `primitive_sync` job.

**Credential status 2026-10-22: no Primitive API key exists** (1Password holds only a signup link). Ship the adapter,
contract tests and the honest `misconfigured` state; BLOCKERS.md tells Ken how to add the key.

## 12. Maritime adapter (`app/providers/maritime/`)

- `pinned_mcp_url(user) -> str` = `f"{settings.MARITIME_MCP_URL}/u/{user.id}"` (or the documented equivalent),
  computed only from the authenticated Principal. No tool, route or model output can pass a user id in.
- Client speaks MCP to Maritime over Streamable HTTP using the SDK client (`from mcp import Client` with an httpx
  transport carrying the documented auth header). Tools used: `get_computer`, `computer`, `request_takeover`,
  `takeover_status` — exact names/args from `docs/research/2026-10-22-maritime-facts.md`.
- `provider_bindings(provider=maritime, binding_type=maritime_computer, user_id)` stores and reuses the computer id.
- Runner (`browser_task_service.run(browser_task_id)`, executed by the worker): get/reuse computer → screenshot → act
  → screenshot → verify per step of a fixed plan for the task type. Task type for the demo: `list_add_items` against
  `MARITIME_SAFE_TARGET_URL` (one known-safe public page, no login, no payment; chosen from research). Items come from
  the approved task's metadata (validated list of short strings), never from free model text.
- Takeover: if a step's screenshot shows login / CAPTCHA / 2FA / payment (Snap `verify_screenshot` = `needs_human`, or
  Maritime itself reports it) ⇒ `request_takeover`, store `takeover_url`, status `needs_takeover`, activity event,
  stop the job. `resume_after_takeover` (API/MCP) polls `takeover_status`; only when control is back with the agent
  does it re-enqueue the run from the last unfinished step.
- Disabled/misconfigured ⇒ browser task status `unavailable` with reason, task stays `approved` (not `completed`).
- Only the fixed plan's actions are allowed (navigate, click/type/key as the plan defines). No shell actions are ever
  sent, and no UI or MCP surface accepts arbitrary actions.

## 13. Services (sync SQLAlchemy sessions; services `flush`, the request/job boundary commits)

- `activity_service.record(...)`, `list(household_id, limit, task_id=None, conversation_id=None)`.
- `conversation_service`: `route_message(principal, text) -> RouteResult(conversation, decision)` (candidates =
  authorized household conversations with open status, ≤5 newest; Snap `thread_routing` + fallback; policy
  `can_resume`), `start(principal, title, topic_key)`, `resume(principal, conversation)` (adds own agent as participant,
  `conversation.resumed` activity), `post_message(...)` (local or Primitive per §11), `list_for(principal)`,
  `get(principal, id)`.
- `thread_state_service`: `load(conversation)`, `apply_request(conversation, items/notes) -> new state` (version++),
  `save(conversation, state)` (Postgres + Primitive memory mirror).
- `task_service`: `create_from_message(...)`, `create(principal, …, source)`, `get/list`, `transition`, `cancel`.
- `approval_service`: `request`, `decide(principal, approval_id, approve, note)` (approve ⇒ task `approved`, browser kinds
  ⇒ enqueue `browser_task_run` via dispatcher), `list`.
- `browser_task_service`: `create_for_task`, `start(principal, task_id)` (only for approved tasks; enqueues job),
  `run(browser_task_id)` (worker), `resume_after_takeover(principal, id)`, `get_status`.
- Intent parsing (in `task_service`, deterministic): extracts list items from "add X and Y to <list>" (comma/and
  split, ≤10 items, each ≤40 chars), list name → topic_key, amounts, kind keywords. Documented as the Day-1
  deterministic parser; no LLM free text drives actions.

Orchestration for `POST /api/conversations/messages` (and MCP `continue_conversation`):
route → resume/start → post user message → (Snap `context_sufficiency`) load state → parse intent → task create
(policy + Snap escalation) → thread state update → agent reply message → activity → response.

## 14. HTTP API

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness, no DB |
| GET | `/ready` | DB `SELECT 1` + migrations at head ⇒ 200, else 503 |
| GET | `/demo/users` | no auth; seeded users with role + agent |
| POST | `/demo/login` | `{handle}` ⇒ signed cookie |
| POST | `/demo/reset` | development only (404 otherwise); wipe + reseed |
| GET | `/api/me` | principal, household, agent |
| GET | `/api/providers` | provider statuses (§5) |
| GET | `/api/conversations` | authorized conversations with participants + transcript provider |
| GET | `/api/conversations/{id}` | messages + thread_state |
| POST | `/api/conversations/messages` | `{text, conversation_id?}` ⇒ routed result `{conversation, routing, user_message, agent_message, task, approval}` |
| GET/POST | `/api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/cancel` | |
| GET/POST | `/api/approvals`, `/api/approvals/{id}/approve`, `/api/approvals/{id}/deny` | |
| GET | `/api/activity?limit&task_id&conversation_id` | |
| POST | `/api/browser-tasks` | `{task_id}` start (approved tasks only) |
| GET | `/api/browser-tasks/{id}` | status, steps, takeover_url, verified |
| GET | `/api/browser-tasks/{id}/screenshot` | last screenshot (auth + household scoped) |
| POST | `/api/browser-tasks/{id}/resume` | resume after takeover |
| GET | `/api/board` | Family Board payload (single builder shared with MCP) |
| POST | `/webhooks/primitive` | signature-verified |
| GET | `/` → `/simulator/`; `/simulator/*` static from `web/simulator`; `/board` Family Board page | |
| * | `/mcp` | MCP Streamable HTTP (stateless, JSON responses) |

Errors: `{"detail": "...", "code": "..."}`; 401 auth, 404 not found/foreign, 403 policy, 409 bad transition,
422 validation, 503 provider unavailable (with provider + reason).

## 15. MCP server (`app/mcp/`)

`server.py` builds `MCPServer("uhlexa")` + `Apps`; `tools.py` registers the 11 tools; `resources.py` registers
`ui://uhlexa/family-board.html` (from `app/templates/family-board.html`). Each tool opens its own DB session, resolves
the Principal (§8), calls services, commits, returns a JSON dict (structuredContent). `ServiceError` ⇒ tool error.

`get_agent_profile`, `get_family_board` (Apps-bound to the board resource), `create_family_task(title, kind, items?,
description?, amount_usd?)`, `get_task_status(task_id)`, `approve_action(approval_id, decision, note?)`,
`cancel_task(task_id, reason?)`, `get_activity(limit?, task_id?, conversation_id?)`,
`continue_conversation(text, conversation_id?)` (same orchestration as the REST route; returns routing + thread state),
`start_browser_task(task_id)`, `get_browser_task_status(browser_task_id)`, `resume_after_takeover(browser_task_id)`.
Tool descriptions tell the calling LLM: only humans approve; never pass user ids; Maritime runs only for approved tasks.

Family Board (`app/templates/family-board.html`): self-contained (inline CSS/JS, no external requests); MCP Apps
handshake per §4; standalone fallback at `/board` polls `/api/board`. Shows members + agents, threads (with "resumed
by" participants), pending approvals, tasks by status, browser task card (status, verified, takeover link), activity,
provider badges.

## 16. Web simulator (`web/simulator/`)

Plain HTML/CSS/JS, no build, no external requests. Compact Alexa+-style hub: household/user selector, provider badges
(disabled/unavailable states visible), conversation pane (thread list + transcript, "resumed from Jordan's Uhlexa"
marker, transcript-provider label), task list, approvals list with Approve/Deny (only for allowed deciders; children
see "Waiting for a grown-up"), activity timeline, browser task status card (status, steps, screenshot thumbnail,
verified badge), **Run browser task** button (approved tasks), takeover banner with link + "I'm done, resume" button,
send button + Enter, quick prompt chips including the demo sentence, Reset demo (development). Polls every 2 s.
Escapes all text. Works at 375 px and on a projector.

## 17. Testing

Default DB for tests: SQLite in-memory (portable schema); `TEST_DATABASE_URL` runs the same suite on Postgres
(run once against compose Postgres before shipping). Provider HTTP is faked with `httpx.MockTransport` / in-process MCP
fakes in tests only; product code never fakes success.

Required (from the brief) + ours:
- unit: approval-required policy (all rules, escalation-only merge, can_decide), SubQ fallback when unavailable,
  conversation creation/routing logic (new vs continue, authorization), intent parsing.
- integration: create task → activity event; approval changes status; MCP family board payload (in-process Client +
  `_meta.ui.resourceUri` + resource mime); all 11 MCP tools; provider status endpoint with flags on/off; no
  cross-household reads.
- contracts: Primitive webhook signature verification (documented scheme + test vector if published); SubQ malformed
  output fails closed (bad JSON, unknown choice, 4xx/5xx, timeout); Maritime pinned URL server-side and stable (same
  user ⇒ same URL; no input path can change it); request shapes match the research files.
- e2e: simulator task → approval → browser task → status update (HTTP flow as the simulator does it, worker
  `run_once`, Maritime faked at the transport boundary) — plus a manual live run recorded for the demo.
- migrations: upgrade head on a fresh DB; autogenerate diff empty.

## 18. Delivery order (brief's order; report after each phase in TASKS.md + commit)

1. Foundation (FastAPI, settings, DB, Docker, compose) 2. Migrations + models 3. Demo auth + seed 4. Services +
policy 5. MCP + tools + Family Board 6. Simulator 7. Primitive 8. SubQ Snap 9. Maritime 10. Full vertical slice
11. Tests (written alongside; this phase fills gaps) 12. Docs + demo checklist. Then: Neon + Manufact deploy, live
provider smoke tests, demo capture, draft PR.

## 19. Risks

Maritime run cost/latency and takeover UX; Primitive key missing (fallback is local, labeled); Snap image-input
format for screenshot verification may not be documented (fallback: honest `not_verified`); Manufact cold starts
(warm `/health` before demos); Neon free-tier limits; MCP Apps rendering differs per host (standalone `/board`
fallback).

## 20. Research amendments (2026-10-22, override earlier sections where they differ)

Sources: `docs/research/2026-10-22-{primitive,maritime,subq,keiki-neon-manufact}-facts.md` (live-checked).

**Primitive is email infrastructure for agents** (managed `*.primitive.email` inboxes; a thread is an email
conversation; "Memories" is an org-scoped JSON KV store). Mapping of the brief's capabilities:
`get_thread` = `GET /v1/threads/{id}` (metadata + message list, no bodies); `get_message` = `GET /v1/emails/{id}`
(inbound) or `GET /v1/sent-emails/{id}` (outbound, by the thread view's `direction`); `reply_to_thread` = composed:
`get_thread` → `latest_inbound_id` → `POST /v1/emails/{id}/reply` with `if_latest_message_id` and an
`Idempotency-Key` derived from our `messages.id` (no thread-id reply endpoint exists); `search_messages` =
`GET /v1/emails/search`; `get_memory`/`put_memory`/`delete_memory` = `GET|PUT|DELETE /v1/memories` (key ≤512 B,
value ≤64 KiB, `if_version` CAS ⇒ 409). Auth `Authorization: Bearer prim_…`; envelope `{success,data,meta}`.
Webhook: header `Primitive-Signature: t=<unix>,v1=<hex>`, HMAC-SHA256 hex over `"<t>.<raw body>"`, secret =
`GET /v1/account/webhook-secret` used as UTF-8 (not base64), tolerance 300 s past / 60 s future, multiple `v1=`
allowed; event name in `X-Webhook-Event`; dedupe on body `id`; register via `POST /v1/endpoints`. Use plain httpx +
stdlib hmac (not the Alpha SDK). Contract tests embed the 12 official signing vectors. Seeded threads stay
`transcript_provider=local` (creating a Primitive thread needs real, recipient-gated email). No key exists ⇒
`misconfigured`; never promise sends to arbitrary recipients. Brand the sender "Uhlexa", never Alexa/Amazon.

**Maritime Computers** (confirmed): MCP `https://mcp.maritime.sh/mcp`, Streamable HTTP, stateless; auth
`Authorization: Bearer mk_…` = `MARITIME_COMPUTERS_KEY` (Computers-only scope; the broader `MARITIME_API_KEY_ALT`
is never used by the app). Pinned URL `f"{MARITIME_MCP_URL}/u/{quote(str(user.id), safe='')}"`; omit `user_id` from
`get_computer`. The server exposes 10 tools; the client **allowlists** `get_computer, computer, request_takeover,
takeover_status, close_computer` and the `computer` actions `screenshot, left_click, double_click, type, key, wait,
scroll, zoom`; `run_shell/read_file/write_file/android/computer_batch` are never called. Screenshots come back as MCP
image blocks (JPEG, 1200×750 frame) plus a text block (frame_id, width, height); coordinates are in the last
screenshot's frame. Linux has no open-URL action: the plan focuses/launches the browser from screenshot evidence,
then `ctrl+l` → type URL → Return → wait → screenshot. `get_computer` timeout ≥90 s (cold create up to 60 s).
`request_takeover(wait=false)` returns a viewer/control link (treat as a secret: show only to the owning user, never
log, clear when done); `takeover_status` reports `human|agent` + last result; while a human holds the desktop actions
return 409 `human_in_control`. Errors `computer_limit|concurrency|plan_lapsed|no_capacity|computers_disabled` ⇒
browser task `unavailable`. Call `close_computer` at job end. Account is on an active flat-rate seat plan (no
per-use charge). Python: mcp 2.3 connects with `streamable_http_client(url, http_client=httpx2.AsyncClient(headers=…))`
(SDK 2.x uses httpx2) — builder confirms against the installed package; bridge async into the sync worker.
Safe target: `MARITIME_SAFE_TARGET_URL=https://todomvc.com/examples/react/dist/` (public, MIT, no login/payment);
its state persists in the computer's browser profile, so verify "milk" and "eggs" are visible rather than counting.
Verification is vision-only: SubQ Snap `verify_screenshot` with the screenshot as image input (format per the SubQ
facts file); if image input is unavailable the result is `not_verified` (honest). Takeover demo: a second task type
`sign_in_check` against our own public `/demo/sign-in-wall` page (a real password form that we never fill) ⇒ the
runner requests takeover and waits; this shows the takeover path visibly without touching any third-party login.

**SubQ Snap** (confirmed live): `POST https://api.subq.ai/v1/systemone`, Bearer key, body `{model, state,
questions:{<question_id>:{type:"choice", instructions, criteria:{option: description}}}}`; answer
`{type:"choice", choice, probabilities, confidence}`. `decide()` keeps an `INSTRUCTIONS[question_id]` registry.
Fail-closed validator (from the facts file): 200, no `detail`, answer keys == {question_id}, type choice, choice ∈
options, probabilities keys == options, each in [0,1], sum ≈ 1, choice is argmax, confidence in [0,1] and ≥
`SUBQ_MIN_CONFIDENCE` (0.6). Retry 429/5xx/timeout once with backoff; never retry 400/422; 401/402/403 ⇒
`unavailable` (config). Timeout 10 s. Send minimal synthetic state only (no ages, no real data). Optional
record/replay cache keyed by (question_id, hash(state, choices)) for demo stability. UI shows an "AI-assisted"
label wherever a Snap decision is displayed. Public materials do not use SubQ's or Primitive's name/logo without
written consent (ToS) — see BLOCKERS.md.

**Neon + SQLAlchemy:** app uses the pooled `-pooler` host with `postgresql+psycopg://…?sslmode=require&
channel_binding=require`, engine `pool_pre_ping=True, pool_recycle=240, pool_size=5, connect_args={"prepare_threshold":
None, "connect_timeout": 15}` for Postgres URLs; Alembic uses `MIGRATIONS_DATABASE_URL` (direct host) when set,
else `DATABASE_URL`. **Worker idling:** the worker sleeps `WORKER_IDLE_SECONDS` (default 1 locally); the embedded
worker (Manufact) is woken in-process by `dispatcher.enqueue` (a `threading.Event`) and otherwise heartbeats every
`WORKER_IDLE_SECONDS=900` so Neon can scale to zero.

**Manufact:** listen on `PORT` if set, else `APP_PORT` (8000); exec-form CMD; `/health` DB-free. Non-/mcp routes and
custom-header forwarding are undocumented ⇒ smoke-test on first deploy; MCP identity falls back to the cookie and
`DEMO_DEFAULT_USER`. Free plan is scale-to-zero: warm `/health` before demos.

**Keiki:** onkeiki.com (agent channels runtime); stays off (`ENABLE_KEIKI=false`, no adapter).
