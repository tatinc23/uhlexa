# Keiki, Neon, Manufact Cloud: ground-truth facts for Uhlexa

Checked 2026-10-22. Every statement below comes from a page fetched or a probe run on 2026-10-22. Raw pages were saved
under `$TMPDIR/keiki`, `$TMPDIR/neon`, `$TMPDIR/manufact`, `$TMPDIR/mcpuse` (not in the repo). "Not documented" means the
docs I read do not say; it is not a claim that the feature is absent.

## Sources

Keiki
- https://onkeiki.com/llms.txt, https://onkeiki.com/llms-full.txt (every doc in one file)
- https://onkeiki.com/developers, https://onkeiki.com/openapi.json
- https://onkeiki.com/docs/external-harness.md, https://onkeiki.com/docs/harnesses.md, https://onkeiki.com/pricing.md
- https://onkeiki.com/terms (last updated Aug 18, 2026), https://onkeiki.com/privacy (updated Aug 18, 2026)
- https://onkeiki.com/.well-known/mcp.json

Neon
- https://neon.com/docs/introduction/plans.md, https://neon.com/docs/introduction/scale-to-zero.md,
  https://neon.com/docs/guides/scale-to-zero-guide.md
- https://neon.com/docs/connect/connection-pooling.md, https://neon.com/docs/connect/connect-from-any-app.md,
  https://neon.com/docs/connect/connection-errors.md, https://neon.com/docs/guides/sqlalchemy.md
- https://neon.com/platform-terms (Product Specific Schedule, updated Oct 14, 2026), https://www.databricks.com/legal/mcsa
- https://docs.sqlalchemy.org/en/21/dialects/postgresql.html, https://www.postgresql.org/docs/current/libpq-connect.html

Manufact Cloud (mcp-use)
- https://docs.manufact.com/llms.txt (index), plus `.md` versions of /dashboard/{deployments,build-runtimes,settings,
  environment-variables,domains,billing,servers,quickstart,connect-existing-server,observability,sessions,api-keys},
  /cli/deploy
- https://manufact.com/pricing.md, https://manufact.com/terms (updated Sep 28, 2026), https://manufact.com/privacy
  (updated Sep 2, 2026), https://manufact.com/developers.md
- https://docs.mcp-use.com/v2/typescript/server/deployment/mcp-use.md, .../self-hosted.md,
  https://docs.mcp-use.com/python/server/{running,fastapi_mount,inspector}.md

---

# 1. Keiki

## 1.1 What it is (and the name trap)

- The product with a public API, docs and an MCP server is **Keiki at `onkeiki.com`**, "a product of Zero Email, Inc.,
  a Delaware corporation" (terms, footer). `keiki.com` (domain-for-sale page), `keiki.app` (a kids learning app) and
  `keiki.dev` are different things and mention nothing about onkeiki. `keiki.ai` redirects to keikicenter.org.
- Self-description (llms.txt): "Keiki runs autonomous agents on the channels customers already message you on: SMS,
  iMessage, WhatsApp, Telegram, Slack and email, where they answer questions, call your systems, take payment, and
  escalate to a person when policy requires it." An agent is a system prompt, a model, custom tools and feature toggles;
  the platform owns channel ingress/delivery, memory, steering, browser automation, sandboxed code execution, billing and
  observability.
- It is not described anywhere as an "orchestration and communication router". The older blueprint's wording is not from
  the vendor. The closest real thing to its "external turn harness" is the documented **External harness** (below).
- SDK `@onkeiki/agents` (`AgentKit`): cloud-served (`kit.sync()`) or self-hosted (`kit.serve()`).
- Docs are also published as markdown at `/docs/<name>.md`.

## 1.2 API facts

- Base URL: `https://onkeiki.com` (OpenAPI `servers[0]`). REST is versioned in the path, `/api/v1`.
- Auth: bearer API key. `Authorization: Bearer $KEIKI_API_KEY`. Anthropic-compatible routes use `x-api-key` instead.
  Keys are created in the dashboard (https://onkeiki.com/dashboard), can be bound to one agent, and carry scopes
  (`agents:chat` is the one the public routes check). Docs: "Never place a secret key in browser code or a public
  repository."
- The MCP endpoint `https://onkeiki.com/mcp` (Streamable HTTP) uses **OAuth** (server card: `authentication.type =
  oauth2`), not the API key. Unauthenticated `GET /mcp` returns 401 `{"error":"invalid_token","error_description":"A
  bearer token for this deployment is required."}`.
- Public OpenAPI paths (complete list in `openapi.json`, title "Keiki Agent API" 1.0.0, license "Proprietary"):
  - `GET  /api/v1/openai/models` (listKeikiAgentModels): "Returns the agents that the authenticated API key can call and
    whose API gateway is enabled." 200 `{"object":"list","data":[AgentModel]}`; 401 key missing/invalid; 403 key lacks
    `agents:chat`. **This is the only read-only account-level call with an API key.**
  - `POST /api/v1/openai/chat/completions` (starts or continues a persisted, billed conversation; header
    `x-conversation-id` 1-64 chars `[A-Za-z0-9_-]`; 409 if a turn is already in progress)
  - `POST /api/anthropic/v1/messages`, `POST /api/anthropic/v1/messages/count_tokens`
  - `POST /api/v1/developer/deployments`, `GET|DELETE /api/v1/developer/deployments/{external_ref}`, MCP sender
    credentials under `.../mcp-senders`, `POST /api/v1/developer/agents/{agentId}/notice`,
    `GET /api/v1/developer/trace-exporters` (+ attach/detach)
  - No whoami endpoint is documented.
- Agent must have its "API gateway" enabled to be callable; the agent name or ID is the `model` value.
- Dashboard "playground" is the documented test environment; no sandbox phone number or free channel is documented.

## 1.3 Webhook signing (the older blueprint was half right)

Keiki has **two** signing schemes, depending on the webhook:

1. Identity webhook and action-lifecycle webhooks (docs/identity, docs/actions in llms-full.txt):
   ```
   X-Keiki-Timestamp: 1789600000
   X-Keiki-Signature: sha256=<hex HMAC-SHA256(secret, "<unix_timestamp>.<raw_request_body>")>
   ```
   The digest covers `<timestamp>.<raw body>`, not the body alone. "Reject requests outside your replay window before
   parsing the body" (identity doc: reject anything older than five minutes), then compare in constant time. Webhook
   URLs must be public `https://`. The secret is a `whsec_...` value you supply.
2. External-harness turn dispatch (docs/external-harness.md): headers have no `X-` prefix.
   ```
   Keiki-Signature: t=<unix-seconds>,v1=<hex HMAC-SHA256(secret, "<unix-seconds>.<raw-body>")>
   Keiki-Signature-Ed25519: t=<unix-seconds>,kid=<key id>,v1=<base64url Ed25519 signature over "<unix-seconds>.<turn-token>.<raw-body>">
   ```
   plus `Keiki-Turn-Id` and `Keiki-Turn-Token` (short-lived bearer for that turn's routes). Ed25519 public keys:
   `https://onkeiki.com/.well-known/keiki-turn-keys.json` (JWKS, `OKP`/`Ed25519`). Signing secret from
   `POST /api/v1/developer/agents/:agentId/harness/signing-secret` (shown once).

External harness in one paragraph: agent `harness: "external"`, `harness_config: {mode: "push"|"relay"|"inbox",
dispatchUrl, timeoutMs (max 10 min)}`. Keiki keeps channel ingress, steering, delivery and the trace; for each turn it
sends a signed `keiki.turn` JSON (version 1) to your app, which answers 2xx (docs: respond `202`) and later calls
`POST {turnUrl}/messages|stream|typing|lease|complete` and `GET {turnUrl}/arrivals`. Push turns are synchronous: if the
endpoint is down the turn fails. Inbox mode (long-poll `GET /api/v1/harness/inbox/next?wait=25`) needs no public URL.

## 1.4 Pricing and availability (pricing.md)

- "Plan preview. Contact us for current pricing and availability." "Can I purchase a proposed plan? Not yet. These plans
  are staged in billing and open at launch."
- Proposed: Starter $1/mo for 3 months then $20/mo (includes $20 credits); Pro $100/mo; Max $500/mo; Enterprise custom.
  Phone lines billed separately: $300/mo each (Starter), $150 (Pro), $99 (Max). No free tier is documented.

## 1.5 Live probe

Command shape (key redacted; run read-only):
```
set -a; . ./.env.secrets.local; set +a
curl -sS -m 20 -H "Authorization: Bearer $KEIKI_API_KEY" https://onkeiki.com/api/v1/openai/models
```
Result 2026-10-22 10:35 UTC: `HTTP/2 200`, `content-type: application/json`, body
`{"object":"list","data":[]}`. Served via CloudFront, `access-control-allow-origin: *`, no rate-limit headers.
Meaning: the key is valid and holds the `agents:chat` scope (a bad key is 401, a key without the scope is 403 per
OpenAPI), and **no agent with the API gateway enabled exists** on that key. I did not call chat completions (stateful,
billed) or MCP (needs OAuth).

## 1.6 Terms (https://onkeiki.com/terms, updated Aug 18, 2026)

- 05 Acceptable use, you may not: "send unsolicited or unlawful messages, or otherwise violate the rules of a messaging
  provider or carrier, including consent and opt-out requirements"; "impersonate a person or organization, or hide that a
  message came from an automated agent where disclosure is required"; "deploy an agent to give medical, legal, or
  financial advice in a way that requires a licensed professional"; "probe, scrape, overload, or reverse engineer the
  Service".
- 06: "you are responsible for what your agents say and do, including any messages they send and any actions they take in
  connected systems"; "do not use the Service where an incorrect answer or unsupervised action would create serious risk
  to health, safety, or legal rights"; you own "notices, consents, and opt-out handling ... including telecom, email, and
  privacy requirements".
- 08: "We do not use Your Content to train foundation models."
- 11: beta/early-access features are "as-is, may change or disappear".
- 14 Indemnity covers "what your agents say or do".
- 15: they "may suspend or terminate access if you breach these terms, if your use creates risk or legal exposure".
- Privacy: "The Service is not intended for children, and we do not knowingly collect personal information from anyone under
  16." Conversation data (including the contact's phone number/email/handle) is processed with you as controller.
- Reading: usable for a clearly fictional demo, but a *family/child* agent that texts real people is outside the spirit of
  "not intended for children" and puts consent/opt-out duties on us. The terms say nothing about demos or test use.

## 1.7 Design implications

- Recommendation: **leave Keiki off** (`ENABLE_KEIKI=false`, no adapter). It matches the brief and DECISIONS.md.
- Reasons grounded above: (1) its value is real-world messaging channels, and Uhlexa's demo surface is a web simulator +
  Family Board + MCP; (2) channels that matter need paid phone lines and no plan can be purchased yet; (3) children's
  data and consent/opt-out duties; (4) the demo already has Primitive for messaging.
- If ever revisited: build a Keiki *external-harness* app (`/keiki/turn` push endpoint verifying `Keiki-Signature` or the
  Ed25519 header over the raw body) or inbox-mode poller; do not model it on a body-only HMAC.
- Do not store the key in Manufact secrets or the repo while the flag is off.

## 1.8 Blockers

- None for the demo (feature stays off). For a future Keiki channel: no purchasable plan, no documented free channel,
  agent gateway not enabled on the existing key.

## 1.9 Unknowns

- Whether a free dashboard account can attach a channel without a paid line (not documented).
- Rate limits for the REST API (none documented).
- Exact `AgentModel` schema was not inspected (empty list in probe).

---

# 2. Neon (now documented as "Lakebase Postgres, by Databricks")

Docs and terms now carry Databricks branding ("Built around Lakebase Postgres, by Databricks"); the product name Neon,
the `neon.com` docs and the `*.neon.tech` hostnames are still what the docs use.

## 2.1 Free plan (plans.md, "Plan overview" and FAQ)

| Item | Free |
| --- | --- |
| Price | $0/month |
| Projects | 100 |
| Branches | 10/project (extra branches not available) |
| Compute | 100 CU-hours/project/month ("enough to run a 0.25 CU compute in a project for 400 hours/month") |
| Autoscaling | up to 2 CU (8 GB RAM) |
| Scale to zero | after 5 min inactivity; "cannot disable" |
| Storage | 1 GB/project, 20 GB account total |
| Public network transfer | 5 GB/project/month |
| Monitoring | 1 day |
| History window (instant restore) | 6 hours, up to 1 GB-month |
| Snapshots | 1 manual |
| Support | Community |

- Exceeding limits (FAQ): "when you run out of CU-hours or public network transfer, your compute is suspended until the
  next billing period or until you upgrade. Exceeding the 1 GB per-project storage cap ... causes operations that
  increase storage (inserts, updates, and deletes) to fail ... None of these limits delete your data."
- Compute max connections (connection-pooling.md): 0.25 CU = 104 `max_connections` (7 reserved, so 97 usable); 0.5 CU = 209;
  1 CU = 419.
- Suspended computes "do not accrue CU-hours". Cold wake: scale-to-zero page says "reactivates automatically within a few
  hundred milliseconds"; connection-errors page says "Activation typically happens within a few seconds". Two docs, two
  figures; plan for seconds.
- Neon's inactivity test is "no active queries for 5 minutes" (connection-errors page). A background loop that queries
  the DB (for example a jobs-table poller) keeps the compute active and burns CU-hours.
- Launch plan (for reference only): $0.106/CU-hour, $0.35/GB-month.

## 2.2 Connection strings

- Console default (connect-from-any-app.md), pooled:
  `postgresql://alex:AbC123dEf@ep-cool-darkness-a1b2c3d4-pooler.us-east-2.aws.neon.tech/dbname?sslmode=require&channel_binding=require`
- Direct host = same string without `-pooler` in the hostname. Port 5432 for both. Pooler is PgBouncer in
  `pool_mode=transaction`, up to 10,000 client connections, `default_pool_size = 0.9 x max_connections`.
- "Neon requires that all connections use SSL/TLS encryption."
- Pooled connections do **not** support `SET`/`RESET`, `LISTEN`/`NOTIFY`, SQL-level `PREPARE`/`DEALLOCATE`, temp tables,
  session-level advisory locks. Protocol-level prepared statements are supported (PgBouncer >= 1.22,
  `max_prepared_statements=1000`). Use the direct string for migrations, `pg_dump`, and anything session-dependent.
- SQLAlchemy + psycopg 3: Neon's SQLAlchemy page only shows psycopg2 and a `postgresql://` URL. For psycopg 3 the
  SQLAlchemy docs (en/21 dialect page) give the prefix `postgresql+psycopg://` (sync engine by default, async engine with
  `create_async_engine`). So the Uhlexa form is:
  `postgresql+psycopg://USER:PASS@ep-xxxx-pooler.<region>.aws.neon.tech/DBNAME?sslmode=require&channel_binding=require`
  Tell the user to paste Neon's string and rewrite `postgresql://` to `postgresql+psycopg://` in settings (my inference
  from the two docs; not tested live because no Neon resource was created).
- libpq `channel_binding` (postgresql.org): `require` means the connection must use channel binding; "only supported over
  SSL connections with PostgreSQL 11 or later servers using the SCRAM authentication method". psycopg 3.3.6 is current on
  PyPI (Python >= 3.10). The `psycopg[binary]` wheel's bundled libpq version was not checked; if
  `channel_binding=require` fails on a driver build, fall back to `sslmode=require` (inference, flagged).
- SQLAlchemy guidance on Neon (sqlalchemy.md): use SQLAlchemy >= 2.0.33; to avoid `SSL SYSCALL error: EOF detected` after a
  suspend, set `pool_pre_ping=True` or `pool_recycle` <= the scale-to-zero setting (300 s on Free).

## 2.3 Terms

- Governing text: Product Specific Schedule (Neon), https://neon.com/platform-terms, updated Oct 14, 2026, entered with
  "Databricks, Inc., the parent company of Neon, LLC", subject to the Databricks Master Cloud Services Agreement
  (https://www.databricks.com/legal/mcsa). Quote: "Databricks may make changes to the Agreement, this Schedule, and
  pricing from time to time, and Customer's continued use of the Platform Services will constitute consent to such
  changes."
- Schedule: "Customer ... shall not include in Customer Content any cardholder data as defined under PCI-DSS."
- MCSA use restrictions: customer will not "violate the Acceptable Use Policy or use the Databricks Services in violation of
  applicable laws"; "use the Platform Services other than in accordance with the Documentation"; "include in Customer
  Content ... any data for which Customer does not have all rights, power and authority necessary for its collection, use
  and processing"; "sell, resell, ... or otherwise provide access to the Platform Services to any third party".
- MCSA suspension: Databricks may suspend "immediately without notice if Databricks reasonably suspects that Customer has
  violated its obligations ... in a manner that may cause material harm".
- Reading: a demo with a fictional household and no card data is inside the terms. Real family or child data would need
  the "all rights" condition satisfied; keep seed data fictional. The AUP page (databricks.com/legal/aup) renders its text
  client-side, so I could not read its body; treat the AUP as unread.

## 2.4 Design implications

- Two URLs: `DATABASE_URL` (pooled, for the app) and a direct URL (for Alembic migrations, and for LISTEN/NOTIFY if ever
  used). Spec section 6 already keeps portable types, so no session features are needed.
- Engine settings: `pool_pre_ping=True`, `pool_recycle=240` (under 300 s), small pool, `connect_timeout=15` in the URL or
  `connect_args` so the first request after a suspend retries instead of failing. Optionally `prepare_threshold=None` on
  the pooled engine (conservative; Neon says protocol-level prepared statements work, but psycopg 3 auto-prepares after
  repeated statements; I did not test this).
- **`EMBEDDED_WORKER` poll loop:** a loop that selects from `jobs` every few seconds keeps the compute active 24/7. At 0.25 CU
  that is about 182 CU-hours/month, over the 100 CU-hour Free allowance (about 16.7 days), after which compute is
  suspended until the next billing period. Poll only while work is pending (in-process wake-up on enqueue plus a slow
  heartbeat), or a short demo window is fine (7 days at 0.25 CU = about 42 CU-hours).
- `/health` must not touch the DB (already in the spec) so Manufact health checks do not wake Neon.
- 1 GB storage is ample for the seed. 5 GB egress is ample.

## 2.5 Blockers

- None. Creating the project needs Ken's Neon account/connection string; nothing was created here.

## 2.6 Unknowns

- Whether the default Free compute minimum is 0.25 CU (docs only say "enough to run a 0.25 CU compute for 400 hours").
- Databricks AUP body; Neon's behavior with idle open connections (docs speak only of "active queries").
- Whether `psycopg[binary]`'s bundled libpq honors `channel_binding=require` (very likely, untested).

---

# 3. Manufact Cloud (docs.manufact.com, runs mcp-use)

## 3.1 Hosting model

- Manufact Cloud hosts MCP servers: "A server is the central object". Deploy from a GitHub repo through the Manufact
  GitHub App, or with `mcp-use deploy --no-github` (uploads local source to "a platform-managed private repository";
  compressed archive limit 80 MB; archive excludes `.git`, dependencies, build output, `.env*`, caches, symlinks and
  paths in `.gitignore`).
- Runtime detection (build-runtimes.md): Node from `package.json` + lockfile; **Python from `pyproject.toml` /
  `requirements.txt` with uv, poetry, pipenv or pip**; "Anything else: a committed `Dockerfile`". "If you commit a
  `Dockerfile` to your repo, Manufact uses it as-is." Default for Node/Python: runs the `build` script (if present) and the
  `start` script; both overridable per server ("Build command", "Start command").
- Dockerfile preset (settings.md): "Choose the same framework or **Dockerfile** preset used during server creation.
  Framework presets fill in Build Command, Start Command, and Port defaults that you can customize. The Dockerfile preset
  clears command overrides so your Dockerfile is used, and lets you set its path and listening port." CLI:
  `--dockerfile <path>` (relative to repo root), `--port <port>` ("Port your server listens on (default `3000`)").
- **How `PORT` is passed to the container: not documented.** No page says Manufact injects a `PORT` env var. The only
  statements are the configurable "listening port" (default 3000) and a Uvicorn example
  `uvicorn app:app --host 0.0.0.0 --port 3000 --timeout-graceful-shutdown 300` ("use the port configured for your server").
  The mcp-use self-hosted Dockerfile example sets `ENV HOST=0.0.0.0`, `ENV PORT=3000`, `EXPOSE 3000`.
- Platform-injected env vars (environment-variables.md): `MCP_URL` ("the public URL of the deployment") and `CSP_URLS`.
- Health checks: pipeline steps are Cloning, Analyzing, Building, Deploying, **Health Checks**, Complete. "a failure at
  Health Checks means the deployed server didn't respond correctly, which usually points to a wrong start command or a
  missing environment variable." Endpoint, interval and timeout: **not documented**.
- Graceful shutdown: previous process gets `SIGINT`; "Up to 300 seconds (5 minutes)" then possibly `SIGKILL`; "The
  300-second window is a maximum shutdown allowance, not a request-duration limit".
- Scale to zero: the Free plan has it implicitly; **"Prevent Scale to Zero" keeps your server warm so clients never hit a
  cold start. Available on paid plans** (Hobby+). Cold-start duration and idle timeout: **not documented**.
- Per-branch deploys: "Preview environments are created per branch", each with
  `https://<slug>--br-<branch>.run.mcp-use.com/mcp`; "Preview deployments require the Hobby plan or higher". Production
  branch auto-deploys on push; watch paths, branch allow list, "Wait for CI" available.
- Environment variables: scoped to Production or one Preview branch; add one by one, paste a `.env`, or `--env KEY=VALUE` /
  `--env-file`; "Saving variables does not restart your server. Trigger a redeploy". CLI "does not echo uploaded values".
  Whether values are masked or encrypted in the UI: not documented.
- Regions: AUTO (default, US East); EU/APAC/Global need Startup plan. "An MCP session stays in the region that started it,
  and OAuth and other non-MCP requests go to US East."
- Rate limiting: optional per-caller (real client IP) gateway throttle, off by default, default 1,000 requests/minute
  when enabled, `429` when exceeded.
- Public URL: `https://<slug>.run.mcp-use.com/mcp`; custom domains via CNAME to `deploy.mcp-use.com`.

## 3.2 Gateway: headers and non-/mcp routes (the two questions that matter)

- Forwarding of custom request headers such as `X-Uhlexa-User`: **not documented.** The gateway records "request metadata"
  and optionally bodies ("Capture payloads"); "Server configuration (JSON)" lists "custom headers" but that is for the
  dashboard's own test client, not client-to-server forwarding.
- `Mcp-Session-Id`: **not documented** anywhere on docs.manufact.com (searched all fetched pages). Uhlexa's design uses
  `stateless_http=True` with JSON responses (spec section 4, "MCP SDK 2.x facts"), so it does not depend on this header.
- Reachability of non-`/mcp` HTTP routes (`/simulator/`, `/board`, `/health`, `/api/*`): **not documented.** Every doc shows
  only `https://<slug>.run.mcp-use.com/mcp`. Weak evidence for pass-through: settings.md refers to "OAuth and other non-MCP
  requests" reaching the server in Global mode; billing.md says bandwidth covers "streamed responses, widgets, and asset
  serving"; the mcp-use servers expose `/inspector` etc. next to `/mcp`. Weak evidence against: no page promises it, and
  the gateway is described as MCP-aware (counts only `tools/call` as requests). Treat as **unverified until a smoke test on
  the first deploy**.

## 3.3 Free plan and pricing (docs.manufact.com/dashboard/billing + manufact.com/pricing.md)

- Free: $0/month, **$5 in monthly credits**, 2 projects, 30k requests/mo, 1 team member, 7-day analytics retention,
  community support; no Public Chat, no preview deployments, no Prevent Scale to Zero, region AUTO only.
- "Requests" = MCP `tools/call` only; discovery calls are not billed. Other meters from the same credit pool: build minutes
  $0.07/min, bandwidth (egress) $0.15/GB (includes streamed responses, widgets, asset serving), requests $0.10 per 1,000.
- Free exhaustion: "the gateway returns `402 Payment Required` until you upgrade or receive additional credits." Free orgs
  cannot set an overage limit.
- Hobby: $30/mo ($25/mo yearly), $30 credits, 5 projects, 300k requests, preview deployments, Prevent Scale to Zero.
  Startup (application, <$2M funding): $300/mo.
- **Conflict between pages fetched today:** docs.manufact.com/dashboard/billing and /dashboard/servers say Free has no
  "GitHub org deploys" ("Deploying from a GitHub organization repository ... requires the Hobby plan or higher"), while
  manufact.com/pricing and pricing.md list "GitHub organization deploys: Included" for Free. Our repo is
  `github.com/tatinc23/alexa-hackathon` (an org repo). Conservative reading: assume an org-repo deploy may be refused on Free
  and use `mcp-use deploy --no-github` (or a personal-account repo) if so.

## 3.4 Terms (https://manufact.com/terms, updated Sep 28, 2026; privacy updated Sep 2, 2026)

- 5 Acceptable use: do not "Deploy or run MCP servers that are malicious, abusive, or otherwise violate acceptable use";
  "Abuse third-party API connections (e.g., LLM providers) through the platform"; "Use the Service for any illegal or
  unauthorized purpose".
- 6: "You are responsible for your own API keys and any costs associated with third-party services ... You are responsible
  for all content processed through MCP servers you deploy or connect to the platform."
- 7: "Manufact may suspend or remove servers that violate these Terms or acceptable use, without prior notice."
- 11: they may terminate "immediately, without prior notice or liability, under our sole discretion, for any reason
  whatsoever"; export/deletion request within 30 days.
- 8 is stale/contradictory: "The Hobby plan is free. The Business plan is offered at custom pricing", but pricing shows
  Free $0, Hobby $30, Enterprise custom. "Manufact reserves the right to modify plan limits, features, and pricing."
- 15: 30 days' notice for material term changes.
- 1: signing in constitutes consent to "analytics and tracking technologies (such as cookies, session recording, and
  product analytics)"; 4: account creation subscribes you to marketing mail.
- Privacy: "not intended for children under 13". Gateway captures request metadata by default; "Capture payloads" and
  "Identify actors" are opt-in and docs warn "Names, emails, and payloads can contain personal data."
- Reading: the Service is defined as "MCP server hosting". Shipping a whole web app (simulator, board, REST, worker) in one
  container is not prohibited, but is not the described use either. Ambiguous, so keep the MCP endpoint first-class, keep
  payload capture off, and keep demo data fictional.

## 3.5 Design implications for Uhlexa

- Make the container port configurable and unambiguous: read `PORT` first, then `APP_PORT`, default **3000**; bind
  `0.0.0.0`; in the Manufact "Dockerfile" preset set the listening port to match; `EXPOSE` the same port; use exec-form
  `CMD ["uvicorn", ...]` so `SIGINT` reaches uvicorn (graceful shutdown), and set `--timeout-graceful-shutdown` modestly.
- Health: serve `GET /health` (no DB) and also answer 200 on `/`; the health-check target is undocumented, so make `/`,
  `/health` and `POST /mcp` all work. Cold start is undocumented: warm `/health` or `tools/list` before any live demo.
- Identity header: do not rely on `X-Uhlexa-User` surviving the gateway. Smoke-test on first deploy (curl with the header,
  compare against `ctx.headers`); keep the cookie / default-user fallback already in the spec.
- Simulator/board: smoke-test `GET <slug>.run.mcp-use.com/simulator/`, `/board`, `/health`, `/api/board` immediately after the
  first deploy. If non-`/mcp` routes are blocked, fallback is the MCP Apps resource `ui://uhlexa/family-board.html` for the
  board and running the simulator locally (docker compose) for the demo video; do not promise a public simulator URL until
  verified.
- Request budget: Free allows 30k `tools/call` and $5 of credits; demo traffic is far below that, but bandwidth (`$0.15/GB`)
  and 402-on-exhaustion apply to the public simulator if it is served through the gateway.
- Deploy path: Free + org repo may be refused (see conflict above). Plan: try GitHub App deploy; otherwise
  `mcp-use deploy --no-github`. Previews per branch are not available on Free; deploy `main` only.
- Secrets: set via dashboard/CLI `--env`; redeploy after changes; keep keys out of the repo (`.env*` is excluded from
  `--no-github` archives). `MCP_URL` is injected and can seed the app's public base URL.
- mcp-use docs warn `421 Misdirected Request` behind proxies with DNS-rebinding protection when `host="127.0.0.1"`; the
  official MCP SDK has equivalent transport-security settings, so bind `0.0.0.0` and disable/relax host checks behind the
  gateway (inference from mcp-use docs; verify on first deploy).

## 3.6 Blockers

- None that stop the build. Risks to verify on first deploy: non-`/mcp` route reachability, custom-header forwarding,
  cold-start length, org-repo deploy on Free.

## 3.7 Unknowns (all "not documented")

- Whether `PORT` is injected; health-check path/interval/timeout; cold-start duration and idle timeout before scale-to-zero;
  header forwarding; `Mcp-Session-Id` handling; non-`/mcp` route pass-through; maximum request/response size and gateway
  request timeout; whether env values are masked; Free-plan limit on image size, CPU or memory; the Free-plan org-repo
  conflict above.
- I did not use the connected Manufact MCP tools or create any server (the instruction said no probe needed).
