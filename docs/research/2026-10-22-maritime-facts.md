# Maritime Computers: build-team ground truth

**Checked 2026-10-22.** Every fact below comes from a page fetched or a live call made on 2026-10-22. Anything not
confirmed is marked "not documented" or "unverified". Sections 0-7 were written before any computer existed (only
`initialize`, `tools/list` and read-only REST GETs had been made); **section 8, "Live run", was added later the same day
after one computer was provisioned and driven for real, and it overrides sections 0-7 wherever they disagree** (it
resolves most of the section 7 unknowns).

## Sources (all fetched 2026-10-22)

- https://maritime.sh/docs/computers (and `.md` variant) - the Computers guide, main source
- https://maritime.sh/llms.txt and https://maritime.sh/llms-full.txt - docs index and full docs
- https://maritime.sh/docs/billing.md, https://maritime.sh/pricing, https://maritime.sh/docs/limits.md
- https://maritime.sh/terms (Terms of Service, "Last updated: May 17, 2026")
- https://maritime.sh/privacy (Privacy Policy, "Last updated: September 28, 2026")
- https://maritime.sh/changelog (Computers shipped ~2026-09-11 per the changelog entries)
- https://api.maritime.sh/openapi.json (OpenAPI, title "Maritime API" 0.1.0; no `servers` block)
- Live: `https://mcp.maritime.sh/mcp`, `https://mcp.maritime.sh/mcp/u/<id>`, `https://api.maritime.sh/api/...`
- Target page: https://todomvc.com/examples/react/dist/ and https://raw.githubusercontent.com/tastejs/todomvc/master/license.md
- https://pypi.org/pypi/mcp/json (Python MCP SDK versions)

## 0. Brief assumptions vs reality

| Brief assumed | Verdict |
| --- | --- |
| `MARITIME_MCP_URL=https://mcp.maritime.sh/mcp` | CONFIRMED. Docs show it; `GET /api/computers/plan` returns `"mcpUrl":"https://mcp.maritime.sh/mcp"`. |
| Pinned per-user URL `{MARITIME_MCP_URL}/u/{user_id}` | CONFIRMED: `https://mcp.maritime.sh/mcp/u/<externalUserId>`. Also pinnable with header `X-Maritime-User: <id>` on the plain endpoint. |
| Who chooses the id / format | WE choose it. The tool schema calls it any stable string of at most 256 characters: our own end-user id, not a Maritime id. Live: the server URL-decodes the path segment (`%40`, `%3A`, `%20`, `%2F` all accepted and echoed decoded). |
| Server-side `MARITIME_COMPUTERS_KEY` | CONFIRMED. Header `Authorization: Bearer mk_...`. A key whose only scope is `computers` is refused on every other Maritime endpoint (verified live, see section 2). |
| Streamable HTTP | CONFIRMED (stateless flavour, see 1.1). |
| Tools `get_computer`, `computer`, `request_takeover`, `takeover_status` | CONFIRMED to exist. The server exposes **10 tools**, not 4 (see 1.2). |
| screenshot -> act -> verify loop | CONFIRMED; it is the documented loop, each result carries a monotonic `frame_id`. |
| human takeover for login/CAPTCHA/2FA/payment | CONFIRMED; the tool descriptions and docs say exactly this. |

## 1. API facts

### 1.1 Endpoint, auth, transport

- MCP URL: `https://mcp.maritime.sh/mcp`. Pinned: `https://mcp.maritime.sh/mcp/u/<externalUserId>` (URL-encode the id,
  e.g. `urllib.parse.quote(id, safe="")`). Header pin: `X-Maritime-User: <externalUserId>` on the plain URL.
- Auth: `Authorization: Bearer mk_<...>` (the "Computers key", minted at https://maritime.sh/computers or Settings > API
  keys > Computers; shown once; keep server-side). A `manage` or full-access `mk_` key also works on the MCP server.
  No other auth header is documented.
- Missing/invalid auth: HTTP 401 with a JSON-RPC error body `{"jsonrpc":"2.0","error":{"code":-32600,"message":"..."},"id":N}`.
  The invalid-key message says Maritime rejected the key as an invalid or missing Computers API key; the missing-header message says to add the key as a Bearer
  Authorization header (both point to https://maritime.sh/computers).
- Transport: Streamable HTTP, **stateless**. Observed live: `POST` returns `content-type: text/event-stream` with a single
  `event: message` / `data: {json-rpc}` frame; **no `Mcp-Session-Id` header is issued**; `notifications/initialized` -> HTTP 202;
  `GET /mcp` -> HTTP 405 (JSON-RPC error -32000, method not allowed; no server-initiated stream);
  POST without credentials -> 401 even with no `Accept` header.
- `initialize` result (live): `protocolVersion` echoes the requested `2025-06-18`; with the Python SDK 2.3.0 the negotiated
  version was `2025-11-25`. `serverInfo`: `{"name":"maritime-computers","version":"0.1.0"}`. `capabilities`: `tools` and `logging`
  only (no resources/prompts). `instructions` (server-provided text): screenshots 1200x750 by default (physical 1280x800),
  coordinates are in the frame of the last screenshot, take a screenshot before acting, one computer per end user, call
  `get_computer` first (pass `user_id` unless the connection is pinned), then pass `computer_id` to every other tool. On a
  pinned connection the instructions append a sentence naming the pinned end user.
- Client timeouts: most MCP clients cut a call at 60 s. `get_computer` first use creates the desktop (3-8 s normal, up to
  60 s cold; later wakes about 3 s). `request_takeover` with `wait:true` blocks for minutes. Raise the per-call timeout for both
  (Claude Code: `MCP_TOOL_TIMEOUT` ms; Python SDK `Client(read_timeout_seconds=...)`).
- There is also a separate OAuth-gated resource at `https://mcp.maritime.sh/mcp/agents` (401 "sign-in is required"; advertised
  by `/.well-known/oauth-protected-resource`). It is the agents product, NOT Computers. Do not use it.

### 1.2 Tools (live `tools/list`, 10 tools, no `outputSchema`, no `annotations`)

`get_computer`, `computer`, `android`, `computer_batch`, `run_shell`, `read_file`, `write_file`, `request_takeover`,
`takeover_status`, `close_computer`.

**get_computer** - input `{user_id: string (required on the plain endpoint; optional on a pinned connection and must equal
the pin if given), name?: string, os?: "linux"|"windows"|"android" (new computers only, default linux), arch?: "x86_64"|"arm64"
(android only)}`. Idempotent: same id -> same computer. Returns (per docs) `computer_id`, status, screen size and a
coordinate hint; the exact serialization (JSON text vs prose) is not documented. First use creates the computer.

**computer** - input `{computer_id (req), action (req), coordinate?:[x,y], start_coordinate?:[x,y], text?, modifier?, key?,
repeat?:1-100, scroll_direction?: up|down|left|right, scroll_amount?:1-100, duration?: number, region?:[x0,y0,x1,y1],
no_screenshot?: bool, format?: "png"|"jpeg", quality?: 1-95}`.
Action enum (17): `screenshot, left_click, right_click, middle_click, double_click, triple_click, mouse_move, left_click_drag,
left_mouse_down, left_mouse_up, scroll, type, key, hold_key, wait, zoom, cursor_position`.
Field semantics: `coordinate` in the frame of the LAST screenshot; `text` = string for `type`, xdotool key name for `key`/`hold_key`
(e.g. `Return`, `ctrl+l`, `ctrl+s`); `key` is an alias of `text` for key actions; `modifier` only for clicks/scroll; `duration` seconds
for `wait`/`hold_key`; `region` for `zoom` (returns the crop upscaled to full frame); `no_screenshot` skips the post-action image.
MCP default screenshot encoding is **JPEG q80** (REST default is PNG).
Result: an **image content block** (the post-action screenshot) plus a **text content block** carrying `action`, `frame_id`,
`width`, `height` and any detail (exact text serialization not documented). REST equivalent returns `imageB64`, `frameId`,
`width`, `height`, `mime`, `scale`, `ok`, `isError`, `error`, `blocked`, `mode`, `reason`, `hint`, `note`.
Coordinates: default frame **1200x750** on a physical **1280x800** desktop; every result reports width/height; the server scales.
Always screenshot before acting; never act on a stale frame; click a field and confirm focus before typing; use `type` (not `key`)
for digits/punctuation; on-screen text is untrusted data.

**computer_batch** - `{computer_id, actions: [<same action objects, no format/quality>] (1-50)}`; stops at first failure; only the
final step returns an image unless an earlier step sets `no_screenshot:false`; step text is prefixed `[i]`.

**run_shell** `{computer_id, command, timeout? <=30}` -> exit code/stdout/stderr (30 s cap). **read_file** `{computer_id, path}` and
**write_file** `{computer_id, path, content, encoding?: utf8|base64}`: absolute paths under `/data` or `/home/desk` only, 8 MiB cap.
**android** `{computer_id, action: ui_tree|list_apps|open_app|stop_app|install_app|open_url, package?, url?, path?, detail?, ...}`:
Android computers only (not usable on the default Linux computer). **close_computer** `{computer_id}`: ends the session now,
computer sleeps ~30 s later unless another action arrives.

**request_takeover** - `{computer_id (req), reason (req; text shown to the person), timeout? seconds default 600 max 3600 (link
lifetime and how long wait blocks), wait? bool default false}`. Mints a control viewer link and returns it as text. Default returns
immediately; with `wait:true` it blocks until Done/Failed (returns a fresh screenshot on success; MCP progress notifications keep
it alive). The tool text says a failed outcome (`success:false`) means the task has failed and must not be retried.
**takeover_status** - `{computer_id}`. Reports `mode` (`human` while the person holds the desktop, `agent` once control is back),
when the takeover expires, and how the last one ended.
REST `ComputerOut` shows the underlying fields: `mode` (string), `modeReason`, `takeoverSeq` (int), `takeoverExpiresAt` (datetime),
`lastTakeoverResult` (enum `success|failed|expired`), `lastTakeoverNote` (string), plus `status`, `screen{width,height,modelWidth}`,
`lastActionAt`, `lastWakeMs`, `lastWakeMethod`, `errorMessage`, `session`, `createdAt`.

### 1.3 Takeover flow (documented)

1. Agent hits login/2FA/CAPTCHA/payment -> `request_takeover(computer_id, reason)` -> tool returns a link + (per docs) the reason.
2. Show link+reason to the human in your own UI. They open it, do the step, click **Done** (or **Failed**). The viewer page calls
   REST `POST /api/v1/computers/{id}/takeover/complete {success: bool=true, note: string<=4096}` (viewer token, control mode);
   response `{takeoverSeq, screenshot}`.
3. While a person holds the desktop every `computer` action AND screenshot is refused: HTTP 409 slug `human_in_control`,
   `retryAfterS: 15`. Poll `takeover_status` until `mode == "agent"`, or just retry an action.
4. Link facts: view-only "watch" links via REST `POST /api/v1/computers/{id}/viewer {mode:"watch"|"control", ttlS 30-3600 default
   600, reason<=512}` -> `{url, expiresAt, mode}`. Links are short-lived, bound to one computer and one mode, **revoked when the
   session closes, the computer sleeps, or a takeover completes**; a new control link revokes the previous one. Link URL host/format
   is not documented (treat as an opaque secret).
5. Watch/takeover/wake never open a session.

### 1.4 Lifecycle and persistence

- One computer per end user, created on first `get_computer`; a persistent Linux micro-VM (XFCE desktop, Chromium, LibreOffice),
  own disk, cannot reach other computers.
- Session = continuous use. First action with no open session opens one; screenshots count as actions; 5 minutes with no action
  closes the session and the computer sleeps (memory snapshot incl. open windows); next action wakes it in about 1 s (a few s if
  snapshot gone). Snapshot kept 7 days after last action; after that a fresh boot with the same disk (open windows lost).
- Files, browser profile and logins live on `/data` and `/home/desk` until the computer is deleted. Delete = disk and snapshots
  destroyed; deleted computers 404.
- `close_computer`: 30 s grace (an action inside the grace reopens the same session), then sleep.
- Waking can be refused: 402 `plan_lapsed`; 429 `concurrency` (retry 15 s) when awake machines already equal the plan's limit.
  An already-awake computer is never refused.

### 1.5 Limits and errors

- Limits: idle sleep 5 min; close grace 30 s; batch 50; `run_shell` 30 s; files 8 MiB under `/data`|`/home/desk`; viewer links 10 min
  default, 1 h max; **rate limits 30 creates, 300 actions, 30 viewer links per minute per key**; screenshot 1200x750.
- Error body (REST): `{"error": "<slug>", "message": "...", "retryAfterS": n|null}`; on MCP the same message arrives as a tool
  result with `isError: true`. Slugs: `no_plan`/`plan_lapsed` (402), `computer_limit` (402: no paid plan, no free slot, or persisted
  cap), `concurrency` (429), `human_in_control` (409), `unknown_action`/`validation`/`bad_path` (400/422), `wake_transient`/
  `wake_timeout` (503/500), `no_capacity` (503), `computers_disabled` (503), `not_found` (404).

### 1.6 REST surface (same key; field names camelCase)

Base host observed working: `https://api.maritime.sh` (docs say use the host printed on the Computers page; OpenAPI has no
`servers`). Routes (OpenAPI confirmed): `POST /api/v1/computers {externalUserId?, name?, os?, arch?}` (201 new / 200 existing),
`GET /api/v1/computers?externalUserId=`, `GET|DELETE /api/v1/computers/{id}`, `POST .../wake`, `POST .../sleep`,
`POST .../actions` (one action or `{actions:[...]}` max 50, optional `format`/`quality`), `GET .../screenshot?format=&quality=`
(image bytes with headers `X-Frame-Id`, `X-Screen-Width`, `X-Screen-Height`), `POST .../exec {command<=8192, timeoutS 1-30}`,
`GET|PUT .../files?path=`, `GET .../files/list?path=`, `POST .../viewer`, `POST .../sessions/close` (204 if none open),
`GET .../sessions`, `GET /api/v1/computers/usage?from&to&externalUserId`, `POST .../takeover/complete`.
`ActionRequest` also has `package`, `url`, `path`, `detail` (Android actions). A TypeScript SDK `maritime-sdk` `client.computers` is
announced for "the next SDK release"; use REST/MCP.

### 1.7 Pricing and plan

- **No per-minute, per-action, per-session or per-screenshot pricing.** Nothing is metered. A Computer is a "machine", the same
  unit as an agent; the plan counts machines. **Computers need a paid plan; the Free plan (3 agents) does not include them**
  (creating one on Free -> 402 `computer_limit`).
- Plans: Free $0 (3 machines, 1 awake at once); Starter $20/mo (20 machines, 5 awake); Growth $100/mo (100, 25); Scale $500/mo
  (500, 60); Dedicated $5,000/mo (custom). Overflow machine: $1.50 / $1.25 / $1.00 per month on Starter/Growth/Scale. Sleeping
  machines are free and don't count toward the awake limit. Each machine is 1 vCPU, 2 GB RAM, 5 GB SSD.
- Credits: new accounts get a **$5 AI-credit welcome credit** (30-day expiry) which only pays for model calls through Maritime's LLM
  proxy; it does not pay hosting and is irrelevant when we call our own model. Programs: Startups ($1,000 credit, apply), Students
  (see Unknowns for a doc inconsistency).
- **This account (live, `GET /api/computers/plan` with the alt key):** `seatPlan:"scale"`, `seatPlanStatus:"active"`, `machines:0`,
  `computers:0`, `maxMachines:500`, `maxAwakeMachines:60`, `androidAvailable:true`, `windowsAvailable:false`,
  `androidArmAvailable:false`, `mcp.connected:false`. So a Computers plan exists and is active; no computers exist yet.

## 2. Live probe (read-only)

Keys loaded with `set -a; . ./.env.secrets.local; set +a`; values never printed. Both keys are `mk_` + 43 chars (len 46).

```
# unauthenticated
curl -sS -X POST https://mcp.maritime.sh/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.1"}}}'
# -> 401 JSON-RPC error -32600 (missing Authorization)

# authenticated (computers key first)
curl -sS -X POST https://mcp.maritime.sh/mcp -H "Authorization: Bearer [REDACTED mk_ key]" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{...same initialize...}'                      # -> 200 SSE, serverInfo maritime-computers 0.1.0
curl ... -H 'MCP-Protocol-Version: 2025-06-18' -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'   # -> 202
curl ... -H 'MCP-Protocol-Version: 2025-06-18' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' # -> 200, 10 tools
```

Results:
1. `MARITIME_COMPUTERS_KEY` (plain endpoint): initialize 200, tools/list 200, 10 tools listed (names in 1.2).
2. `MARITIME_API_KEY_ALT` (plain endpoint): same, 200 and 10 tools. Both keys are accepted by the Computers MCP.
3. Pinned path `/mcp/u/uhlexa-probe-readonly` and header `X-Maritime-User: uhlexa-probe-readonly` (computers key): both pin; `get_computer`
   schema changes to `required: []` and the `user_id` description says (the schema marks `user_id` optional on a pinned connection and requires it to equal the pin if given)
4. Pinned id handling at `initialize`: UUID, `ken%40example.com`, `fam%3A123`, `a%20b`, `a%2Fb`, 256-char and 257-char ids all accepted
   and echoed URL-decoded (length is not enforced at connect time; enforcement presumably at `get_computer`, schema says max 256). `/mcp/u/`
   with an empty id -> 404.
5. Bad key (`mk_000...`) -> 401, JSON-RPC -32600, message says Maritime rejected the key.
6. REST `GET https://api.maritime.sh/api/v1/computers` -> 200 `{"computers":[]}` with BOTH keys.
7. REST `GET https://api.maritime.sh/api/computers/plan`: computers key -> **403** (message: key is scoped to Computers only; works on the
   Computers MCP server and `/api/v1/computers`; use a `manage` or full-access key for the rest of the API); alt key -> 200 with the plan JSON in 1.7.
   **Conclusion: `MARITIME_COMPUTERS_KEY` is the least-privilege Computers-only key; `MARITIME_API_KEY_ALT` is a broader (manage or full-access)
   platform key. Ship only the Computers key in the app; keep the alt key out of the app and out of Manufact.**
8. Python MCP SDK smoke test (isolated venv in `$TMPDIR`, `mcp==2.3.0`, latest on PyPI 2026-10-02): `Client` and `ClientSession` both
   connected, negotiated `2025-11-25`, listed the same 10 tools. Verified shape (key from env, never inline):

```python
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEY}"}, timeout=httpx2.Timeout(60.0))
async with http:
    async with Client(streamable_http_client(PINNED_URL, http_client=http)) as c:   # c.server_info, c.instructions
        tools = (await c.list_tools()).tools                                         # c.call_tool(name, args)
```

   Notes: SDK 2.x imports `httpx2` (not `httpx`); the spec's "from mcp import Client with an httpx transport" needs this exact shape.
   `Client(url_str)` alone cannot carry the Authorization header. In the dev sandbox `httpx2` failed TLS (`OSStatus -26276`, truststore cannot read
   the macOS keychain) until given `verify=ssl.create_default_context(cafile="/etc/ssl/cert.pem")`; this was a sandbox artifact, not a Maritime issue.
   The v1 SDK line (1.30.0 also current) was NOT tested.

Not done on purpose: `get_computer`, `computer`, `request_takeover`, `takeover_status`, `run_shell`, viewer minting (all need or create a computer).

## 3. Terms (read 2026-10-22; quoted at most once, rest paraphrased with section numbers)

**Terms of Service** (maritime.sh/terms, last updated 2026-05-17, Maritime AI, Inc.; this predates the Computers launch, so Computers are not named):
- S2: the service is a cloud platform for hosting AI agents; Maritime may modify/suspend/discontinue parts with reasonable notice.
- S3: account via Google/GitHub; you are responsible for all activity under the account; users must be 13+ (under 18 needs guardian consent).
- S5 Acceptable Use (list of things you may not do): break the law; deploy malware or agents meant to cause harm; "Perform unauthorized access to or scanning of third-party systems";
  crypto mining or resource abuse; spam, phishing or fraud; infringe IP; harass or harm people; disrupt the service; attack the service; resell or
  redistribute the service without written permission. Maritime may suspend immediately at its sole discretion.
- S7: you own Your Content; Maritime does not monitor it and may remove it if it violates the Terms. S11/S12: liability capped at 12 months of fees; you indemnify Maritime
  for your use and for violating third-party rights. S13: on termination, data is deleted after a 30-day retention window.
- **Nothing in the ToS or docs addresses automating third-party websites specifically, names off-limits sites, or lists prohibited targets. "Not documented."**
  Maritime's own docs treat browsing as normal and tell the agent to hand login, CAPTCHA, 2FA and payment steps to a human via takeover (i.e. the product design is
  not to bypass CAPTCHAs). Maritime's migration guides also list "scrapers" as a normal workload.
- Reading: using a Computer to operate a public page we are allowed to use, with a human present, is within S5. The risk is the *target site's* terms and the
  "unauthorized access" clause, so: only public pages, no login, no payment, no CAPTCHA, human handles anything else. The S5 resell/redistribute item is ambiguous next to the
  Build-a-platform docs that invite a desktop per end user; **conservative reading: one computer per demo user inside our own app, no exposing the key, MCP URL or raw access to third parties,
  nothing sold.**

**Privacy Policy** (last updated 2026-09-28):
- Maritime stores the full content of AI model calls only when an agent uses Maritime's own LLM credits (S2.5); we use our own model, so that clause should not apply to Computers screenshots.
  It is encrypted in S3, retained indefinitely, visible to authorized Maritime staff.
- Not documented: how long screenshots are retained, whether staff can view a computer, and whether computers have egress restrictions. The Computers docs only say typed text is never logged and never appears in errors.
- S10: the service is not intended for children under 13 and Maritime does not knowingly collect data from them. **Do not put real child data on a Computer.** Computer disks persist until deleted.
- Infrastructure subprocessors named: Stripe, Railway, AWS, Hetzner, Neon.

**Target-page terms:** TodoMVC is a public demo project (repo license MIT, copyright Addy Osmani, Sindre Sorhus, Pascal Hartig, Stephen Sawchuk), served from GitHub Pages;
`https://todomvc.com/robots.txt` returns 404 (none). I did not review GitHub Pages terms today; a few page loads per demo is negligible traffic. Unknown beyond that.

## 4. Safe public target page

**Proposed `MARITIME_SAFE_TARGET_URL=https://todomvc.com/examples/react/dist/`**
- Verified today: `curl` HTTP 200, `text/html`, tiny shell (`app.bundle.js`, `app.css`), `<title>TodoMVC: React</title>`; hosted by GitHub Pages (`server: GitHub.com`), no login, no payment, no CAPTCHA, no cookies needed.
- Verified in a real browser (local Browser pane, 800x600 viewport): page renders the new-todo input; clicking it, typing `milk` + Return, then `eggs` + Return
  renders both items in the list (page text: `milk`, `eggs`). State is browser `localStorage` only; nothing is sent to a server.
- Same-origin alternates that also return 200 today: `/examples/vue/dist/`, `/examples/preact/dist/`, `/examples/javascript-es6/dist/`. (`/examples/vanillajs/` is a 404.)
- Gotchas for the runner: (a) the Computer's Chromium profile persists, so items from earlier runs remain in localStorage; verify by "milk" and "eggs" being visible, not by count, or add a reset step;
  (b) at some widths the TodoMVC site shows a framework sidebar on the left; the todo input location depends on the frame, so take a screenshot first and use returned coordinates;
  (c) I did not check how it renders inside a 1200x750 Maritime frame (no computer was provisioned) and I did not verify that the Computer's Chromium can reach the site (egress policy not documented).
- Fallback: host a trivial static "Sunday groceries" page from the Uhlexa app itself (public URL on Manufact). It removes dependence on a third party and its terms; the cost is one more route to build and keep public.

## 5. Design implications for Uhlexa

1. **Pinned URL, server-side only**: `f"{MARITIME_MCP_URL}/u/{quote(str(user.id), safe='')}"`, built from the authenticated Principal. Never pass a `user_id` to `get_computer` (omit it; a mismatching one is refused anyway).
   Use a UUID or `[a-z0-9_-]` slug id (verified: any string works, but encode it). Store the returned `computer_id` in `provider_bindings` (type `maritime_computer`).
2. **Key**: use `MARITIME_COMPUTERS_KEY` (Computers-only scope). Keep `MARITIME_API_KEY_ALT` out of the app and out of Manufact secrets; it is a wider-scope key. Update DECISIONS.md: "which key the Computers MCP takes" is answered (both are accepted; use the Computers key).
3. **Client**: MCP Python SDK `mcp>=2.3` needs `httpx2` + `streamable_http_client(url, http_client=...)` as in section 2 item 8; the server is stateless (no session id), so a fresh `Client` per job is safe and cheap. The worker is sync; run MCP calls in an `anyio`/`asyncio.run` bridge per job.
4. **Tool allowlist in the adapter**: only `get_computer`, `computer`, `request_takeover`, `takeover_status` (+ `close_computer` at the end; `computer_batch` optional). Hard-refuse `run_shell`, `read_file`, `write_file`, `android`: the server exposes them on the same key, so
   the "no shell actions" rule must be enforced client-side. Restrict `computer.action` to the fixed plan's subset (`screenshot, left_click, type, key, wait, zoom`, plus `scroll` if needed); never accept action JSON from model output.
5. **Timeouts**: set the `get_computer` call timeout to at least 90 s (cold create up to 60 s); all other calls 30-60 s. `request_takeover` must use `wait:false` (default) in a chat/Family Board product; hold `takeover_url` in `browser_tasks`, status `needs_takeover`, and resume via `takeover_status`.
6. **Takeover flow maps cleanly**: `request_takeover(reason)` -> store link -> show on board -> poll `takeover_status` until `mode == "agent"` (REST/DB fields: `lastTakeoverResult` in `success|failed|expired`). `failed` (or `expired`) = terminal `failed`, no retry. Expect `human_in_control` (409, retry after 15 s) on any action meanwhile.
   Treat the takeover link as a secret: signed, ~10 min default life, anyone holding it controls the desktop. Show only to authorized household members, never log it, expire/clear it on completion.
7. **Screenshots**: arrive inline as MCP image content (base64, JPEG q80 by default, 1200x750). Safe to persist in our DB/blob store for `last_screenshot_ref`; consider `format:"jpeg", quality:60` to keep rows small. The text block carries `frame_id`/`width`/`height`: parse tolerantly (format not documented) and
   keep the monotonic `frame_id` in the step log to prove the verify step used a fresh frame.
8. **Verification is vision-only on Linux**: there is no DOM/accessibility tool for Linux computers (`ui_tree` is Android-only). "verified" must come from reading a post-action screenshot (SubQ Snap `verify_screenshot` or the model) with `zoom` for small text. `not_verified` stays the honest default.
9. **Opening the URL**: no `open_url` on Linux. The plan must do screenshot -> locate/launch Chromium (click taskbar/panel, the docs say do NOT use launcher keystrokes) -> `ctrl+l` -> `type` URL -> `key Return` -> `wait` -> screenshot. Whether Chromium is already open on a fresh computer is not documented.
10. **Idle/lifecycle**: computers sleep after 5 idle minutes with a memory snapshot and wake in ~1-3 s; call `close_computer` at job end (30 s grace) to free an awake slot (Scale allows 60 awake). A `computer_limit`/`concurrency`/`plan_lapsed` error must map to browser task status `unavailable` with the reason, never `succeeded`.
11. **Rate limits** (300 actions/min/key, 30 creates/min) are irrelevant at demo scale.
12. **Cost**: flat, no per-use charge, so a demo run costs nothing extra on this account (already on an active `scale` seat plan, 0 computers). One computer per demo user = one machine slot.
13. **Data hygiene**: demo household is fictional; keep it that way. Do not enter real names, child data or credentials into a Computer (disk persists until deleted; screenshot retention is undocumented).

## 6. Blockers

- **None hard-blocking** for the Maritime adapter: auth works, tools are present, the plan is active, a safe target exists.
- Not yet proven end to end: the first live `get_computer` + `computer` run (it creates a machine on the account). That is a deliberate, explicit step for the build team and Ken's go-ahead (Ken's pre-approval says to read keys and make a few live test calls; creating a computer is stateful, so log it in DECISIONS.md and delete the test computer afterwards with `DELETE /api/v1/computers/{id}` if it is not the demo computer).
- Minor environment note (not Maritime): `.env.secrets.local` line 7 (`NEON_DATABASE_URL`) contains an unquoted `&`, so `. ./.env.secrets.local` prints a zsh parse error at that line (the Maritime lines load fine). Quote the value or load the file with a dotenv parser.

## 7. Unknowns (all "not documented" or unverified)

> Status after the live run (section 8): resolved are the result serialization of `get_computer`, `computer`,
> `request_takeover`, `takeover_status` and `close_computer` (all JSON in a text block), the takeover link's host and
> path shape, which Chromium launch path a fresh computer has (already open), and egress to todomvc.com (works). Still
> open: items 2 (a takeover longer than 5 minutes), 5 (screenshot retention, staff visibility), 7-8 (student offer,
> why the account is on `scale`), 9, and a takeover that a person completes with Done or Failed.

- Exact serialization of `get_computer`, `takeover_status` and `request_takeover` results (JSON-in-text vs prose), the takeover link's host/path, and the exact `computer` text-block format. Resolve with the first live run and pin it in adapter contract tests.
- Whether human activity during a takeover keeps the computer awake (docs: 5 min with no *action* sleeps the computer and viewer links are revoked when it sleeps). A takeover longer than 5 minutes might lose its link.
- Which Chromium launch path a fresh Linux computer has, and the browser binary name; no `open_url` for Linux.
- Whether the Computer has unrestricted internet egress (needed for todomvc.com); no allow/deny list documented.
- Screenshot retention, staff visibility into computers, and data-processing terms specific to Computers. The ToS (2026-05-17) pre-dates the Computers launch (changelog ~2026-09-11) and doesn't mention them.
- Whether hosted MCP connectors (Anthropic Messages/OpenAI Responses) pass image tool results to the model; unneeded if we use our own client (our design).
- Student offer inconsistency: `llms.txt` says 3 months free of Growth or Scale; the pricing FAQ says 4 months free of Starter; the pricing page also contains an unexplained "$20/mo for 7 agents" string. Prices elsewhere agree.
- How this account came to be on `scale` (list price $500/mo): possibly a promo or credit. Check https://maritime.sh/billing before leaving computers running; not determinable from the API calls made.
- The v1 MCP Python SDK API (1.30.0), the REST `url`/`package`/`path` fields on Linux, and `os: windows` (`windowsAvailable:false` on this account) were not tested.

## 8. Live run (2026-10-22, about 15:00-15:12 UTC)

One real computer, one real run, made by the build team under Ken's pre-approval for live test calls (DECISIONS.md;
section 6 above asked for a stateful step like this to be logged). Recorded here so nobody repeats it blind.
Nothing secret is recorded: no key, no computer id, no control-link value.

### 8.1 What ran and what it cost

- Computer: the demo user Alex's pinned computer (the app stores its id in `provider_bindings`, type
  `maritime_computer`; the id is not recorded here). It is **kept** (not deleted) and now sleeps; it is the demo
  computer. Connection: `MARITIME_COMPUTERS_KEY` over the pinned URL `.../mcp/u/<uuid>`, `get_computer` called with
  **no** `user_id` (pinned) and it worked. The reply echoes the pinned id as `external_user_id`.
- Budget used: 1 `get_computer`, 33 `computer` calls (30 returned a frame, 2 were refused with `human_in_control`
  during the takeover probe, 1 final screenshot), 1 `request_takeover`, several `takeover_status`, and a `close_computer` after each run.
  31 screenshots were kept as evidence in the scratch folder (not in the repo).
- Sequence: 16 exploration actions (frames 1-16), then the product's own fixed plan `list_add_items` run once through
  `MaritimeClient.run_list_add` (frames 17-30), then the takeover probe (section 8.5).
- The plan's outcome was `succeeded` with `verified=False`. That is correct and deliberate: the harness answered the
  first two checkpoints (it had looked at the exploration screenshots) and left the final check `not_verified`, so the
  product never claimed more than it could confirm. A person then read the final screenshot: **both "milk" and "eggs"
  were visible** in the TodoMVC list ("2 items left!"). No SubQ Snap call was made in this run.
- Cost/accounting: nothing is charged per use on this seat plan (section 1.7). Note for the demo: the `close_computer`
  reply carries a session summary with `invocations_charged: 1`, `wall_seconds`, `actions`, `screenshots`; the REST
  `GET /api/v1/computers/usage` returns the same counters (OpenAPI `UsageOut`: counts only, no money field). Counters
  exist, a price per invocation is not documented: check https://maritime.sh/billing before a large demo.

### 8.2 The desktop a fresh computer starts on

- Screenshots are **JPEG** (`image/jpeg`), **1200x750**, physical 1280x800. A desktop frame is about 23 KB; a frame with
  a full web page about 65-73 KB. `frame_id` is monotonic per computer across runs (the plan run began at frame 17).
- XFCE-style desktop, taskbar along the bottom (a **Menu** button, quick-launch icons for a browser and a terminal, one
  taskbar button per window, a clock showing UTC date and time at the right). Without a window: dark background with
  **Home** and **File System** icons at the top left.
- **Chromium is already open, maximised, on `about:blank`, with the address bar focused and its text selected** the first
  time the computer is used. No launcher click is needed, so no open-URL action is needed either.
- **The taskbar button of the active window MINIMISES it** (a click at about (240, 736) sent the browser to the desktop).
  Never click it. `alt+Tab` is the safe way to bring the browser forward: a no-op when it already is in front, it
  restores a minimised one. The runner starts with `alt+Tab`.
- `ctrl+shift+n` opens an **Incognito window** (title "New Incognito Tab - Chromium", a dark page reading "You've gone
  Incognito", an **Incognito** badge at the right of the toolbar, an "AI Mode" button in the address bar). It becomes the
  front window and its address bar is focused. This gives clean storage each run (TodoMVC keeps its list in browser
  storage, so a normal window piles up "milk, eggs, milk, eggs") and leaves nothing on the computer.
- `ctrl+shift+w` closes the Incognito window and returns to the normal `about:blank` window exactly as it was (verified).
  Only press it after a screenshot confirms the front window IS the Incognito one: on the normal window it would quit
  Chromium.

### 8.3 Opening the page and the TodoMVC geometry

- `ctrl+l`, `type` the URL (the address bar shows a suggestion list, first entry is the URL itself), `key Return`,
  `wait 3` (seconds): `https://todomvc.com/examples/react/dist/` is fully loaded after that 3 s wait. So the computer
  **can reach the internet** (todomvc.com at least; no egress block was seen).
- At 1200x750 the page shows the TodoMVC **framework sidebar on the left** (React text, "Official Resources" ...) and the
  app in the right two thirds: the big "todos" heading, then the input "What needs to be done?" with its centre at
  **(733, 256)** in the frame. Items appear below it as rows at about y=315 (first) and y=371 (second), with the footer
  "N items left!" and the All / Active / Completed filters under them. (A framework sidebar is a layout fact the earlier
  section 4 only guessed at.)
- `left_click` at (733, 256) focuses the field (its placeholder shows a caret). Then per item: `type` the text, `key
  Return`. The field keeps focus after Return, so the next item can be typed straight away. After `milk` and `eggs` a
  1 s `wait` returns a frame showing both rows.
- Every action (`key`, `type`, `left_click`, `wait`) returns the post-action screenshot in the same result, so the plan
  never needs a separate `screenshot` between steps.

### 8.4 Exact result formats (live; JSON inside the text block, no `structuredContent`)

Pin these in contract tests; the fake server in `tests/contracts/test_maritime_contract.py` answers in exactly these shapes.

- **`get_computer`**: text block, one JSON object with sorted keys: `arch`, `computer_id`, `external_user_id`, `hint`,
  `mode` (`"agent"`), `model_frame` `{height:750,width:1200}`, `os` (`"linux"`), `screen` `{height:800,width:1280}`,
  `status` (`"ready"`). No image.
- **`computer`**: one **image block** (`image/jpeg`) plus one **text block** holding JSON with keys `action`, `blocked`,
  `coordinate`, `error`, `frame_id`, `height`, `hint`, `is_error`, `mode`, `note`, `ok`, `path`
  (`/data/desktop/screenshots/latest.png`), `physical` (`[1280,800]`), `reason`, `scale` (`1.0667`), `width`. On success
  **every one of `blocked`, `error`, `reason`, `hint`, `is_error`, `mode`, `note`, `ok`, `coordinate` is `null`**. A reader
  that treats `"error":null` as an error is wrong (the first version of the adapter did exactly that: its loose
  `name: value` prose pattern matched `"error":null`; fixed, covered by a regression test).
- **`request_takeover`** (`wait:false`, `timeout:45` was honoured): text block, JSON `{expires_at, instructions, status:
  "waiting", viewer_url}`. `expires_at` is the request time plus `timeout`. The `instructions` text tells the caller to
  send the link to the person through its own channel, that the desktop is theirs until they click Done or Failed, that
  actions return `human_in_control` meanwhile, and not to mint a second link while one is live. **Link shape:
  `https://maritime.sh/computer/<computer id>?t=<token>`** (host `maritime.sh`, a `t` query token). It is a secret; the
  adapter returns it to the caller and never logs it or puts it in an error.
- **`takeover_status`**: text block, JSON `{last_takeover_note, last_takeover_result, mode, takeover_expires_at}`;
  `mode` is `"agent"` or `"human"`, `last_takeover_result` is `null` or `"expired"` (`"success"` and `"failed"` are
  documented but were not produced: nobody clicked).
- **`close_computer`**: text block, JSON `{closed: true, session: {actions, bytes_out, client, computer_id, end_reason
  ("closed"), ended_at, grace_until (ended_at + 30 s), id, invocations_charged, last_action_at, model_hint, screenshots,
  started_at, wall_seconds, ...}}`. The `client` field is the SDK's user agent (the Python SDK 2.x showed `python-httpx2`).
- Tool errors arrive as `isError` results whose text starts with the slug, e.g. `human_in_control: a person holds the
  desktop, retry after 15 s` (that exact message was seen live). The other slugs in section 1.5 were not triggered.
- The MCP **server `instructions`** and each tool's `description` match section 1.1 and 1.2; the connection negotiates
  fine with Python `mcp` 2.3 + `httpx2`; the adapter's `streamable_http_client(..., terminate_on_close=False)`
  worked live against the stateless server.

### 8.5 Takeover, as it behaved

1. `takeover_status` on a fresh computer: `mode: agent`, nothing else set.
2. `request_takeover(wait=false, timeout=45)` returned at once with the link (no image). `takeover_status` flipped to
   `mode: human` immediately.
3. Two `computer` calls made while it was `human` were refused with the `human_in_control` tool error (mapped to the
   adapter's `HumanInControl`).
4. Nobody opened the link, so it **expired**. `expires_at` passed, yet `mode` was still `human` 21 s later; at the next
   poll (about 48 s after that) it was `agent` with `last_takeover_result: "expired"` and stayed so. **Expiry is applied
   lazily, up to about a minute after `expires_at`.** `close_computer` while a takeover is open returned normally.
5. Meaning for the product: after `needs_takeover`, `mode == "agent"` alone does NOT mean the person finished: read
   `last_takeover_result` as well (`MaritimeService.takeover_state`). `expired` or `failed` means the task failed and
   must not be retried (documented, section 1.2); only `success` means continue.
6. Not exercised live: a person completing the takeover (Done or Failed), the viewer page itself, and a takeover longer
   than 5 minutes (section 7 item 2).

### 8.6 What the adapter does because of this

- `app/providers/maritime/runner.py` holds the two fixed plans, written from the sequence above: `list_add_items`
  (screenshot, `alt+Tab`, `ctrl+shift+n`, verify "is Incognito", `ctrl+l`, type URL, `Return`, `wait 3`, verify page
  ready, click (733, 256), per item `type` + `Return`, `wait 1`, verify items visible, `ctrl+shift+w` if the window was
  confirmed private, `close_computer`) and `sign_in_check` (the same opening, then one verify; it never types into the
  page). Verification is an injected callback (production: SubQ Snap `verify_screenshot` with the JPEG as image input; no
  verifier or any failure of it means `not_verified`); `needs_human` at any checkpoint requests a takeover and leaves the
  computer open (closing it would revoke the link).
- Validation happens before any connection is opened: bad items (not 1-10 printable single-line strings of at most 40
  characters) or a target that is not a plain http(s) URL produce a `failed` outcome with no network call.
- Contract tests (`tests/contracts/test_maritime_contract.py`) run against an in-process fake MCP server that exposes the
  documented tools and answers in the formats of 8.4; the fake also exposes the five forbidden tools so a test can prove
  they are never called.
