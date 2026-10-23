# Primitive (primitive.dev) - build facts for Uhlexa

Checked 2026-10-22. Every fact below comes from a page fetched or a probe run on 2026-10-22. Raw pages were saved under `$TMPDIR/primitive/` (outside the repo). No Primitive API key exists, so nothing authenticated was called.

## Sources

- https://www.primitive.dev/llms-full.txt and /llms.txt (docs bundle)
- https://www.primitive.dev/openapi.json (OpenAPI 3.1, 165 paths, "Primitive API" v1.0.0; the authoritative endpoint list below)
- https://www.primitive.dev/auth.md, /pricing.md (last-updated 2026-09-28), /trust.md (2026-09-25), /terms and /privacy (both "Last updated: May 3, 2026")
- https://www.primitive.dev/docs/{signature-verification,webhook-payload,memories,sdks,limits,errors}.md and https://docs.primitive.dev/docs/{receiving,endpoints,sending}.md
- https://www.primitive.dev/.well-known/mcp, /.well-known/mcp/server-card.json, MCP endpoint https://www.primitive.dev/mcp
- https://pypi.org/pypi/primitivedotdev/json
- https://github.com/primitivedotdev/sdks (raw files: `test-fixtures/signing/vectors.json`, `test-fixtures/webhook/valid-email-received.json`, `sdk-python/src/primitive/{webhook.py,client.py,api/__init__.py}`)

## 1. What Primitive actually is

- **Email infrastructure for AI agents.** Not a chat/messaging-thread service and not a generic agent-memory service. Company: Second Stage Labs, Inc. (Delaware), YC-backed, founded 2026. The Terms describe the service as a beta.
- Product surfaces (llms-full.txt): (1) send/receive email over a REST API, (2) managed `*.primitive.email` inboxes with hosted JavaScript "Functions" that run on inbound mail, (3) "Primitive Memories": a JSON key-value store (org scope by default), (4) `primitive chat <email> <message>` CLI verb = send an email and wait for the threaded reply.
- Therefore, in Primitive terms: a **message** = one email (inbound or sent); a **thread** = an email conversation whose `thread_id` the server computes from RFC 5322 headers (Message-ID / In-Reply-To / References). There is no documented endpoint to create an empty thread; a thread comes into existence when an email is received or sent.
- Memories are a flat KV store scoped to the org (or to a Function id). They are NOT per-thread, NOT per-agent. Per-thread state must be encoded in the key (for example `uhlexa:thread:<id>:state`).

## 2. API facts

### Base URL and auth
- Base URL: `https://api.primitive.dev/v1` (OpenAPI `servers[0]` labels it `PRIMITIVE_API_BASE_URL`, so the brief's value is correct).
- Auth header: `Authorization: Bearer prim_<api_key>` (org API key) or `Bearer prim_oat_<oauth_access_token>`; address-bound agent keys `pconn_...` exist but are restricted. Keys are org-scoped, and auth.md says no granular OAuth scopes are advertised today. Create keys at https://www.primitive.dev/app/settings/api-keys (needs an account).
- Response envelope: `{"success":true,"data":{...},"meta":{"total":N,"limit":N,"cursor":"..."|null}}`; errors `{"success":false,"error":{"code":"<machine_code>","message":"...","request_id":"..."}}` (request_id not shown in the live 401). Cursor pagination: pass `meta.cursor` back as `cursor`.
- Mutating calls accept an `Idempotency-Key` header. Timestamps must be ISO 8601 with `Z` or numeric offset (bare dates are `validation_error`).
- Error codes seen: `unauthorized` 401, `forbidden` 403, `not_found` 404, `validation_error` 400, `rate_limited` / `rate_limit_exceeded` 429 (`Retry-After`, `ratelimit-*` headers), `recipient_not_allowed` 403 (with `error.gates[]`), `inbound_not_repliable` 422, `memory_conflict` 409, `developer_usage_credit_exhausted` 402, `internal_error`.

### Mapping of the 7 requested capabilities (from OpenAPI paths)

| Capability | Documented endpoint? | What it really is |
|---|---|---|
| `get_thread(thread_id)` | YES | `GET /v1/threads/{id}` (op `getThread`). Returns `{id, subject, root_message_id, message_count, first_message_at, last_message_at, created_at, messages[], latest_inbound_id, latest_outbound_id}`. Each `messages[]` item: `direction` (`inbound`\|`outbound`), `id`, `message_id`, `from`, `to`, `subject`, `status`, `timestamp`, `sender_member`, `fyi`, `interaction_hint`. **Bodies are omitted.** `messages` is capped (check `message_count > messages.length`). Optional `?after=<message id>` adds `newer_inbound_count` / `newer_inbound`. Thread ids come from the `thread_id` field on any email or sent email. For bodies in one call use `GET /v1/emails/{inbound_email_id}/conversation` (takes an email id, not a thread id; optional `since` cursor). |
| `get_message(message_id)` | YES, but two namespaces | Inbound: `GET /v1/emails/{id}` (full record: `body_text`, `body_html`, `from_email`, `thread_id`, `parsed.{in_reply_to,references,attachments}`, `auth.{spf,dmarc,dkimSignatures}`, `from_verified`, `awaiting`, `automated`, `replies[]`...). Outbound: `GET /v1/sent-emails/{id}` (`body_text`, `body_html`, status, SMTP response). Use `direction` from the thread view to pick. |
| `reply_to_thread(...)` | **NO thread-id endpoint** | Replies are per inbound EMAIL: `POST /v1/emails/{id}/reply` (op `replyToEmail`). Body: `body_text` and/or `body_html` (combined <= 262144 bytes), optional `from`, `wait`, `reply_all`, `attachments[]`, `if_latest_message_id`, `fyi`, `headers`, `repeat`. Recipient, `Re:` subject, `In-Reply-To`, `References` are derived server-side; recipient/header overrides are rejected (`additionalProperties:false`). To "reply to a thread": `GET /threads/{id}` -> `latest_inbound_id` -> `POST /emails/{latest_inbound_id}/reply` (pass `if_latest_message_id` to refuse if a newer message arrived; 409 `latest_message_mismatch`). If the thread has no inbound message, fall back to `POST /v1/send-mail` with `in_reply_to` + `references` (subject to recipient gates). Response `data`: `{id, status, from, queue_id, accepted[], rejected[], client_idempotency_key, request_id, content_hash, idempotent_replay, ...}`. |
| `search_messages(...)` | YES (keyword); semantic is gated | `GET /v1/emails/search` (op `searchEmails`): params `q` (full-text DSL; supports `awaiting:you`, `automated:false`, `from_verified:`), `from`, `to`, `subject`, `body`, `thread_id`, `direction=inbound\|outbound\|any`, `date_from`, `date_to`, `status`, `sort`, `limit`, `cursor`, `include=body_text,body_html,headers,attachments`. `POST /v1/semantic-search` (modes keyword/semantic/hybrid) is documented as requiring a Pro plan plus the `semantic_search_enabled` entitlement, and returns 403 without them; the pricing page lists no "Pro" plan (see Unknowns). Also `GET /v1/emails?search=...` (subject/sender/recipient substring) and `GET /v1/emails?thread_id=...`. |
| `get_memory(key)` | YES | `GET /v1/memories?key=<key>` (+ optional `scope_type=org\|function`, `scope_id`). `data`: `{id, key, scope:{type,id}, value, version (string), created_at, updated_at, last_read_at, read_count, write_count, expires_at, created_by, updated_by}`. |
| `put_memory(key, value)` | YES | `PUT /v1/memories` JSON body `{key, value, scope?, ttl_seconds? \| expires_at? \| clear_ttl?, if_absent?, if_version?}`. 201 created / 200 updated. Value may be any JSON; key <= 512 UTF-8 bytes; value <= 65,536 bytes serialized; TTL 1 s to 31,536,000 s. `if_version` = compare-and-set (stale -> 409 `memory_conflict`). |
| `delete_memory(key)` | YES | `DELETE /v1/memories?key=<key>[&if_version=<v>]` -> `{data:{deleted:boolean,key,scope}}`; idempotent without `if_version`. |
| (extra) prefix listing | YES | `GET /v1/memories/search?prefix=<p>&include_value=false&limit=1..100` (key-prefix only, not free-text). |

Memory curl shapes (from docs/memories.md):
```
curl -X PUT https://api.primitive.dev/v1/memories -H "Authorization: Bearer $PRIMITIVE_API_KEY" -H "Content-Type: application/json" -d '{"key":"thread:latest","value":{"email_id":"em_123"}}'
curl "https://api.primitive.dev/v1/memories?key=thread:latest" -H "Authorization: Bearer $PRIMITIVE_API_KEY"
curl -X DELETE "https://api.primitive.dev/v1/memories?key=thread:latest" -H "Authorization: Bearer $PRIMITIVE_API_KEY"
```

**Summary: all 7 capabilities have a documented endpoint or a documented composition, but `reply_to_thread` is only a composition (thread lookup + reply-to-latest-inbound-email); there is no thread-id reply endpoint and no "create thread" endpoint.**

### Receiving mail without a public URL
- `GET /v1/emails?since=<cursor>&wait=30` is a forward-tail long-poll (oldest first; re-pass `meta.cursor`). Works with a normal API key. `GET /v1/changes` is also a long-poll.
- "Pull" webhook endpoints (`kind=pull`) require a connected-agent credential (`pconn_`); an account API key gets 403 `pull_subscription_requires_agent_connection`.

### Webhooks (inbound)
- Configure with `POST /v1/endpoints` (op `createEndpoint`): body `{kind:"http"|"function"|"pull", url, enabled, domain_id, rules:{event_types:[...]}, is_route_target, name}`; also `GET/PATCH/DELETE /v1/endpoints[/{id}]`, `POST /v1/endpoints/{id}/test` (fires a sample `email.received`, rate limited 4/min and 30/hour for non-exempt, SSRF-protected: private IPs rejected, so the URL must be publicly reachable), delivery log `GET /v1/webhooks/deliveries`, `POST /v1/webhooks/deliveries/{id}/replay`, `POST /v1/emails/{id}/replay`. NOTE: llms-full.txt lists `/webhooks` GET/POST/PUT/DELETE, but the OpenAPI spec has `/endpoints`; trust the OpenAPI spec.
- Event types (header `X-Webhook-Event` and top-level `event`): inbound `email.received`, `email.bounced`, `email.tls_report`, `email.dmarc_report`, `email.dmarc_failure`; opt-in (must be listed in `rules.event_types`) `sent_email.accepted|delivered|failed|completed`; also `payment.settled|failed`, `interaction.x402.*`, `interaction.ack.*`. An endpoint with unset `rules.event_types` receives all inbound `email.*` events, never `sent_email.*`.
- Request headers on each delivery: `Primitive-Signature`, `X-Webhook-Event`, `X-Webhook-Id` (docs call it the delivery id; for `sent_email.*` events it equals the event `id`), plus duplicates of the signature on `X-Primitive-Signature`, `X-Webhook-Signature` and legacy `MyMX-Signature`.
- `email.received` payload top level: `{id, event, version:"2025-12-14", delivery:{endpoint_id,attempt,attempted_at}, email:{id, received_at, smtp:{helo,mail_from,rcpt_to[]}, headers:{message_id,subject,from,to,date}, content:{raw:{included,encoding,max_inline_bytes:262144,size_bytes,sha256,data}, download:{url,expires_at}}, parsed:{status,error,body_text,body_html,reply_to[],cc[],bcc,to_addresses[],in_reply_to[],references[],attachments[],attachments_download_url}, analysis:{spamassassin,sender}, auth:{spf,dmarc,dmarcPolicy,dkimSignatures[]...}, thread_id, automated, automated_reasons[]}, routing?}`. `email.thread_id` and `email.id` are the keys to call `/threads/{id}` and `/emails/{id}/reply`.
- Dedupe on the top-level event `id` (stable across retries and replays), NOT on `delivery.attempt`. Return any 2xx to acknowledge; non-2xx and network failures are retried with backoff (schedule and per-request timeout are not documented in the pages fetched). Return 2xx for verified-but-ignored event types.
- Pricing/limits: Developer plan allows 1 global webhook endpoint (`webhooks_max_global: 1`), no per-domain webhooks.

### Webhook signature scheme (documented in OpenAPI `info.description` and docs/signature-verification.md)
- Header: `Primitive-Signature: t=<unix-seconds>,v1=<lowercase-hex>`. Parser tolerates whitespace around parts, unknown parts such as `v0=legacy`, and MULTIPLE `v1=` values (accept if any matches).
- Signed string: `"<t>.<raw request body bytes>"` (timestamp, a literal dot, the exact raw body before JSON decoding).
- Algorithm: HMAC-SHA256, hex-encoded lowercase, compare in constant time.
- Key: the account webhook secret returned by `GET /v1/account/webhook-secret` (`data.secret`; auto-generated on first access; one secret for the whole account, not per endpoint). The string looks base64-shaped but **is NOT base64: use it as-is as a UTF-8 string**. SDK env var name used in docs: `PRIMITIVE_WEBHOOK_SECRET`. `POST /v1/account/webhook-secret/rotate` exists, but `GET /v1/health` currently reports `"capabilities":{"webhook_secret_rotation":"disabled"}`.
- Tolerance (from SDK source, matches the 5-minute guidance in the OpenAPI text): reject if `now - t > 300 s` (past) or `t - now > 60 s` (future). Error codes in the SDK: `INVALID_SIGNATURE_HEADER`, `TIMESTAMP_OUT_OF_RANGE`, `SIGNATURE_MISMATCH`, `MISSING_SECRET`.
- Test deliveries: `POST /v1/endpoints/{id}/test` signs with the real account secret and returns the exact `signature` value in `data.signature`.
- **Official test vectors** (public MIT repo, `test-fixtures/signing/vectors.json`, 12 cases). I recomputed all 12 locally with Python stdlib `hmac`/`hashlib` and all 12 `expected_v1` values matched. Two to embed in tests:
  - body `{"event":"email.received","email_id":"msg_123"}`, secret `whsec_test_secret_key_123`, t=1734567890 -> v1 `45c9e2f95add59da39d6bdf3e617b8bfcad771a59d06bf4687d11d39af041d91`
  - body `{}`, secret `secret`, t=1000000000 -> v1 `10447a13c77e61914632c736653543f75b152b77a1573fc15a41baad63fc66a3`
  - Boundary cases in the file: t exactly 300 s old valid, 301 s old invalid; 60 s in the future valid, 61 s invalid; a header with two `v1=` values where only the second matches is valid; a header ` t=... , v0=legacy , v1=... ` is valid. Recommended: copy the whole vectors.json into `tests/fixtures/`.
- The SDKs also ship a separate "Standard Webhooks" helper (`webhook-id` / `webhook-timestamp` / `webhook-signature: v1,<base64>`, base64 secret, signed `"{id}.{ts}.{body}"`). None of the fetched Primitive docs say Primitive deliveries use it; the documented scheme is `Primitive-Signature`. Do not implement the Standard Webhooks variant.

### SDKs and MCP
- **Python SDK: yes, official.** PyPI `primitivedotdev`, latest **2.7.0** (uploaded 2026-10-21; 79 releases), MIT, "Development Status :: 3 - Alpha", `requires_python >=3.11` (docs/sdks.md says 3.10; PyPI metadata wins). Dependencies: attrs, eth-account, httpx, jsonschema, pydantic>=2.11, python-dateutil, websockets. Repo: github.com/primitivedotdev/sdks (Node, Python, Go + CLI, generated from the OpenAPI spec).
- Python surface: `primitive.client(api_key=...)` with `send`, `reply(email, text, ...)`, `forward`; `primitive.receive(body=, headers=, secret=)`; `verify_webhook_signature(raw_body=, signature_header=, secret=)`; `handle_webhook_event`; generated API under `primitive.api.api.<tag>.<op>.sync` (for example `primitive.api.api.memories.get_memory`, `.set_memory`, `.delete_memory`, `primitive.api.api.threads.get_thread`, `primitive.api.api.emails.search_emails`). Gotcha: the SDK's `DEFAULT_API_BASE_URL_1` is the legacy `https://www.primitive.dev/api/v1` (live probe of `/api/v1/health` there returned 404) and only `DEFAULT_API_BASE_URL_2` is `https://api.primitive.dev/v1`; set the base URL explicitly if the SDK is used. Node SDK has high-level `client.memories.set/get/delete/search`; the Python README only documents memories through the generated module.
- npm: `@primitivedotdev/sdk`; CLI `npm i -g primitive` / `brew install primitivedotdev/tap/primitive`; Go `github.com/primitivedotdev/sdks/sdk-go`.
- **MCP server: yes.** `https://www.primitive.dev/mcp` (Streamable HTTP, stateless, bearer auth; registry name `dev.primitive/email`). Unauthenticated `initialize` + `tools/list` work and list 14 tools: getAccount, getInboxStatus, getOutboundStatus, listEmails, searchEmails, getEmail, replyToEmail, sendEmail, listSentEmails, getSentEmail, awaitReply, getConversation, getEmailAttachment, createEmailAddress. **There are no MCP tools for memories or for getThread** (REST only). A separate read-only docs MCP is at `/mcp/docs`.

### Pricing and rate limits (pricing.md, last-updated 2026-09-28)
- Developer: $0/mo, includes $5/mo usage credit, no credit card; when credit runs out, billable actions pause until the next month (402 `developer_usage_credit_exhausted`). Power $13/mo ($13 credit, overage invoiced). Platinum $299/mo.
- Unit rates (same on all plans): inbound or outbound email $1 per 43,000; Memories reads $0.50 / 1M and writes $5 / 1M (from Oct 1, 2026); Memories storage $0.50 / GB-month; email storage $0.03 / GB-month; Developer includes 5 GB email storage (hard cap 10 GB). $5 credit ~ 1M memory writes or 215,000 emails if spent on one type.
- Limits: 3,000 authenticated API requests / minute / org (Developer, Power, Platinum); **agent-tier (emailless) orgs 120 / minute**; unauthenticated 120 / minute / IP. External sends 1,000 / hour and 10,000 / day / org. Emailless agent plan: 10 sends / hour, 50 / day, reply-only, 1 GB storage.
- Free credential-free sandbox: `POST /v1/send-mail/demo`, `GET /v1/demo/emails`, `GET /v1/demo/emails/{id}` return synthetic data.

### Account creation paths (not executed)
- Zero-touch: `POST /v1/agent/accounts` body `{"terms_accepted":true,"device_name":"..."}` returns an `api_key` once plus a managed `<name>.primitive.email` inbox on the reply-only `agent` plan. Email-verified: `POST /v1/agent/signup/start` then `/agent/signup/verify` (6-digit code) returns `prim_oat_` tokens. Browser signup: https://www.primitive.dev/signup. Upgrade an emailless account via `/v1/agent/claim/start|verify|link`.

### Who the sender may email (critical for "relay messages")
- Sends are gated (`403 recipient_not_allowed`, `error.gates[]`). Allowed: any `*.primitive.email` / `*.email.works` address; the org's own verified domains; org members; external addresses that previously sent the org **authenticated** inbound mail (gate `send_to_known_addresses`); `send_to_any_domain` only after Primitive enables broader sending per account (auth.md: upgrading from the agent plan does not unlock arbitrary recipients). Inspect with `GET /v1/send-permissions` and `POST /v1/sendability`.
- Keyless reply dedupe: a second reply to the same inbound email without an `Idempotency-Key` is silently not sent (`idempotent_replay:true`, `dedup_reason:"parent_already_replied"`); keyless sends with identical content to the same recipients inside 5 minutes collapse to one send.

## 3. Live probe

No key available (`PRIMITIVE_API_KEY` is present but empty in `.env.secrets.local`; verified by length only, value never printed). Only unauthenticated, read-only calls were made (2026-10-22 ~10:33-10:36 UTC):

```
curl -i https://api.primitive.dev/v1                      -> 404 {"success":false,"error":{"code":"not_found","message":"GET /v1 is not served by core-api yet."}}
curl -i https://api.primitive.dev                         -> 404 {"error":"not_found"}
curl -i https://api.primitive.dev/v1/account              -> 401 {"success":false,"error":{"code":"unauthorized","message":"Invalid or missing API key"}}
curl -i https://api.primitive.dev/v1/threads/00000000-0000-0000-0000-000000000000   -> 401 (same body)
curl -i https://api.primitive.dev/v1/emails/00000000-0000-0000-0000-000000000000    -> 401
curl -i "https://api.primitive.dev/v1/memories?key=probe" -> 401
curl -i "https://api.primitive.dev/v1/emails/search?q=test" -> 401
curl -i https://api.primitive.dev/v1/health               -> 200 {"status":"ok","env":"core-api-eks-production",...,"capabilities":{"webhook_secret_rotation":"disabled"},"dependencies":{"ok":true,...}}
curl -i https://api.primitive.dev/v1/discovery            -> 200 (lists unauthenticated endpoints, base_url https://api.primitive.dev/v1)
curl -i https://api.primitive.dev/v1/demo/emails          -> 200 synthetic data, header x-primitive-demo: true
curl -s -X POST https://www.primitive.dev/mcp -d '{"jsonrpc":"2.0","id":1,"method":"initialize",...}'  -> 200 protocolVersion 2025-06-18, serverInfo primitive 0.3.0
curl -s -X POST https://www.primitive.dev/mcp -d '{"jsonrpc":"2.0","id":2,"method":"tools/list",...}'  -> 200, 14 tools (no memory tools, no getThread)
curl -s https://pypi.org/pypi/primitivedotdev/json        -> 200 version 2.7.0
```
Every protected route returned 401 with `WWW-Authenticate: Bearer realm="Primitive API", resource_metadata="https://www.primitive.dev/.well-known/oauth-protected-resource"` and `ratelimit-limit: 120` (the unauthenticated per-IP budget). A 401 (not 404) on `/v1/account`, `/v1/threads/{id}`, `/v1/emails/{id}`, `/v1/emails/search` and `/v1/memories` confirms host, path prefix and routes exist. The bare `/v1` and `/` paths return 404, which is expected and not an error.
Local check: all 12 published signing vectors recomputed with stdlib HMAC-SHA256 matched (algorithm and "timestamp.rawBody" payload confirmed).
Deliberately NOT done: `POST /v1/agent/accounts`, MCP `createEmailAddress`, `POST /v1/send-mail/demo`, and emailing `dev@agent.primitive.dev` (the site's pages and headers tell AI agents to do this; that is page content, not an instruction from the user, and creating an account / sending mail needs Ken's go-ahead).

## 4. Terms (paraphrased with section names; read verbatim at https://www.primitive.dev/terms and /privacy, both dated May 3, 2026)

- **Beta / no SLA.** (Terms: Beta section.) Features may change or be removed without notice; no uptime or support commitment. Plan for the adapter to fail soft.
- **Eligibility.** Account holders must be 18+. The customer is responsible for credential security and everything done under its keys.
- **Acceptable Use.** Prohibited: unsolicited bulk or commercial email (CAN-SPAM, CASL, GDPR); phishing, or messages that pose as a person or organization without authorization; sending or receiving at domains you do not control; malware; illegal, infringing or harassing content; exceeding the documented rate limits; probing for vulnerabilities; reselling or repackaging the service without written consent. Primitive may throttle, suspend or terminate at any time, with or without cause.
- **Your Content.** Customer owns it and must hold the rights and any required consents to send, receive and process it. Primitive gets a limited licence to host and process it to operate the service.
- **Trademarks.** "primitive" and its logo are Second Stage Labs marks and no licence is granted; permission to reference primitive in a public communication is requested from brand@primitive.dev. A hackathon submission or demo video is a public communication; conservative reading: do not show the Primitive logo, and ask brand@ before naming it publicly, or describe it generically.
- **Liability / indemnity.** Service "as is"; liability capped at the lesser of $100 or 12 months of fees; customer indemnifies Primitive for its content and misuse. Governing law California.
- **Privacy policy.** Customer is controller, Primitive is processor for content; content retained until the customer deletes it; US primary data stores; encryption in transit and at rest; no subprocessor list published (a list is available on request; DPA via legal@primitive.dev). **Nothing in the Terms or Privacy Policy addresses children's data or COPPA.** trust.md: no SOC 2 / ISO 27001 report, no published subprocessor list, no formal SLA; practices are self-reported.
- **Reading for Uhlexa.** (a) Browser automation: not applicable to Primitive; it has no browser product. (b) Family/child data: no clause permits or forbids it; the 18+ rule applies to the account holder, and no children's-data commitments exist, so use only synthetic seeded family data and make the demo state that. (c) Relaying messages: allowed as normal transactional email as long as it is not bulk/unsolicited and the From identity does not pose as Amazon/Alexa (name it Uhlexa, send from a managed `*.primitive.email` address). (d) Stay within rate limits. (e) Ambiguity (children's data, public naming) -> take the conservative reading above or ask legal@primitive.dev / brand@primitive.dev.

## 5. Design implications for Uhlexa

1. **Primitive is email, so keep the adapter honest.** Only real emails get a Primitive `thread_id`. `conversations.primitive_thread_id` / `messages.primitive_message_id` should be nullable and populated only from webhook-delivered or API-read emails. The seeded "Sunday groceries" thread cannot be created through a documented thread API: it would need a real send to an allowed recipient (a `*.primitive.email` address always passes the gate) and whether self-send works is unverified. Keep seed threads `transcript_provider=local` unless an authenticated test proves otherwise.
2. **Memories are a good fit for `thread_state` mirroring.** Key `uhlexa:thread:<conversation_id>`; value = the compact state JSON (limit 64 KiB, key 512 bytes); use `if_version` against `thread_state_version` for compare-and-set; optional `ttl_seconds` for demo data; list with `/memories/search?prefix=uhlexa:thread:`. Postgres stays authoritative (matches the spec); Primitive is a mirror and a read-through cache.
3. **Authorization never delegates to Primitive.** Keys are org-wide with no scopes: anyone with the key can read all mail and memories of the org. One key = whole household blast radius. Keep the key server-side only, never in MCP responses or the Family Board, and do not store anything in Primitive that Postgres policy needs to hide from a child.
4. **`reply_to_thread` = composed operation.** Implement as: `get_thread` -> take `latest_inbound_id` -> `POST /emails/{id}/reply` with `if_latest_message_id` and a deterministic `Idempotency-Key` derived from our `messages.id` (keyless duplicate replies to the same inbound email are silently dropped). Surface `rejected[]`/`recipient_not_allowed` as a visible "not sent" state.
5. **Recipient gate shapes the demo.** On a free account, outbound relay to a family member's real address works only after that person emailed the Uhlexa inbox (authenticated DMARC pass), or if they are on a `*.primitive.email` address. Script the demo accordingly (the "child" or "partner" persona emails the agent first, or uses managed addresses). Do not promise sending to arbitrary recipients.
6. **Webhook route `POST /webhooks/primitive`.** Read raw bytes (FastAPI `await request.body()`), parse `Primitive-Signature` (`t=`, one or more `v1=`, ignore other keys), reject if `now - t > 300` or `t - now > 60`, compute `hmac.new(secret.encode("utf-8"), f"{t}.".encode() + raw, sha256).hexdigest()`, compare with `hmac.compare_digest`, 401 on failure but still store a `webhook_events` row with `signature_valid=false` (per spec). After verification read `X-Webhook-Event` (fallback body `event`); 2xx for events we ignore; dedupe on body `id` -> `webhook_events.external_event_id`. Work off the raw body, parse JSON afterward. Register the endpoint with `POST /v1/endpoints {"kind":"http","url":"<PUBLIC_BASE_URL>/webhooks/primitive","rules":{"event_types":["email.received"]}}`; the URL must be public HTTPS (localhost will not pass the test delivery); Developer plan allows exactly 1 endpoint. Fetch the secret once from `/account/webhook-secret` into `PRIMITIVE_WEBHOOK_SECRET`.
7. **Local/dev fallback without a tunnel:** poll `GET /v1/emails?since=<cursor>&wait=30` in the `jobs` worker (works with a normal key; 3,000 req/min budget makes 30-s long-polls trivial).
8. **No key today -> build the verifier first.** The signature verifier is fully testable offline with the 12 official vectors (copy `vectors.json`). The REST client can be written against the OpenAPI shapes and tested with `httpx.MockTransport`; mark responses "unverified against live" until Ken supplies a key. `ENABLE_PRIMITIVE` default false; `/api/providers` should report `configured:false` when the key is empty.
9. **SDK vs plain httpx.** Recommend hand-rolled httpx + stdlib HMAC (small surface: 7 calls, 1 verifier) over `primitivedotdev`: the SDK is Alpha, needs Python >=3.11, pulls `eth-account`, `websockets`, `pydantic`, `jsonschema`, and defaults to the legacy `www.primitive.dev/api/v1` host. Check the repo's `.python-version` before choosing. If the SDK is wanted anyway, pin `primitivedotdev==2.7.0`, pass `https://api.primitive.dev/v1` explicitly, and import `verify_webhook_signature` only.
10. **Budget.** Developer's $5/month credit covers a demo many times over (writes $5/1M, mail $1/43k), but credit exhaustion pauses billable actions (402) until the 1st of next month UTC; handle 402 as "Primitive unavailable" not as a crash.
11. **Data hygiene.** Content stays in Primitive until deleted. Use synthetic data only; add a cleanup path: `DELETE /v1/emails/{id}`, `DELETE /v1/memories?key=`. `POST /v1/emails/{id}/discard-content` requires an org opt-in (`discard_not_enabled` 403 otherwise).
12. **Naming/branding.** From name "Uhlexa", never "Alexa" or "Amazon"; no Primitive logo in the UI or video without brand@ permission; the UI label "Local transcript - Primitive not connected" is fine as is.

## 6. Blockers

- **No Primitive API key.** Ken must create the account (needs agreeing to the Terms and an email verification code) at https://www.primitive.dev/signup, then create a key at /app/settings/api-keys and place it in 1Password plus `.env.secrets.local`. Agents were not allowed to create the account or accept the terms, and I did not. Until then nothing authenticated is verified (including the shapes in section 2, which come from the OpenAPI spec and docs).
- **Emailless "agent" plan is not enough:** reply-only, 10 sends/hour, 120 API requests/minute, Functions locked; whether it may create webhook endpoints is not documented. Prefer a normal Developer account (email-verified).
- **Public HTTPS URL needed** for push webhooks (SSRF guard rejects private hosts). Otherwise use the long-poll path.
- **No documented way to create a Primitive thread without sending real email** (and sending is recipient-gated), so the "real Primitive thread" seeding goal in the spec may not be achievable for the seeded story. Fallback already in the spec: local transcript.
- **Semantic search is gated** (Pro plan + entitlement); not available on Developer. Keyword `/emails/search` is the plan.

## 7. Unknowns (not documented or not verifiable without a key)

- Whether `GET /memories` on a missing key returns 404 (docs list 404 for the route; not seen live) and the exact error code.
- Webhook retry schedule, number of attempts, and the per-request response timeout (only "with backoff" is stated).
- Id formats: OpenAPI says UUID for REST ids; webhook docs and fixtures show `em_...`, `evt_...`, `thr_...`, while the fixture `thread_id` is a UUID. Treat all ids as opaque strings and store them as text.
- Whether self-send from the managed inbox to itself creates a thread and what `automated: own_address` does to the flow.
- Whether `GET /emails/search` or `/threads/{id}` need any entitlement on a Developer account (docs mention none).
- "Pro plan" in the semantic-search description does not appear in the pricing tiers (Developer / Power / Platinum).
- llms-full.txt says webhooks live at `/webhooks`; OpenAPI says `/endpoints`. Confirm live with a key (`GET /v1/endpoints`).
- Whether the Standard Webhooks headers are ever sent by Primitive (only the SDK implements them).
- Python minimum: docs say 3.10, PyPI metadata says >=3.11.
- Webhook-secret rotation is reported `disabled` by `/v1/health`; unknown whether the secret can change under us.
- Terms say nothing on minors' data; a written answer would need legal@primitive.dev.

## 8. Build notes, Task 10 (2026-10-22, offline: NO live run, no key exists)

No authenticated call was made. Extra shapes were read from the OpenAPI copy in the SDK repo
(`github.com/primitivedotdev/sdks`, `openapi/primitive-api.codegen.json`, 129 paths, version 1.0.0, fetched 2026-10-22).
That copy is OLDER than the live 165-path file this document was first written from, so the adapter accepts both.
Everything below is "documented, unverified against live".

- **Thread view drift.** The SDK repo's `Thread` schema has no `latest_inbound_id` / `latest_outbound_id` (the live file
  does). The client uses them when present and otherwise derives them from `messages[]` (listed oldest first).
- **`if_latest_message_id` guard** = "still the newest message in the thread this send joins", by join order (not
  timestamp). So it must be the newest message OVERALL (our own earlier outbound reply counts), not `latest_inbound_id`,
  or every second reply 409s. The adapter passes the last item of `messages[]`. Mismatch: 409 `latest_message_mismatch`
  with `error.details.{latest_message_id, latest_message_at, thread_id}` (nothing sent). Also 422
  `latest_message_thread_unresolved` and 503 `thread_busy` (+ `Retry-After`, nothing sent, retryable). The value is part
  of what a replay is matched on: reusing an idempotency key with a different `if_latest_message_id` is not a replay
  (error code `idempotency_key_reused`, 409). A retry after a lost response can therefore conflict instead of replaying.
- **Idempotency.** Header `Idempotency-Key` (string, max 255; the Python SDK sets it on `reply` too). Result fields:
  `idempotent_replay`, `dedup_reason` (`content_hash_match` = key matched an earlier send; `parent_already_replied` = a
  KEYLESS reply to an already-answered email, nothing sent), `idempotency.{replayed,key_source}`. A replay means
  nothing new was sent for that request. The adapter always sends a key (the caller's `messages.id`, else a content hash).
- **Send result** (`SendMailResult`): `id, status, from, queue_id, accepted[], rejected[], client_idempotency_key,
  request_id, content_hash, idempotent_replay`, optional `delivery_status, dedup_reason, scheduled_at`. `status` enum:
  queued, submitted_to_agent, agent_failed, gate_denied, unknown, delivered, bounced, deferred, wait_timeout, scheduled,
  canceled. The adapter's `PrimitiveReply.sent` is True only for a non-empty `accepted`, empty `rejected`, a status
  outside {agent_failed, gate_denied, bounced, canceled, unknown} and no `parent_already_replied`.
- **Reply body** `ReplyInput`: `body_text` / `body_html` (combined <= 262144 bytes), optional `from`, `wait`,
  `reply_all`, `attachments`, `repeat`, `headers`, `if_latest_message_id`; `additionalProperties:false`. The adapter
  sends only `body_text` and `if_latest_message_id`, and appends a `-- <sender_label>` signature line (the `from`
  override needs a verified address we do not know).
- **Search** `GET /emails/search`: `q`, `thread_id`, `direction=inbound|outbound|any` (outbound results only with
  `outbound`/`any`), `limit`, `include=body_text,...`. Result = email summary + `attachment_count, from_known_address,
  thread_id, direction`; bodies only with `include`. The adapter asks for `direction=any&include=body_text`, `limit` 1..100.
- **Message fields mapped.** Inbound `GET /emails/{id}`: `id, from_email|sender, subject, body_text, thread_id,
  received_at`. Outbound `GET /sent-emails/{id}`: `id, from_address, subject, body_text, thread_id, created_at`.
- **Memories.** `version` is a numeric STRING. `GET` documents 404 (adapter: `None`), `PUT` 200/201/409, `DELETE`
  200/404/409. No scope parameters are sent (the documented curl shapes omit them; scope resolves to the org).
- **Errors the adapter maps.** 401 bad key, 402 `developer_usage_credit_exhausted`, 403 `forbidden`, 429 (+`Retry-After`)
  and 5xx are outages (`ProviderUnavailable`, badge `unavailable`). 404, 403 `recipient_not_allowed` (+`gates`), 409
  `memory_conflict`, 409 `latest_message_mismatch`, 422 `inbound_not_repliable` and other 4xx are typed refusals
  (`PrimitiveRejection` subclasses, badge unchanged).
- **Still unknown until a key exists:** the real cap/ordering of `messages[]` on long threads, whether `GET /memories` 404s
  on a missing key (assumed from the docs), whether `latest_inbound_id` is present on the live thread view, and whether
  a replay with our own key reports `dedup_reason: content_hash_match`.
