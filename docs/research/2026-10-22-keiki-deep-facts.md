# Keiki deep facts: external harness, channels, conversations, gateway (Uhlexa build ground truth)

Checked 2026-10-22 (fetches and probes between 15:20 and 15:35 UTC). Every statement below comes from a page fetched,
package source read, or read-only probe run on 2026-10-22. "Not documented" means the pages and source I read do not say;
it is not a claim the feature is absent. Items marked **(inference)** are my reading, not vendor text. Items marked
**(self-generated)** are computed by me, not supplied by the vendor.

Raw copies (not in the repo): `/tmp/claude-501/keiki2/` (`$TMPDIR/keiki2/`): `llms-full.txt`, `llms.txt`, `openapi.json`,
`docs_*.md`, `pricing.md`, `cdn/src/*` (SDK source), `npm/*.json`, `fresh/*` (tunnel script, terms, privacy), `live/*`
(probe bodies, secrets not stored).

## Sources

- https://onkeiki.com/llms.txt, https://onkeiki.com/llms-full.txt (366 KB, every doc + blog in one file)
- https://onkeiki.com/openapi.json ("Keiki Agent API" 1.0.0, license Proprietary)
- https://onkeiki.com/docs/external-harness.md, /docs/harnesses.md, /docs/cli.md, /docs/security.md,
  /docs/conversation-auth.md, /docs/mcp.md, /docs/partner-deployments.md, /pricing.md, /developers, /startups
- https://onkeiki.com/.well-known/keiki-turn-keys.json, /.well-known/mcp.json
- https://onkeiki.com/api/v1/harness/tunnel.sh (read as text only, never executed; it is the vendor's own working client
  of the harness API)
- https://onkeiki.com/terms and /privacy (terms "Last updated August 18, 2026")
- npm registry JSON for `@onkeiki/agents` 0.1.4, `@onkeiki/channels` 0.1.3, `@onkeiki/ai-sdk` 0.1.4, `@onkeiki/eve` 0.1.3,
  `@onkeiki/flue` 0.1.3, `@onkeiki/cli` 0.1.2, and their source via `cdn.jsdelivr.net/npm/@onkeiki/<pkg>@<ver>/...`
  (`channels/src/{types,signature,turn-keys,signing-secret,turn-auth,client}.ts`, `ai-sdk/src/handler.ts`,
  `agents/src/{Cloud,Kit,Server,Tool,Alert}.ts`, `agents/src/types/{config,message}.ts`, `eve/dist/extension/**`)
- PyPI JSON API (9 names), GitHub API for `Mail-0` org and `Mail-0/labs`

## 0. Corrections to `2026-10-22-keiki-neon-manufact-facts.md` section 1

1. That doc said the key "holds the `agents:chat` scope". Live `GET /api/v1/keys` today shows our key `alexa_keiki_key`
   has `scopes: ["*"]`, `agentId: null` (org-wide), no expiry, not revoked. Wildcard, not chat-only.
2. That doc said no agent exists. An agent **"Uhlexa"** (id `1cafe1d6-477a-4b5d-a58d-6875ddc405d4`) exists, created
   2026-10-21T18:50:34Z from the dashboard, model `minimax/minimax-m3.1-flash:free`, `harness: "flue"`, `harness_config: null`.
   It is simply not API-gateway-enabled, which is why `/openai/models` is empty.
3. That doc's recommendation "leave Keiki off" is superseded by Ken's 2026-10-22 scope decision (Keiki core, external-harness mode).
4. Phone calls, SMS/RCS and A2A are explicitly "not live yet" (early access). See section 3.

---

## 1. External harness protocol, end to end (Q1)

### 1.1 Three connectivity modes

`harness: "external"`, `harness_config: { mode, dispatchUrl?, timeoutMs? }`. `timeoutMs` max 600000 (10 min), default 10 min.

| mode | Who initiates | Needs public URL | Needs signing secret | Auth of the app to Keiki | Notes |
| --- | --- | --- | --- | --- | --- |
| `push` | Keiki `POST`s the turn to `dispatchUrl` | yes, public `https://` | no (Ed25519 platform signature) or optional per-agent `whsec_` | per-turn `Keiki-Turn-Token` (`ht_...`) or `kts1.` credential | Synchronous: "if the endpoint is down, the turn fails rather than being queued." Supports mid-turn arrivals. Agent page probes the endpoint and shows last delivery outcome |
| `relay` | a connected relay claims via `GET /api/v1/harness/turns/next` and forwards raw body + signature headers to the app | no | optional | harness API key for the relay | Eve/Flue docs: relay "requires a connected Keiki development relay (`keiki dev`); it is not shipped yet." No arrivals. **Do not plan on it** |
| `inbox` | app long-polls `GET /api/v1/harness/inbox/next?wait=25` | no | no | "harness API key" (Bearer) for polling; per-turn `turnToken` for turn routes | Queued while offline; one active turn per conversation; pending turns for a conversation merge on claim |

Dispatch URL restrictions (harnesses doc, Security): Keiki "will not push turns to private networks, redirects, URLs with
credentials, or plain HTTP unless a self-hosted deployment opts in." CLI doc: "Localhost and private network destinations
are not accepted by production." So the URL must be public HTTPS, no 3xx.

### 1.2 Dispatch request (push, and the body shape also used for relay)

```
POST <dispatchUrl>
Content-Type: application/json
Keiki-Turn-Id: <turnId>            # must equal body.turnId (SDK rejects mismatch)
Keiki-Turn-Token: ht_...           # short-lived bearer for this turn's routes; never log/persist
Keiki-Signature: t=<unix-sec>,v1=<hex>[,v1=<hex>...]                 # HMAC; docs say every request has it, but also that an agent with no signing secret gets "only" the Ed25519 header, so expect it only when a secret exists
Keiki-Signature-Ed25519: t=<unix-sec>,kid=<kid>,v1=<base64url-sig>  # "when Keiki's platform key is configured"
```

Header names have no `X-` prefix (unlike identity/action webhooks, which use `X-Keiki-Timestamp` + `X-Keiki-Signature:
sha256=...`, a different scheme). Lookup is case-insensitive. The HMAC parser accepts multiple `v1=` entries (any match
passes) and rejects a duplicate `t=`.

Body (`@onkeiki/channels` 0.1.3 `KeikiTurn` type plus the SDK's own validator; vendor docs only list the field groups):

```json
{
  "type": "keiki.turn",
  "version": 1,
  "turnId": "turn-123",
  "agentId": "agent-456",
  "conversationId": "+15555550123",
  "heldBack": false,
  "silent": false,
  "quietLoop": false,
  "channel": { "channel": "sms" },
  "messages": [ { "text": "hello", "messageId": "message-789",
                  "replyTo": { "messageId": "m-1", "text": "quoted text" },
                  "screening": { "flagged": false }, "silent": false } ],
  "history": [ { "role": "user", "content": "...", "cursor": "...", "at": "2026-10-22T12:00:00Z" } ],
  "context": { },
  "instructions": "",
  "keiki": { "apiBase": "https://keiki.example/api/v1",
             "turnUrl": "https://keiki.example/api/v1/turns/turn-123",
             "mcpUrl": "https://keiki.example/api/v1/harness/mcp" },
  "deadline": "2026-10-22T12:10:00.000Z"
}
```

Field facts:
- Required by the vendor validator: `type`, `version===1`, non-empty strings `turnId`/`agentId`/`conversationId`, booleans
  `heldBack`/`silent`/`quietLoop`, `channel` object with string `channel`, `messages[]`, `history[]`, `context` object,
  string `instructions` (may be empty), `keiki.{apiBase,turnUrl,mcpUrl}` strings, string `deadline`.
- `channel`: an open object. Only `channel` (string) is validated; other keys are not documented. The tool-context type in
  `@onkeiki/agents` lists `channel` values `'sms'|'telegram'|'whatsapp'|'x'|'slack'|'teams'|'agentmail'|'api'|'agent'|'synthetic'`
  and optional `inboundText, messageId, userId, provider('photon'), gateway, isDirect, teamId, channelId, threadTs, threadId,
  inboxId, phoneNumberId, businessScopedUserId, chatConversationId, fromAgentId, tenantId, conversationId`. Whether the wire
  `channel` object carries the same keys is **not documented**. In the arrival example `channel` is a plain string
  (`"sms"`), while in `keiki.turn` it is an object: parse both shapes.
- `messages[]`: "every current inbound message" (bursts are merged by steering). Entry with `silent: true` is "excluded from
  prompt guarding and inbound recording". `replyTo` present for threaded replies. `screening: {flagged:true}` = soft
  prompt-injection flag (hard Shield matches are refused before dispatch, so the app never sees them).
- `history[]`: bounded; role is `user`|`assistant` in the validator. The docs say team-sent messages show as role `team` in
  `conversation_search` and in the SDK history; if that leaks into `history`, the vendor's own parser would reject it, so
  **Python must tolerate unknown roles** (do not hard-fail the turn). Window size = agent `history_limit` (1 to 200; Uhlexa agent: 16).
- `heldBack`: playground, Live Test, internally steered and "agent-dispatch" turns. Replies sent to the turn API are
  "captured as the turn's answer and are not delivered to the contact." `silent`: nothing should be delivered (the vendor AI SDK
  handler consumes the model stream but sends nothing). `quietLoop`: scheduled loop run that may legitimately say nothing.
- `context`: "structured context", contents **not documented**. `instructions`: "prompt instructions", whether it includes
  the agent's own `system_prompt` is **not documented** (vendor AI SDK passes it as the model `instructions`).
- `deadline`: ISO string; the turn token and slot are released "after observing the outcome, cancellation, or deadline".
- Not in the payload: provider credentials, platform API keys, contact secrets ("Provider credentials and platform API keys
  are not included").
- No `memory` field. Per-user memory is a Keiki managed feature (`features.memory`, currently `false` on Uhlexa); how/whether
  it reaches an external harness is **not documented**.

### 1.3 Verifying the dispatch

Window: 300 s default on both schemes. Verify against the exact raw bytes before parsing JSON. De-duplicate on `turnId`
(Keiki can retry a dispatch: Eve doc "Retried push dispatches are deduplicated per session using the most recent 20 Keiki turn
IDs"; Flue "a retried dispatch is admitted once while duplicate receipts still receive `202`").

Scheme A, HMAC (agent has a signing secret):
```
expected = hex( HMAC_SHA256(key=signing_secret, msg = "<t>" + "." + raw_body) )
valid    = any v1 in header equals expected (constant-time); require /^[0-9a-f]{64}$/i; |now - t| <= 300
```
Scheme B, Ed25519 (no secret needed; used alone when the agent has no secret):
```
msg  = "<t>" + "." + <Keiki-Turn-Token header, "" for cancellations> + "." + raw_body
sig  = base64url-decode(v1)            # header regex: ^t=(\d+),kid=([\w-]+),v1=([\w-]+)$
pub  = JWKS key with matching kid from https://onkeiki.com/.well-known/keiki-turn-keys.json  (kty OKP, crv Ed25519, alg EdDSA)
```
Vendor JWKS handling: cache 5 min, refetch on unknown `kid` at most every 30 s, fail 503 `turn_keys_unavailable` if keys cannot
be fetched and cache is stale. Live JWKS today: one key, `kid = DN6QA6pKiv1PfnCm`, `x = 6GgdWi_yxgK5lkQjZ2ToL4nxm-TYV3JcSySvnkjz1aM`.

Per-turn bearer for turn routes: either the dispatch `Keiki-Turn-Token` (`ht_...`) or a credential you mint when you hold the
signing secret ("Runtimes that cannot carry the dispatch token between processes"):
```
payloadB64 = base64url_nopad( JSON {"a": agentId, "t": turnId, "ts": unixSeconds} )   # compact JSON, keys in this order
kts1       = "kts1." + payloadB64 + "." + hex( HMAC_SHA256(secret, "keiki-turn-auth.v1." + payloadB64) )
Authorization: Bearer <kts1>      # bound to agent + turn, "expires after five minutes", mint fresh per request
```

Signing test vector: **the vendor publishes none** (grepped docs, llms-full, README, source). Self-generated vector below
(secret is a made-up string; HMAC and kts1 computed in Python and cross-checked in Node `crypto`; Ed25519 computed and
verified in Node only, key derived from seed bytes 0..31, `kid` and token are fake):

```
secret   = whsec_TESTVECTOR_not_a_real_secret
t        = 1789600000   (use nowSec=1789600000 to bypass the stale check)
body     = {"type":"keiki.turn","version":1,"turnId":"turn-123","agentId":"agent-456","conversationId":"+15555550123","heldBack":false,"silent":false,"quietLoop":false,"channel":{"channel":"sms"},"messages":[{"text":"hello","messageId":"message-789"}],"history":[],"context":{},"instructions":"","keiki":{"apiBase":"https://keiki.example/api/v1","turnUrl":"https://keiki.example/api/v1/turns/turn-123","mcpUrl":"https://keiki.example/api/v1/harness/mcp"},"deadline":"2026-10-22T12:10:00.000Z"}
Keiki-Signature: t=1789600000,v1=b636f0c4860123d9898f5aa40a344c4552b3bc5029d30937cd4beeac908cb5af
kts1 (a=agent-456,t=turn-123,ts=1789600000):
  kts1.eyJhIjoiYWdlbnQtNDU2IiwidCI6InR1cm4tMTIzIiwidHMiOjE3ODk2MDAwMDB9.86de5d08b75b797563a414fa7ae8ab2990799118dc1cbdbe8d8c08dca00845d2
Ed25519 (token ht_TESTTOKEN, public JWK x = A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg):
  Keiki-Signature-Ed25519: t=1789600000,kid=TESTKID,v1=Wr_f_XVMSt2I8nc3ohXB_XNLMgOMXCAHHIoURtmNAtD5eGH8boA7hRET7Ef59YNtLqeOSi6qNCa5ZSfOsPUAAw
```

Python sketch of the verifier (my port of `@onkeiki/channels` 0.1.3 `signature.ts`/`turn-keys.ts`; stdlib for HMAC;
Ed25519 needs `cryptography` or PyNaCl, which are **not installed in this sandbox**, so the Ed25519 half is untested in Python):
```python
def verify_hmac(raw: bytes, h, secret: str, now=None, tol=300) -> None:
    t, sigs = parse_sig(h["keiki-signature"])            # t=..., one or more v1=...
    if abs((now or time.time()) - int(t)) > tol: raise Stale
    exp = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).digest()
    if not any(hmac.compare_digest(exp, bytes.fromhex(s)) for s in sigs): raise Invalid
def verify_ed25519(raw: bytes, h, jwks_get_key, now=None, tol=300) -> None:
    t, kid, sig = re.fullmatch(r"t=(\d+),kid=([\w-]+),v1=([\w-]+)", h["keiki-signature-ed25519"].strip()).groups()
    msg = f"{t}.{h.get('keiki-turn-token','')}.".encode() + raw
    Ed25519PublicKey.from_public_bytes(b64url(jwks_get_key(kid))).verify(b64url(sig), msg)
```

### 1.4 Required response to the dispatch

- Any 2xx once accepted. Docs: respond `202`. Every vendor adapter returns `202` with `{"accepted": true}` (also for
  arrivals, cancellations and duplicates). Whether Keiki reads the response body: **not documented**.
- The model loop continues asynchronously and reports back through the turn API (1.5). Do not hold the HTTP request open.
- Vendor error bodies (use the same): `{"error": "<code>"}` with `400` for `malformed_payload`, `401` for
  `missing_signature|invalid_signature|stale_signature|turn_mismatch|missing_token|unknown_agent`, `500`
  `signing_secret_missing`/`signing_secret_unavailable`, `503` `turn_keys_unavailable`.
- Keiki-recorded dispatch failure codes (visible in the trace with a remediation hint): `signing_secret_missing`,
  `no_dispatch_url`, `unreachable`, `signature_rejected`, `app_rejected`, `relay_offline`, `turn_timeout`, `app_error`,
  `cancelled`. Dispatch HTTP timeout, retry count and backoff: **not documented**.

### 1.5 Turn-scoped API (`turnUrl` from the payload)

Auth on every route: `Authorization: Bearer ht_<Keiki-Turn-Token>` or `Bearer kts1.<...>`; "The URL's turn ID must match the
credential." Use `turn.keiki.turnUrl` verbatim (doc examples show `https://<host>/api/v1/turns/<turnId>`).

| Route | Request | Response / failure (documented + vendor-client behavior) |
| --- | --- | --- |
| `POST {turnUrl}/messages` | `Content-Type: application/json` `{"text":"...","idempotencyKey":"..."}` (key optional, recommended; vendors use `<turnId>:step:<n>`) | 2xx success; reusing a processed key returns a duplicate ack, body `{"duplicate":true}`, nothing re-sent. After a delivery error: `502` with `retryable:false` (outcome unknown). Retrying that key returns `409 {"error":"previous_attempt_failed"}`; send a new key to retry anyway (contact may get it twice). `409 {"error":"turn_closed"}` after completion/cancel/deadline. Success body shape beyond `duplicate`: not documented. Not auto-retried by the vendor client |
| `POST {turnUrl}/stream` | `Content-Type: application/x-ndjson`, one JSON per line: `{"type":"text-delta","text":"..."}` and `{"type":"message-break"}` | Slack: appended live to the active stream; otherwise buffered and delivered at message breaks or blank lines. Not auto-retried (outcome unknown). Vendor docs: live token streaming is not bridged by the Flue/Eve adapters (only AI SDK uses `/stream`) |
| `POST {turnUrl}/typing` | no body | Shows typing where the channel supports it. Vendor client retries 5xx twice (50 ms, 100 ms) |
| `POST {turnUrl}/lease` | no body documented | Refresh a live claim; response includes `leaseExpiresAt`. Lease length: not documented. (Stated under Inbox mode; behavior for push turns: not documented) |
| `GET {turnUrl}/arrivals?after=<int>` | cursor `after` >= 0, default 0 | `200 {"arrivals":[{"sequence":1,"messages":[{"text","messageId","replyTo?","screening?"}]}]}`, up to 50 batches ordered by sequence; use highest sequence as next cursor. Only batches accepted by push dispatch are pullable; requires an open turn (closed turn: `409 turn_closed` or 401 per the vendor handler) |
| `POST {turnUrl}/complete` | `{"status":"completed"\|"failed","error?":"...","consumedArrivals?":[1,2],"usage?":{"model?","tokensIn?","tokensOut?","costUsd?"}}` | Repeats are acknowledged as duplicates; late call `409 turn_closed` (treat as done). Vendor client retries on status 0 or >=500 at 1 s, 5 s, 15 s. "Complete the turn after all messages and tool calls have settled." |

Rules: a `/messages` send returning ok or `turn_closed` marks an arrival "answered"; report processed arrival sequences in
`consumedArrivals` on `completed` (on `failed`, only those already answered); unreported arrivals are requeued at the front
and answered by a later turn.

Held-back turns: same calls succeed but the contact receives nothing.

Turn MCP: `keiki.mcpUrl` (`.../api/v1/harness/mcp`) serves the turn's Keiki tools, same Bearer. Vendor adapters describe them as
"reply, files, reactions, Slack/Teams/Telegram/email/X tools, capabilities, skills"; delivery tools are `reply` and
`send_message` (a successful call ends the turn in the AI SDK helper). Not needed for a plain text reply via `/messages`.

### 1.6 Push-only follow-ups from Keiki to the app

- `keiki.turn.arrival` (only with `midTurnStrategy: append`, eligible push turns; polled about once a second):
  `{"type":"keiki.turn.arrival","version":1,"turnId","agentId","conversationId","heldBack","channel":"sms","keiki":{apiBase,turnUrl,mcpUrl},"sequence":1,"messages":[{"text","messageId",...}]}`
  Same signature headers plus `Keiki-Turn-Id` (required) and `Keiki-Turn-Token`. Answer 2xx. The pull API is the source of truth;
  an app may ack the push without processing it.
- `keiki.turn.cancelled`: `{"type":"keiki.turn.cancelled","turnId","agentId","conversationId"}`, signed; Ed25519 message uses an
  empty token segment. Sent with `midTurnStrategy: interrupt` (Keiki cancels the active turn and starts a fresh one with the
  correction) or when the turn is cancelled. A late `/complete` then returns `409 turn_closed`.
- `queue` (default for Slack docs): the new message waits and starts the next turn when this one ends.

### 1.7 Inbox mode details

```
GET /api/v1/harness/inbox/next?wait=25      # wait 0..30 seconds
Authorization: Bearer <HARNESS_API_KEY>
-> 200 {"turn": null}                        # wait expired
-> 200 {"turn": { ...turn..., "turnToken": "...", "keiki": {"turnUrl": "..."} , "deadline": "..." }}
```
Then `POST {turn.keiki.turnUrl}/messages` with `Authorization: Bearer <turn.turnToken>` and a stable `idempotencyKey`, then
`POST .../complete`. Documented behavior: claims are leased; if the app dies the lease expires and a later poll reclaims with a
**rotated turn token** (message idempotency state survives); only one active turn per conversation; a fresh claim merges
pending turns for that conversation in arrival order; nothing is delivered to contacts while offline; test/steer/Live Test
turns need a recent poll or fail with `inbox_offline`. The vendor's "connect dot" prompt says: complete every claimed turn,
refresh the lease before expiry, "Never send a contact message outside the turn API."
**Not documented:** the exact field list of the inbox `turn` object (the doc says "the turn, deadline, `turnToken`, and
turn-scoped URL"; the prompt text uses `turn.keiki.turnUrl` and `turn.turnToken`), lease duration, and what a "harness API key"
is. Evidence from the vendor's tunnel script (below) suggests it is an **agent-bound** developer API key ("Use the key the agent page
offers for the agent"); an org-wide key returned `agents: 0` from `GET /api/v1/harness/ping` today. **(inference)**

### 1.8 Sequence for Uhlexa in push mode

1. Contact messages the Keiki agent on a channel. 2. Keiki steering debounces/merges, takes a turn slot. 3. Keiki POSTs
`keiki.turn` to Uhlexa `/keiki/turn`. 4. Uhlexa verifies signature on raw bytes, upserts a `keiki_turns` row keyed by `turnId`
(unique), enqueues a job, returns `202 {"accepted":true}`. 5. Worker answers (Uhlexa logic), `POST {turnUrl}/typing` (optional),
`POST {turnUrl}/messages` (idempotency key stable per reply), `POST {turnUrl}/complete {"status":"completed"}`. 6. Cancellation
or arrival pushes arrive on the same URL; handle by `type`.

---

## 2. Creating and configuring an external-harness agent (Q2)

### 2.1 What exists today

- Agent "Uhlexa" already exists (dashboard-created). Live `GET /api/v1/developer/config?agent_id=1cafe1d6-...`:
  `harness:"flue"`, `harness_config:null`, `execution_mode:"platform"`, `webhook_url:null`, `history_limit:16`,
  `features:{mcp:true,media:true,guards:true,sandbox:true,steering:true, memory/browser/scrape/escalation/promptGuard/loops/wallet/computer/contactCards/operatorHandoff/dynamicUi:false}`.
- There is a second, unused org key "Default" (`*`).

### 2.2 Ways to set `harness:"external"` and `dispatchUrl`

| Route | Evidence | Body | Notes |
| --- | --- | --- | --- |
| Dashboard agent page ("Bring your own agent" / setup card) | external-harness doc says "configure its connection with the developer API"; changelog 2026-10-09: "open the agent page and follow the setup card"; vendor tunnel script error text: choose "Bring your own agent" on the agent first; dashboard can "tunnel to a local push agent and monitor it" | UI | I cannot log in, so exact labels/screens are **not verified** |
| `PATCH https://onkeiki.com/api/v1/developer/config` | The vendor's own `tunnel.sh` does exactly this with an agent-bound key. Org-wide key: add `agent_id` in the JSON body (documented for `/config`) | `{"agent_id":"<id>","harness":"external","harness_config":{"mode":"push","dispatchUrl":"https://.../keiki/turn","timeoutMs":600000}}` | "A PATCH only changes the fields it sends." The script preserves other `harness_config` keys and drops `lastSeenAt`/`lastDelivery`. Not executed by me (no writes). Whether an org-wide key may change `harness` (vs agent-bound) is **not documented** |
| `kit.sync()` = `POST /api/v1/developer/sync` | SDK 0.1.4 `Kit.ts`: body includes `harness: "external"`, `harness_config:{mode: connectivity, dispatchUrl, timeoutMs(ms)}` plus `name, model, system_prompt, history_limit, features, tools[], ...`. Docs: "Include `harness` on every sync; omitting it sends `flue` and switches an existing Hermes or external agent back to Flue." | n/a | Needs `agents:write`, `tools:write`, `config:write`, `alerts:write`. How sync picks the target agent (by `name`?) is **not documented**; do not use against the existing agent without testing, it also overwrites prompt/tools |
| `PUT /api/v1/agent-channels/dispatch` `{"mode":"push","dispatchUrl":"..."}` | Vercel Connect doc; needs an OAuth token for `https://onkeiki.com/api/v1/agent-channels` ("An org needs a plan to grant it"). Live: with our API key `GET /api/v1/agent-channels` -> `401 "This Keiki connection is invalid or expired. Reconnect Keiki."` | n/a | API key does not work there; OAuth only |
| `POST /api/v1/developer/deployments` | Inline agent spec `harness` accepts only `flue` or `hermes` (doc), so it cannot create an external agent | n/a | Deployments are for partner-per-user agents |

SDK equivalent (TypeScript, `@onkeiki/agents` 0.1.4):
`harness: { type: 'external', connectivity: 'push'|'relay'|'inbox', dispatchUrl?, timeoutMs?: '5 minutes' | number }`.

### 2.3 Signing secret (optional)

`POST /api/v1/developer/agents/:agentId/harness/signing-secret` returns a `whsec_...` value shown **once** (response field name not
documented; store it immediately). `kit.sync()` does not create it. Rotation immediately breaks apps holding the old value (CLI
doc). Needed for HMAC verification and for minting `kts1.` credentials; not needed for Ed25519-only verification or inbox mode.
Required key scope for this route: **not documented**.

### 2.4 Scopes

Documented scope model: `<resource>:read` for GET, `<resource>:write` for mutations (write implies read); resources `config,
agents (incl. versions, storage), tools, lines, secrets, alerts, usage, people, outcomes, handoffs`; plus `deployments:*`,
`trace_exporters:*`, `keys:manage`, `agents:chat` (gateway), `*`. Notice route: `agents:write` or `*`. `/sync`: the four write scopes
above. Org-wide-only routes (a key bound to one agent gets `403 An org-wide API key is required`): `/api/v1/keys`, `/orgs`,
`/billing`, `/audit`, `/usage`, `/people`, `/phones/*`, `/photon/*`, `steering_config`, every secret write.

**Can our key do it?** The key is `["*"]` (live `GET /api/v1/keys`), so by the docs it is allowed to call `PATCH /config`,
`/sync`, the signing-secret route and the notice route. I have not executed any of them, so "allowed" is from the scope list, not
demonstrated. It is org-wide (`GET /api/v1/harness/ping` -> `agentId:null, agents:0`), which matters for harness-key routes (1.7).

### 2.5 Exact steps for Ken (dashboard path; labels unverified, I cannot sign in)

1. https://onkeiki.com/dashboard -> Agents -> **Uhlexa**.
2. On the agent page find the setup card / harness choice, pick **Bring your own agent** (external). Choose a mode:
   Push (needs the Uhlexa public URL, path `/keiki/turn`), or Inbox (nothing to enter).
3. Push: paste dispatch URL, e.g. `https://<slug>.run.mcp-use.com/keiki/turn` (only after the Manufact route smoke test), leave
   timeout default, save. Optional: generate the signing secret, copy it once into 1Password (team vault) and Manufact env
   `KEIKI_SIGNING_SECRET`.
4. Settings -> API keys -> create a key **bound to Uhlexa** (name it e.g. `uhlexa-harness`); needed for inbox polling and for
   `GET /api/v1/harness/ping`. Save into 1Password, then `.env.secrets.local`.
5. Use the agent page's Live Test/playground to send one test turn (held back, reaches no contact). Confirm Uhlexa received it
   (delivery outcome shown on the agent page, trace in Logs).
6. Channels: see section 3 (iMessage line costs money; Telegram/Slack/email are free to attach).
Alternative with no clicking: the PATCH above with the existing key, run by the build team once the dispatch URL is verified.

---

## 3. Channels (Q3)

Self-description: "SMS, iMessage, WhatsApp, Telegram, Slack and email". The cloud-agents doc says channels are added in the
dashboard (Channels page) "one identity per iMessage line, bot token or inbox".

| Channel | How attached | Provider / cost | Status |
| --- | --- | --- | --- |
| iMessage | Dashboard Channels -> add iMessage line: **buy a dedicated line in Keiki** or **import a line you already have**; REST: `channels:[{type:"imessage"}]` on `POST /developer/deployments` ("Keiki provisions a dedicated line"); `POST /developer/phones/acquire {"pool","strategy"}` | Keiki-bought lines are Photon (`provider:"photon"`); Linq mentioned for effects. Phone lines: $300/mo each on Starter ($150 Pro, $99 Max), "billed separately"; "Until launch, the channel setup shows the current provider price before purchase". Startup program: free iMessage line for one month | Live. What "import a line you already have" means (which providers, how) is **not documented**. App cards/Apps add-on need a Keiki-bought line |
| SMS / RCS | Dashboard Channels -> Add channel -> SMS/RCS (request) | n/a | **"not live yet" (early access)**. `channel:"sms"` appears in doc examples with an E.164 `conversationId`; whether that is iMessage-with-SMS-fallback is not documented. Android/SMS users of an iMessage line "fall back to SMS or RCS, where carrier rules apply" |
| Phone calls (voice) | Dashboard Channels -> Add channel -> Phone calls (request) | n/a | **"not live yet" (early access)**. No voice API/docs. Do not plan on Keiki for calls |
| A2A (agent-to-agent comms) | request form | n/a | early access, not live |
| WhatsApp | `channels:[{type:"whatsapp",access_token,phone_number_id,app_secret,verify_token,waba_id?}]` (Meta Cloud API creds) or Kapso numbers | your Meta account | Live |
| Telegram | `{type:"telegram",bot_token}` | free bot token | Live; groups mention-gated by default |
| Slack | `{type:"slack",bot_token,signing_secret,client_id?,client_secret?}`; manifest generated | your Slack app | Live; richest approvals/handoff UX |
| Email | `{type:"agentmail",region,mode:"create"\|"existing",inbox_id?,username?,domain?}` (AgentMail inbox) | AgentMail | Live |
| Teams, X chat | appear in `ChannelEnvelope` and adapter text | n/a | details not documented here |
| API gateway (`api`) | toggle "API gateway" on the agent, or `channels:[{type:"api",passthrough?}]` | org API key | Live (section 6) |
| Web playground / Live Test | dashboard | free | held-back turns |

Attaching to an agent: dashboard (agent -> Channels), or REST `POST /api/v1/developer/deployments` (create/update by `external_ref`,
channels additive, sandbox orgs get `402 plan_required` when requesting channels, `status` per channel `connected|needs_payment|error|skipped`,
`checkoutUrl` for payment) or OAuth `POST /api/v1/agent-channels/channels`. External-harness agents use the same channels;
"Messages on any of them reach the same turn route; nothing changes in your app." Any agent in the org can use any of the org's
lines ("whichever API key the agent belongs to"); outbound Photon messages go out on the sending agent's line.

**Bring-your-own number/provider (Twilio, Inkbox, etc.):** not documented anywhere. The only documented BYO channel credentials
are Telegram, WhatsApp (Meta), Slack, AgentMail, plus "import a line" for iMessage. There is **no generic inbound webhook channel**
other than the `api` gateway (your client calls Keiki, the completion is the reply). So Inkbox cannot "feed" Keiki as a channel;
to use Keiki's runtime for an Inkbox message, Uhlexa would call the gateway (and for external agents that path is unverified).

iMessage line operating limits (best-practices doc): guideline 5,000 messages/day/line, <50 new conversations/day; hard ceilings
7,000 sent messages and 50 new conversations per active line per UTC day (`daily_line_volume_reached`); opt-outs honored
(`opted_out`); inactive ~2 months can be deactivated; "Let the user text first".

---

## 4. Conversations and memory (Q4)

- Identification: `conversationId` string in every turn, scoped to the agent (a key bound to one agent gets `404` for a sibling
  agent's records). Documented examples: SMS/iMessage = contact E.164 phone (`"+15555550123"`); API gateway = your
  `x-conversation-id` (1 to 64 chars `[A-Za-z0-9_-]`), traced as `api:<agentId>:<conversationId>`; Slack identity example
  `C_BILLING:1789599000.000100` (channel:thread); WhatsApp `wa:<number>` or `wa:<BSUID>`; Telegram private chat per sender,
  Telegram group = one shared conversation (unless `identity:"user"`). Email thread id format: not documented.
- Sent to the harness: `messages` (all current inbound), bounded `history` (role/content/cursor/at; window =
  `history_limit`), `context` (opaque), `instructions` (opaque), `replyTo` quoting. Memory: not documented as a field
  (Keiki's memory feature is off on Uhlexa). Uhlexa should keep its own authoritative thread store and treat `history` as a
  bootstrap/cross-check.
- Reading a conversation or transcript:
  - No documented **API-key** route to list/get conversations or messages.
  - `GET /api/v1/traces?limit=` and `GET /api/v1/traces/:traceId` (API key, 14-day retention): per turn `trace.phone` (conversation
    identity), `metadata.inboundText`, `channel`, `harness`, model/tokens/cost, spans with inputs/outputs. Live today `{"traces":[]}`.
  - Keiki MCP `https://onkeiki.com/mcp` (OAuth, a signed-in org member, org needs a plan): "read conversations and traces", tools
    named after dashboard endpoints (`get_agents`, `get_agents_by_id`, ...); the conversation tool names are not documented.
  - Dashboard Conversations/Logs; in-turn tool `conversation_search` (agent-side).
  - Unprefixed dashboard routes such as `GET|PUT|DELETE /conversations/:phone/contact-card` exist but are session-auth routes.
- Cross-agent read/resume: **no documented way for agent B to read or resume agent A's conversation**. Documented peer features:
  agents in the same **group** can use `ask_agent` (peer runs its own turn with its own knowledge and tools, returns plain text; at most
  three hops; contacts never see it; refused after 20 asks/hour to one peer on one thread), a **group shared note** (4,000 chars,
  `group_note_write`, hub-only writes in tiered groups), identity scopes `agent:delegate` (peer calls) and `agent:configure` (shared note
  writes), and early-access A2A. Doc: "Playground, internally steered, and agent-dispatch turns are held back", so a peer ask to an
  external-harness agent plausibly arrives at the harness as a `heldBack:true` turn **(inference, not stated)**. Therefore the
  "another authorized agent resumes the thread" requirement must live in Uhlexa's own DB (Uhlexa already owns thread state); Keiki
  conversation ids are just keys into it.
- Keiki retains traces 14 days; attachments 90 days by default; conversation data controller is the customer (privacy page).

---

## 5. Proactive outbound outside a turn (Q5)

There is **no documented "send this text to contact X now" endpoint**. What exists:

1. `POST /api/v1/developer/agents/:agentId/notice` (OpenAPI `sendDeveloperAgentNotice`; key `agents:write` or `*`; org-wide or
   agent-bound key for its own agent). Body `{"text": "...", "phone": "+15551234567"}` (`phone` optional). "The text is written for the
   agent, never delivered verbatim. The agent decides whether and how to tell the person in its own words." `phone` must identify an
   **existing conversation** with that agent (else `404`). With `phone`: `200 {"ok","conversations","ran","queued","unavailable",
   "results":[{"phone","outcome":"ran|queued|unavailable"}]}` (`queued` = inside the ~2 s debounce window or behind a running turn).
   Without `phone`: broadcast to up to 200 recent conversations, `202 {"ok":true,"started":true,"conversations","unavailable","results"}`.
   `400` missing text / empty phone. For an external-harness agent, whether a notice becomes a dispatched turn (and with which
   flags) is **not documented** (traces doc calls notices "turns the platform starts itself").
2. **Loops**: agent-scheduled (cron/interval/once) workflows (`create_loop`, `run_loop`; developer loop presets in `/sync` as
   `loop_presets[{name,description,prompt,schedule_type,schedule_expression,timezone,quiet}]`). The turn payload has `quietLoop`,
   so loop runs probably dispatch to the harness **(inference)**.
3. **Alerts**: `defineAlert` with cron/interval/`webhook` schedule, broadcast to subscribers; `AlertDelivery {phone,text,media?,dedupKey?}`;
   registered via `POST /api/v1/developer/alerts/sync`. Described for SDK/cloud agents; the trigger route for `webhook` alerts is
   not documented; applicability to external-harness agents not documented.
4. **Operator handoff**: `POST /api/v1/developer/handoffs` (needs `operatorHandoff` feature and a Slack destination) announces work to a
   *human operator* in Slack with buttons (webhook.invoke / capability.tool actions, optional `form`, `Idempotency-Key`); it does not
   message the contact.
5. In-turn only: `POST {turnUrl}/messages`, MCP `reply`/`send_message`. Inbox doc: "Never send a contact message outside the turn API."
6. Uhlexa -> Keiki dashboard test only: Keiki MCP `talk_to_agent` sends "to one of your agents as a real user"; "nothing is sent to a real
   line".

Consequence: "notify a parent that an approval is waiting" should go **through Inkbox** (real iMessage/SMS/email) from Uhlexa, or via
the notice route if the parent must receive it as the Keiki agent (agent-mediated wording, requires a prior conversation, unverified
for external agents).

---

## 6. OpenAI-compatible gateway (Q6)

- `GET /api/v1/openai/models` -> `{"object":"list","data":[{"id","object":"model","created","owned_by","name"}]}`, only agents whose
  **API gateway** is on. `POST /api/v1/openai/chat/completions`: `model` = agent name or id; `messages`; optional `stream`, `user`, `tools`,
  `tool_choice`, `response_format`, `reasoning_effort`; header `x-conversation-id` (optional; omitted = new conversation; the response
  returns `x-conversation-id` to reuse). 200 completion or SSE ending `data: [DONE]`; `400/401`; `403` key lacks `agents:chat` **or**
  agent gateway disabled; `404` no enabled agent; `409` turn already running for that conversation (one turn at a time per
  conversation); `500` turn failed. Non-streaming responses carry a `keiki` object naming conversation, agent, trace. Anthropic
  equivalent: `POST /api/anthropic/v1/messages` with `x-api-key`. Sampling params ignored.
- Persistence: "A conversation that arrives this way is stored, searchable and traced just like one that arrived over SMS";
  billed; the agent's prompt/tools/memory/history window apply; if `messages` has only the new turn the stored conversation is the
  context, if it has earlier turns that transcript is the context; a client `system` message is context, not a replacement
  (unless Passthrough mode).
- Gateway toggle: dashboard Agent -> API gateway -> Turn on; or deployment `channels:[{type:"api"}]`. Today Uhlexa's gateway is **off**
  (`models` empty).
- **Interaction with the external harness: not documented.** What is documented: passthrough/tool parking "applies to the managed
  harness"; "An agent running on your own server (`executionMode: 'sdk'`) receives the turn as it always did" (legacy self-host, not
  `external`); trace `metadata.harness` is described as `flue` or `hermes`; held-back semantics for non-channel turns. So whether a
  gateway turn on an `external` agent is dispatched to the harness, and whether the completion then returns whatever Uhlexa posts to
  `/messages`, needs a smoke test before relying on it.
- Simulator as a channel into the same agents: possible in principle (`api` channel, `x-conversation-id` per simulated person), but
  (a) agent must have gateway on; (b) external-harness behavior is unverified; (c) every call is a stateful, billed Keiki turn. The
  simulator can equally call Uhlexa directly; use the gateway only for a "through Keiki" demo path, after a one-message smoke test.

---

## 7. Steering, escalation, handoff, approvals, built-in browser/sandbox (Q7)

Relevant to approvals:
- **Steering** (kept by Keiki in external mode): debounce window, `maxBatch`, `midTurnStrategy` = `queue` | `interrupt` | `append`
  (append -> arrivals, interrupt -> cancellation push + fresh turn). Example config `steering:{debounceWindow:'2 seconds',midTurnStrategy:'interrupt',maxBatch:5}`;
  agent feature `steering:true` already on. Stop commands ("stop", "cancel that") abort the running turn on Slack.
- **Governed actions** (`policy.approval:"ask"`, `requiredScopes`, `previewTool`, `freshnessFields`): durable action records
  `pending -> executing -> completed|failed|declined|superseded`, expire after 24 h; Slack cards, or yes/no in words
  (`action_respond`); webapp routes `GET /api/webapp/agents/:id/actions`, `POST .../actions/:actionId/decide`, `.../groups/:groupId/decide`
  (dashboard session auth, owner/admin); lifecycle webhook `action.<status>` signed with `X-Keiki-Timestamp`/`X-Keiki-Signature`
  (5 s timeout, no retries). Applies to **Keiki-side tools** (webhook tools uploaded to Keiki, SDK tools, MCP tools). Tools that run
  inside Uhlexa are outside it. A turn with no verified speaker refuses every tool with `requiredScopes`.
- **Identity webhook** (`identity:{webhook_url,secret}` on a deployment only today) maps sender -> `userId`, `role`, `scopes`,
  `ttlSeconds` (default 15 min, max 24 h), optional deterministic `reply`. "The partner API is the only place it is set today."
- **Escalation**: feature `escalation` (`routes:{webhook,slack:{channel},email}`, `requireConfirmation`); always queues a handoff in the dashboard
  Handoffs queue; agent tool `resolve_escalation`; unclaimed handoffs close after 7 days; once a teammate claims it the agent stays silent.
  Slack handoff thread: Reply to visitor / Hand back to agent / Close thread; `//` messages stay internal; a human reply takes the
  conversation over (hold lapses 30 min after last reply when not escalated). How takeover interacts with dispatch to an external
  harness (does Keiki stop sending turns?) is not documented.
- **Human supervision of Keiki turns**: dashboard steer (held back), Live Test, Retry on failed messages (replays original messages in the
  same conversation).
Do not use for the demo (Maritime does browsing): **Browser** (cloud browser, optional handoff to a person, screenshot delivery;
`features.browser`), **Computer use** (desktop automation, exclusive with Browser), **Scrape/crawl**, **Sandbox** (persistent code
sandbox per conversation; `features.sandbox` is currently **true** on Uhlexa), **Executor** (background jobs), **MCP integrations**
(`features.mcp` is true), **Wallet**. In external mode your app runs the loop; these only matter if you call Keiki's MCP tools.

---

## 8. Early access, credits, rate limits (Q8)

- Early access (dashboard form, no API): **Phone calls, SMS/RCS, A2A**: Channels -> Add channel -> that option, or the agent editor's
  Channels section including draft agents; state purpose and expected monthly call/message volume; "the team will follow up"; request
  can be updated later. Terms s11: early access/preview/beta features are "provided as-is, may change or disappear".
- Startup program (https://onkeiki.com/startups): "$500 in platform credit", "A free iMessage line for one month", founder support;
  eligibility: founded within 10 years, live website for a software product; reply "within a few days"; questions to nizzy@0.email.
  Application form not submitted by me.
- Pricing (pricing.md, "Plan preview"): "Can I purchase a proposed plan? Not yet." Starter $1/mo for 3 months then $20/mo incl. $20 credits;
  Pro $100/mo; Max $500/mo; lines $300/$150/$99 per month. Credits cover "AI replies and supported platform tools". With an external
  harness, "Model tokens your own provider serves are between you and that provider. You pay for message delivery through the usual
  counters." (harnesses doc, "Traces and billing"; written for hosts you run, i.e. Hermes; applying it to `external` is my
  inference, the external-harness page says nothing about billing)
- Credit balance: dashboard Settings -> Billing only. No documented endpoint. Live `GET /api/v1/billing` and `/api/v1/orgs` ->
  `404 {"error":{"code":"not_found","message":"No API operation matches GET /api/v1/billing."...}}`. Only `GET /api/v1/developer/usage` ->
  `{"turns":0,"messages":0,"tokens_in":0,"tokens_out":0,"tool_calls":0}`.
- Rate limits: none documented for the REST API; no `RateLimit`/`Retry-After` headers in any of the 22 probe responses (two runs). Documented caps: one running turn per
  conversation (gateway `409`); iMessage line caps (section 3); x-conversation-id 64 chars; 64 client tools per gateway request;
  Keiki MCP codemode 120 s / 100 calls; `talk_to_agent` returns a `jobId` after 40 s; notice broadcast up to 200 conversations;
  `delivered` up to 10 items of 2,000 chars; identity `reply` 2,000 chars; sign-in attempts rate-limited.
- Org plan state: unknown. Evidence for a free/sandbox org: agent model is a `:free` slug; docs say a "sandbox organization that requests
  channels receives 402 plan_required"; MCP and agent-channels OAuth need "a plan". Confirm in dashboard.

---

## 9. SDK languages and raw HTTP for Python (Q9)

- **TypeScript only.** npm: `@onkeiki/agents` 0.1.4 (MIT, modified 2026-10-17), `@onkeiki/channels` 0.1.3, `@onkeiki/ai-sdk` 0.1.4,
  `@onkeiki/flue` 0.1.3, `@onkeiki/eve` 0.1.3, `@onkeiki/cli` 0.1.2 (binary `keiki`: `login|whoami|logout|init|dev`; `dev` is a
  cloudflared/ngrok tunnel). Docs also name `@onkeiki/agents-plugins` and storage packages; npm returns 404 for
  `@onkeiki/agents-plugins` today (docs/registry mismatch). Repo field points to `github.com/Mail-0/sms-kit`, which returns 404
  (private). Public `Mail-0` repos are TypeScript/Rust and unrelated to a Python SDK (`labs/agents/lovable` is a `kit.sync()` example).
- **No Python SDK found**: PyPI JSON returns 404 for `keiki`, `onkeiki`, `keiki-agents`, `onkeiki-agents`, `keiki-sdk`, `onkeiki-sdk`,
  `keiki-channels`, `keiki-python`, `orchid-agents`. `openapi.json` covers only the gateway, deployments, MCP senders, trace
  exporters and notice; harness routes are documented in prose only.
- Raw HTTP a Python app needs (all `Authorization: Bearer ...` unless noted; base `https://onkeiki.com`):
  - Receive: `POST /keiki/turn` (our route): verify (1.3), `202 {"accepted":true}`.
  - Turn calls (1.5): `POST {turnUrl}/messages|stream|typing|lease|complete`, `GET {turnUrl}/arrivals?after=`.
  - Inbox: `GET /api/v1/harness/inbox/next?wait=25`.
  - Admin: `PATCH /api/v1/developer/config`, `GET /api/v1/developer/config?agent_id=`, `GET /api/v1/developer/agents`,
    `POST /api/v1/developer/agents/:id/harness/signing-secret`, `POST /api/v1/developer/agents/:id/notice`,
    `GET /api/v1/harness/ping` (undocumented in prose; returns `{ok,apiKeyId,agentId,agents,harness,harnessConfig}`),
    `GET /api/v1/traces[?limit=&status=error]`, `GET /api/v1/traces/:id`, `GET /api/v1/developer/usage`, `GET /api/v1/keys`.
  - Gateway: `POST /api/v1/openai/chat/completions` (+ `x-conversation-id`), `GET /api/v1/openai/models`.
  - Python libs: `httpx` (client), `cryptography` or `PyNaCl` (Ed25519), stdlib `hmac`/`hashlib`/`base64`.

---

## Live probe (read-only GETs, 2026-10-22 15:28 UTC, key never printed)

`set -a; . ./.env.secrets.local; set +a; curl -sS -H "Authorization: Bearer $KEIKI_API_KEY" https://onkeiki.com<path>`

| GET | Status | Result |
| --- | --- | --- |
| `/api/v1/openai/models` | 200 | `{"object":"list","data":[]}` (no gateway-enabled agent) |
| `/api/v1/harness/ping` | 200 | `{"ok":true,"apiKeyId":"<our key id>","agentId":null,"agents":0,"harness":"flue","harnessConfig":null}` |
| `/api/v1/developer/agents` | 200 | one agent: Uhlexa `1cafe1d6-477a-4b5d-a58d-6875ddc405d4`, model `minimax/minimax-m3.1-flash:free`, `active:true`, `last_seen_at 2026-10-21T18:50:33.888Z` |
| `/api/v1/developer/config?agent_id=1cafe...` | 200 | flue harness, `harness_config:null`, `execution_mode:"platform"`, features above, system_prompt "You are the younger sister of Alexa from Amazon" |
| `/api/v1/keys` | 200 | 2 keys, both `scopes:["*"]`, `agentId:null`, no expiry, not revoked: `alexa_keiki_key` (ours) and `Default` (unused) |
| `/api/v1/developer/usage` | 200 | all zeros |
| `/api/v1/developer/trace-exporters` | 200 | `{"exporters":[]}` |
| `/api/v1/developer/secrets` | 200 | `{"secrets":[]}` |
| `/api/v1/traces?limit=5` | 200 | `{"traces":[]}` |
| `/api/v1/agent-channels` | 401 | `{"error":"This Keiki connection is invalid or expired. Reconnect Keiki."}` (OAuth-only) |
| `/api/v1/billing`, `/api/v1/orgs` (earlier 15:14 run) | 404 | `No API operation matches GET ...` |
| `/.well-known/keiki-turn-keys.json` (no auth) | 200 | one Ed25519 JWKS key `kid DN6QA6pKiv1PfnCm` |
| `/api/v1/harness/tunnel.sh` (no auth) | 200 | 10,264-byte vendor script, read as text only |

Not probed on purpose: `GET /api/v1/harness/inbox/next` and `/turns/next` (they claim work), every POST/PUT/PATCH/DELETE, the gateway chat
completion, MCP (OAuth). Vendor `tunnel.sh` proves the harness client contract: `GET /api/v1/harness/ping` for validation, then
`PATCH /api/v1/developer/config` with `{"harness":"external","harness_config":{...existing keys minus lastSeenAt/lastDelivery,"mode":"push","dispatchUrl":"..."}}`.

## Terms (https://onkeiki.com/terms, updated August 18, 2026; privacy same day)

- s5 (you may not): "send unsolicited or unlawful messages, or otherwise violate the rules of a messaging provider or carrier, including
  consent and opt-out requirements"; "impersonate a person or organization, or hide that a message came from an automated agent where
  disclosure is required"; "deploy an agent to give medical, legal, or financial advice in a way that requires a licensed professional".
- s6: you are responsible for what your agents say and do and for "notices, consents, and opt-out handling ... including telecom, email,
  and privacy requirements"; do not use the Service where an unsupervised action "would create serious risk to health, safety, or legal rights".
- s7: messaging/email providers are governed by their own terms. s8: "We do not use Your Content to train foundation models." s11: early
  access/preview/beta "as-is, may change or disappear". s14: indemnity covers "what your agents say or do". s15: suspension if "your use
  creates risk or legal exposure".
- Privacy: "not intended for children, and we do not knowingly collect personal information from anyone under 16"; for conversations
  you are controller, Keiki the processor. A family/kids demo should use fictional or adult consenting participants, label the agent as
  automated, and not message real minors.
- Nothing in terms or docs addresses hackathon/demo use. A demo needs no extra permission, but the consent/opt-out duties stay ours.

## Design implications for Uhlexa

1. **Verify with Ed25519 (no secret) and also support HMAC.** Prefer a signing secret if jobs are durable (point 3).
2. **Public route is the crux.** Push needs `https://<slug>.run.mcp-use.com/keiki/turn` reachable (no redirect). Manufact non-`/mcp` route
   pass-through is still unverified. If it fails, use **inbox mode** (outbound long-poll, no URL, no secret). Inbox needs an always-on process;
   Manufact Free scale-to-zero would stall it (turns just queue), so run the poller where it stays up or keep the server warm.
3. **Ack fast, work async, idempotent.** Return 202 within milliseconds, persist `turnId` (unique) and the payload minus tokens, enqueue to the jobs
   worker. The docs say the turn token should not be logged or persisted; with a signing secret the worker mints a fresh `kts1.` per request
   (5-minute lifetime) so no token needs storing. In inbox mode keep the `turnToken` in memory only; it rotates on reclaim.
4. **Reply path:** `/typing` (optional), `/messages` with a stable `idempotencyKey` per logical reply (e.g. `<turnId>:r<n>`), `/complete`
   always (also `failed` with `error`). Treat `409 turn_closed` as terminal-ok; `502` on `/messages` as unknown-delivery (do not blindly resend).
   Heed `deadline`; default turn budget 10 minutes. Reply content for held-back/Live Test turns is captured, not delivered: use that for
   safe demo rehearsal.
5. **Concurrency model:** one active turn per conversation; bursts merge into one `messages[]`; with `append`, handle `keiki.turn.arrival` and
   `consumedArrivals`; with `interrupt`, handle `keiki.turn.cancelled`. Start with `queue` (simplest) unless the demo needs interruption.
6. **Identity & threads:** key Uhlexa threads on `(keiki_agent_id, conversationId)`; Uhlexa remains system of record. Use Keiki `agentId` to
   resolve which family member's agent was addressed (one Keiki agent per person; multi-agent handler resolves secret by `agentId`, unknown
   agent -> `401 unknown_agent`). Do not assume Keiki gives cross-agent transcript access.
7. **Defensive parsing:** accept unknown `history[].role`, arrival `channel` as string, extra `channel` keys, missing `messageId`, empty
   `instructions`, any `context` shape.
8. **Channels reality for the demo:** free real channels via Keiki are Telegram, Slack, AgentMail email, plus Live Test; iMessage needs a paid line
   (or the startup-program free month); SMS/RCS and voice are not live. **Inkbox must own phone calls and SMS/iMessage/email as direct
   Uhlexa adapters**; Keiki cannot take Inkbox numbers. Proactive notifications (approval waiting) go out through Inkbox.
9. **Mixing paths:** Inkbox -> Uhlexa -> Keiki gateway is possible only if the gateway works for an external agent (unverified); otherwise Uhlexa answers
   Inkbox traffic itself with the same brain. Keep one `Brain.respond(thread, message)` used by both the Keiki turn handler and the Inkbox handler.
10. **Cost:** external mode bills only delivery counters, not model tokens. Startup credit ($500) and 1 free month of iMessage line are the only documented
    offsets; apply early (reply "within a few days").
11. **Admin automation:** one-time `PATCH /api/v1/developer/config` with the existing `*` key can flip Uhlexa to external; write a small script with a
    `--dry-run`, record before/after of `harness`/`harness_config`, keep rollback `{"harness":"flue","harness_config":null}` **(inference for rollback body)**.
12. **Ops hygiene:** secrets in env only (`KEIKI_API_KEY`, `KEIKI_SIGNING_SECRET`, agent-bound harness key); never log `Keiki-Turn-Token`, `ht_`, `kts1.`, `whsec_`.
13. **Python:** no SDK; vendor the ~150 lines (verify HMAC/Ed25519, kts1 mint, turn client with the retry rules in 1.5). Test with the self-generated vectors above plus a
    Live Test turn; there is no vendor test vector.

## Blockers

1. **Dispatch URL reachability on Manufact** (push mode): unverified; fallback = inbox mode (needs a poller host that stays up).
2. **Harness-key semantics:** inbox polling needs a "harness API key"; our org-wide `*` key reports `agents:0` on `harness/ping`. Needs an agent-bound key created in the dashboard (Ken).
3. **Writing the config** (`harness:"external"`) has not been performed or tested (read-only rule); whether an org-wide key may change `harness` is unproven. Dashboard is the safe fallback (Ken).
4. **Real channels:** iMessage requires purchasing a line (price shown only at channel setup; plans "not yet" purchasable); SMS/RCS, phone calls and A2A are early access, not live. No BYO Twilio/Inkbox. Confirm org plan state (sandbox vs paid) in the dashboard before promising any channel.
5. **No API to read Keiki transcripts with an API key**; only traces. Not blocking if Uhlexa stores its own threads.
6. **Unverified gateway-on-external behavior** blocks using the OpenAI gateway as the simulator-to-Keiki path until smoke tested.

## Unknowns (all "not documented" unless noted)

- Dispatch HTTP timeout, retry policy/backoff for failed pushes, response-body handling.
- Contents of `context`; whether `instructions` includes the agent's `system_prompt`; whether Keiki memory is sent; exact keys of the `channel` object per channel; `deadline` format/time zone (example is ISO-8601 UTC).
- Inbox `turn` object exact schema; lease duration; whether an org-wide key can poll; how many pollers per org/agent.
- Success body of `/messages` and `/complete`; `/lease` request body; timeouts of turn routes; scope required for the signing-secret route and for `PATCH /config` harness changes.
- Whether `PATCH /config` accepts `harness`/`harness_config` for org-wide keys (vendor script proves it for agent-bound keys).
- Whether notices, loops, alerts, `ask_agent` peer dispatch, gateway completions and human takeover reach (or bypass) an external harness.
- What "import a line you already have" supports; any BYO-provider path; SMS channel semantics; email thread conversation ids.
- Org plan/credit status and the iMessage line price; rate limits of the REST API; `agents-plugins` npm package absence.
- Dashboard labels/screens for the external-harness setup card (I cannot sign in).
