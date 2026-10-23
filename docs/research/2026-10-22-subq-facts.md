# SubQ (Subquadratic) Decisions API / `subq-snap-preview` - ground truth for Uhlexa

Checked 2026-10-22. Everything below comes from pages fetched today or a live probe run today. "Not documented" means I searched the current docs and the page text and did not find it. Raw pages saved under `$TMPDIR/subq/` (session temp, not in repo).

## Sources (all fetched 2026-10-22)

- https://docs.subq.ai/llms.txt (index; every doc page also exists as Markdown at `https://docs.subq.ai/markdown/<path>.md`)
- https://docs.subq.ai/llms-full.txt (all pages in one Markdown file; used as the primary text)
- https://docs.subq.ai/endpoints/decisions-api/ (Decisions API)
- https://docs.subq.ai/endpoints/models-api/ (List models)
- https://docs.subq.ai/using-the-api/ , /authentication/ , /models/ , /data-retention/ , /support/
- https://subq.ai/terms (Terms of Service, Effective 2026-10-07, Last Updated 2026-10-16)
- https://subq.ai/usage-policy (Usage Policy, Effective 2026-10-12; `/acceptable-use-policy` redirects here)
- https://subq.ai/data-processing-agreement (DPA, Effective 2026-10-16)
- https://subq.ai/privacy-policy (Effective 2026-02-27, Last Updated 2026-10-16)
- Third party, secondhand, used only as a cross-check: https://docs.openclaw.ai/plugins/subq.md (OpenClaw's SubQ adapter docs, checked by them 2026-10-13)

## API facts

Your 2026-10-17 secondhand notes are CONFIRMED on every point they made, with these refinements (marked NEW).

### Basics
- Base URL: `https://api.subq.ai/v1`. Auth: `Authorization: Bearer <key>`; `Content-Type: application/json` on POST. Keys come from console.subq.ai, are shown once at creation, and must not live in source control.
- Endpoint: `POST /v1/systemone` (Decisions API, "Public preview"). Not OpenAI-compatible; it has its own typed contract. Model id `subq-snap-preview` (case-sensitive). Described as a "System One" model for routing, guardrails, triage.
- `GET /v1/models` lists the model ids your key can use (OpenAI-style list: `object`, `data[]` with `id`, `name`, `object`, `created`, `owned_by`).
- Pricing (docs Models page): input $0.05 per 1M tokens, output free. Response `usage.output_tokens` is always 0. Only input is billed.
- NEW: modalities are text in/out plus image input only. Video unsupported (sample up to 4 frames as images).

### Request body (`/v1/systemone`)
- Required: `model`, `state`, `questions`.
- `state`: "the text, object, or array to evaluate". NEW (verified live): a JSON object is accepted as `state`. `null` state is not in the documented contract (OpenClaw also says this).
- `questions`: object of 1-16 named questions. Answer keys equal the question names; names up to 100 chars. Each question has `type` (`noul` | `choice` | `score`), `instructions`, and type-specific `criteria`.
  - `noul` (yes/no gate): optional `criteria: {"true": "...", "false": "..."}`. Answer: `{"type":"noul","noul":<probability of yes 0..1>}`.
  - `choice`: `criteria` is an object `{option_key: description_or_null}`, 2 to 255 options. NEW and answers your question: choice options DO accept per-option descriptions, and a description may be `null`. Answer: `{"type":"choice","choice":<one option key>,"probabilities":{<every option key>: p},"confidence":c}`.
  - `score`: `criteria` is an ordered array of 2-10 level labels (strings; docs also say criteria may contain strings, objects or arrays). Answer: `score` (probability-weighted, can fall between levels), `legend` (index -> label), `probabilities`, `confidence`.
- Optional `images`: up to 4 items `{ "mediaType": "image/png|image/jpeg|image/webp", "base64": "<raw base64, no data: prefix>" }`; 4 MiB decoded each, 8 MiB decoded total; request body up to 13 MiB. URLs and `data:` URLs fail.
- Size limit: the whole request must fit the 384K-token context window (393,216 tokens). Over the limit, or "close to it", returns `400`. That is the only documented size limit for `state`; no per-field byte cap is documented. Images cost roughly 64-256 tokens each.
- Docs guidance to keep in mind: one judgment per question; related questions about the same `state` go in ONE request (state is sent once); trim `state` to what the decision needs; set thresholds from your own labeled data; route low `confidence` / a noul near 0.5 / costly mistakes to a person; retry `429` and `5xx` with backoff; fix `400`/`422` before resending.

### Response shape (live, 2026-10-22; see Live probe)
```json
{"model":"subq-snap-preview",
 "answers":{"thread_routing":{"type":"choice","choice":"continue_existing_thread",
   "probabilities":{"continue_existing_thread":0.9991532564163208,"new_thread":0.0008467980660498142},
   "confidence":0.9983065128326416}},
 "usage":{"input_tokens":166,"output_tokens":0}}
```

### Errors (docs + live-confirmed 422)
- Shape: `{"detail":{"error_type":"<string>","message":"<string>"}}`. Observed `error_type` value: `validation_error` (also the documented one for bad images/criteria).
- `400` input over token limit, or a request the model rejects. `401` missing/invalid credentials. `402` insufficient credits. `403` key cannot use the requested model. `422` invalid request (incl. bad/oversized image) or a model that is not a decision model. `429` rate limit exceeded. `5xx` server error; `503` = temporarily unavailable.
- Live 422 sample (choice with 1 option): `{"detail":{"error_type":"validation_error","message":"questions/thread_routing/type: must be equal to constant; questions/thread_routing/criteria: must have at least 2 options; ... must match exactly one schema in oneOf"}}`. Note the message can echo question names (and likely request fragments), so do not log it blindly if state is sensitive.
- Docs name no other `error_type` values.

### Limits, timeouts, determinism
- Rate limits: NOT documented (no numbers, no tiers, no `Retry-After` or `X-RateLimit-*` headers documented). None of those headers appeared on the 200 or 422 responses I saw. `429` exists as a status.
- Timeouts: NOT documented. Docs' own Python example uses a 60 s client timeout. Observed latency today: GET /models 0.34 s, tiny /systemone 0.37 s, 422 0.27 s (single samples, includes TLS).
- Determinism / temperature / seed / top_p on `/systemone`: NOT documented; no such parameter appears in the Decisions API request spec. The docs state that outputs can vary between requests. Treat results as non-deterministic.
- Idempotency keys / request ids: not documented, none seen in headers.
- `confidence`: documented as returned for choice and score but its formula is NOT documented. Observation from ONE live sample: it equals top probability minus second probability (0.99915 - 0.00085 = 0.99831). The docs' own score example does not match that rule (0.87 - 0.13 = 0.74, but the example shows 0.79), so do not rely on it. OpenClaw's integration notes call it vendor-specific and not the probability of being correct. Docs' sample code uses `confidence >= 0.6` for a choice cutoff.
- Model access: my key's `/v1/models` returned ONLY `subq-snap-preview`. `subq-preview` (chat, early access) is per-account and not enabled on this key (llms.txt: other keys get `403`).

## Live probe

All with the key loaded from `.env.secrets.local` into `$SUBQ_API_KEY` (never printed). 3 calls total.

1. `curl -sS -m 30 --request GET --url https://api.subq.ai/v1/models --header "Authorization: Bearer $SUBQ_API_KEY"` -> `200`, body `{"object":"list","data":[{"id":"subq-snap-preview","name":"SubQ Snap Preview","object":"model","created":0,"owned_by":"subquadratic"}]}`. HTTP/2, security headers only, no rate-limit headers. Key is valid and funded enough to return 200.
2. `curl -sS -m 30 --request POST --url https://api.subq.ai/v1/systemone --header "Authorization: Bearer $SUBQ_API_KEY" --header 'Content-Type: application/json' --data @req2.json` with this body:
   ```json
   {"model":"subq-snap-preview",
    "state":{"new_request":"add milk and eggs to Sunday groceries",
             "open_threads":[{"thread_id":"t_17","title":"Sunday groceries","last_message":"add bread to the list"}]},
    "questions":{"thread_routing":{"type":"choice",
      "instructions":"Should the new_request be appended to an open thread, or does it start a new thread?",
      "criteria":{"continue_existing_thread":"The request is about the same topic or list as an open thread",
                  "new_thread":"The request is about a different topic than every open thread"}}}}
   ```
   -> `200` in 0.37 s, the response shown above: picked `continue_existing_thread`, p=0.9992, confidence 0.9983, `input_tokens` 166 (about $0.0000083). Dict `state` and per-option descriptions both work.
3. Negative-path call (deliberately invalid, 1-option choice; 422 is a validation reject, no inference): -> `422` with the `validation_error` body quoted under Errors. I used the third allowed call here rather than a second success call, so determinism/repeatability was NOT tested.

## Mapping Uhlexa's adapter to the API

`decide(question_id: str, state: dict, choices: dict[str, str]) -> DecisionResult` maps to ONE request:

```json
{"model":"subq-snap-preview",
 "state": <state dict, unchanged>,
 "questions": { "<question_id>": {"type":"choice",
                                  "instructions": <looked up by question_id>,
                                  "criteria": <choices dict: option_key -> description>} }}
```
- Gap in the signature: there is no `instructions` argument. Keep a registry `INSTRUCTIONS[question_id]` in the adapter (or add a parameter). Without instructions the question is not well-formed per the docs ("Each uses `instructions`"); I did not test omitting it.
- `choices` keys become the option ids returned in `answers[question_id].choice`; values are descriptions (`None` allowed). Require 2 <= len(choices) <= 255 before sending.
- Use `question_id` <= 100 chars. Batch related questions for one `state` into one request only if Uhlexa later needs it; the adapter signature is single-question.

### Detecting malformed output (fail closed)
Treat the result as invalid, and return the safe default or send to a human, if ANY of these hold. This validator was run today against the real 200 body (accepted) and against the real 422 body plus 8 mutated bodies (all rejected):

```python
import math
class Malformed(Exception): pass

def validate_choice(body, question_id, choices, *, model="subq-snap-preview", min_conf=0.6):
    if not isinstance(body, dict): raise Malformed("body not object")
    if "detail" in body: raise Malformed(f"error body: {body['detail']}")
    if body.get("model") != model: raise Malformed("model mismatch")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != {question_id}: raise Malformed("answers keys != requested")
    a = answers[question_id]
    if not isinstance(a, dict) or a.get("type") != "choice": raise Malformed("type != choice")
    ch, probs, conf = a.get("choice"), a.get("probabilities"), a.get("confidence")
    if not isinstance(ch, str) or ch not in choices: raise Malformed("choice not in requested options")
    if not isinstance(probs, dict) or set(probs) != set(choices): raise Malformed("probabilities keys != options")
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    if not all(num(p) and 0 <= p <= 1 for p in probs.values()): raise Malformed("bad probability")
    if abs(sum(probs.values()) - 1) > 0.01: raise Malformed("probabilities do not sum to 1")
    if probs[ch] < max(probs.values()): raise Malformed("choice is not argmax")
    if not num(conf) or not 0 <= conf <= 1: raise Malformed("bad confidence")
    if conf < min_conf: raise Malformed("low confidence")   # uncertain -> human / safe default
    return ch, conf, probs
```
Plus transport rules: non-2xx, non-JSON body, timeout, or connection error -> fail closed. By status: `429`/`5xx`/timeout -> bounded retry with backoff (docs recommend), then fail closed; `400`/`422` -> do not retry (a code bug); `401`/`402`/`403` -> do not retry, surface as config error. The 0.6 cutoff is the docs' sample value, not a tuned one; the docs say to tune thresholds on labeled data. The ~1e-7 probability-sum error seen live is well inside the 0.01 tolerance.

## Terms (paraphrased with section refs; verbatim text at the URLs above)

Versions read: ToS eff. 2026-10-07 (upd. 2026-10-16); Usage Policy (UP) eff. 2026-10-12; DPA eff. 2026-10-16; Privacy Policy upd. 2026-10-16. Only one short verbatim quote is used here; everything else is paraphrase so verify exact wording before relying on it.

- Who may use it (ToS preamble, section 1): the Services are for business/commercial use only, not personal, household or consumer use; account holder must be 18+; Authorized Users must be adults. Reading: fine for TAT Inc's key; the key stays on TAT's server.
- Automating / abuse (ToS 2; UP 2.3, 2.5, 3.1, 6.3): no circumventing rate limits, no extracting output programmatically in violation of rate limits or docs, no excessive-traffic bots, no multiple accounts to dodge limits, no resale/sublicensing of access. Reading: a server-side adapter making a few calls is fine; do not hammer it, do not rotate keys/accounts to escape a 429, and do not expose the key or a proxy to the public.
- Keys (UP 10.3): a key may not be published in a public repo or embedded in a client-side app. Reading: server-side only; the Family Board/simulator front end must never see it.
- Output use (ToS 2; UP 2.4): no using Output to train, fine-tune, distill or build any ML model without written consent. Reading: caching or replaying outputs as demo fixtures is not training; do not harvest outputs as labels for our own classifier.
- Routing/approval decisions (UP 7, 7.4, 7.5, 9.3; ToS 11): the service is a general-purpose tool, not validated for high-risk use. High-risk includes decisions with legal or similarly significant effects on a person (employment, credit, housing, insurance, education, healthcare access, benefits) and requires human oversight and independent verification. ToS 11 also says Output may be wrong, you must verify it for consequential decisions, and you must use human review suitable to the use case where Output affects individuals. UP 9.3: do not present Output as infallible or professionally certified. Reading: thread routing and grocery/chore-style routing are not in the listed categories, but treat SubQ as ADVISORY only. Anything that spends money, contacts someone, or approves a child's request must be confirmed by a parent/code rule, never by SubQ alone. Conservative.
- AI disclosure (UP 9.1, 9.2; ToS 2): if the Services are deployed in an application that interacts directly with end users, show clear disclosure that they are talking to AI-generated content. Reading: Uhlexa is an AI agent anyway; add an on-screen "AI-assisted" note.
- Children's data (DPA section 1 "Prohibited Data" and 3.3; UP 5.1; ToS 2 last bullet; Privacy Policy section 6): DPA defines Prohibited Data to include health data, government ID numbers, financial account info, biometric identification data, special-category data, and, quoting the DPA, "information relating to children under the age of sixteen (16)" unless the customer has obtained verifiable parental consent as required by applicable data protection law. The customer agrees not to submit Prohibited Data unless Subquadratic agrees in writing. The Privacy Policy says the Services are not directed to children under 18 and Subquadratic does not knowingly collect their data. Reading (conservative): never send real data about a real child (names, ages, school, health, location, photos) to SubQ. For the demo use a fictional family with synthetic names, mark them fictional in the data, and strip fields the decision does not need. Do not add real-family data without verifiable parental consent and written OK from Subquadratic. Also avoid health details (allergies, medications) and card numbers in `state`.
- Images (UP 1.9): no face identification, no biometric categorisation. Not relevant unless Uhlexa sends images; it should not.
- Retention (ToS 4; DPA Sched. 1; docs Data retention): inputs and outputs (and related safety-classification results) deleted within 60 days; content flagged for a possible violation can be kept longer; limited human review can occur on flagged content; no training on customer content. Zero data retention is available on request, subject to their approval; Decisions API works normally under ZDR. Processing is in the US.
- Preview status (ToS 8): preview features are as-is, may change or be discontinued at any time, and are outside normal availability commitments. Reading: build a non-SubQ fallback.
- Publicity (ToS 17 "Publicity"; ToS 9): neither party may use the other's name or logo publicly without prior written consent; logo use only per their Brand Guidelines. AMBIGUOUS whether a plain-text mention of "SubQ" in a README/Devpost counts. Conservative reading: do not put SubQ's name or logo in public submission materials (Devpost text, video, public README) until legal@subq.ai agrees in writing; internal/private docs are fine.
- Payment (ToS 6): self-serve accounts prepay credits by card; at zero balance requests may be blocked (`402`); in-flight requests are still billed. Price changes need 30 days' notice. Billing disputes go to billing@subq.ai within 30 days.
- Order of precedence (ToS preamble): a signed API Services Agreement overrides the self-serve ToS. I could not tell which one TAT's account is under (see Unknowns).

## Design implications for Uhlexa

1. One adapter call = one `/v1/systemone` request, one choice question, `state` passed as the dict (verified). Keep an `INSTRUCTIONS[question_id]` registry because the adapter signature has no instructions field.
2. Run the validator above on every response, with the transport rules; any failure returns a "no decision" `DecisionResult` and the caller takes the safe path (ask the user, or route to the parent). SubQ never gates a side effect by itself.
3. Always send a rendered, minimal `state`: only the fields the question needs, synthetic family data, opaque ids instead of real names where possible, no child ages or health data. This also keeps token cost near zero (166 tokens for the probe).
4. Preview + non-deterministic + undocumented rate limits means the live demo needs a fallback: a deterministic rule-based decider and/or a record-and-replay cache keyed by `(question_id, hash(state, choices))` with recorded real responses as fixtures. Cache is allowed (not training).
5. Set client timeouts ourselves (e.g. 8-10 s total, our choice, not vendor-documented; observed latency is about 0.3-0.4 s) and cap retries at 1-2 for `429`/`5xx`. Do not call it in a polling loop.
6. Server-side only; key in env/secret manager, never in the Board front end, repo or logs. Redact `detail.message` before logging when state is sensitive.
7. Show an "AI-assisted" label in the simulator/Board. Keep SubQ branding out of public submission text pending consent.
8. Choose the confidence cutoff from a handful of labeled Uhlexa examples; start at the docs' 0.6 and treat it as a knob.

## Blockers

- None for building and running the demo today: key works, model is reachable, schema confirmed.
- Not a build blocker, but a submission/compliance gate: public naming/logo of SubQ needs written consent (ToS 17), and any real child or family data must not go to SubQ (DPA Prohibited Data). Use synthetic data only.

## Unknowns (not documented / not verified)

- Rate limits (RPM/TPM, per-key or per-org), `Retry-After` behavior, and what a `429` body looks like.
- Server-side request timeout, and any SLA (preview terms say none).
- Whether outputs are deterministic for identical input: docs say they can vary; I made only one success call so I did not measure it.
- Exact `confidence` formula (one sample matches top1 minus top2 for a 2-option choice; the docs' score example does not).
- Whether `instructions` is strictly required, whether unknown extra fields (e.g. `temperature`, `metadata`) are rejected or ignored, and any character rules for option keys (snake_case worked).
- Whether TAT's account is under the self-serve ToS or a signed API Services Agreement (the latter overrides).
- Whether "SubQ" in plain text counts as public use of their name under ToS 17.
- Third-party claim, unverified: launch coverage said Snap capacity is limited with a waitlist; I saw no such notice in the docs and my key works.
- Current credit balance (a 200 response proves some credit or promo exists, not how much).

## Live run - Task 9 client (2026-10-22)

Run through the real `SnapService` -> `SnapClient` (httpx, a recording transport that kept only SubQ's response), key loaded from `.env.secrets.local` inside the command and never printed. **4 requests reached SubQ, all HTTP 200, no retries.** (Two earlier attempts failed locally with `ConnectError` before any request left the sandbox; they cost nothing.) The probe script was throwaway and is not in the repo; the mocked equivalents are in `tests/contracts/test_subq_contract.py`.

Image input re-read from https://docs.subq.ai/markdown/endpoints/decisions-api.md today (it was already in the facts above): optional top-level `images` array next to `model`/`state`/`questions`, each `{"mediaType": "image/png|image/jpeg|image/webp", "base64": "<one line, no data: prefix>"}`, up to 4 images, 4 MiB decoded each, 8 MiB total, 13 MiB body; bytes that do not match `mediaType`, URLs, `data:` URLs and oversize files return `422 validation_error`. The adapter checks size, mediaType and magic bytes before sending so those 422s never cost a call.

| call | request | latency | answer | input_tokens |
|---|---|---|---|---|
| a | `thread_routing`, options `continue_1`, `continue_2`, `new_thread` (state: new request + two synthetic open threads) | 0.52 s | `continue_1`, p 0.9989, confidence 0.9984 | 255 |
| b | `verify_screenshot` + 416x160 PNG (899 bytes, generated with stdlib, text "milk"), state `{"expected_items": ["milk"]}`, options `verified`, `not_verified`, `needs_human` | 0.40 s | `verified`, p 0.9652, confidence 0.9478 | 244 |
| c | same PNG, state `{"expected_items": ["eggs"]}` (negative control) | 0.41 s | `not_verified`, p 0.9187 (`verified` 0.0110, `needs_human` 0.0702), confidence 0.8781 | 244 |
| d | `thread_routing`, options `continue:<uuid>` x2 + `new_thread` (the spec section 10 key format) | 0.39 s | `continue:00000000-0000-0000-0000-000000001111`, p 0.9982, confidence 0.9973 | 378 |

What this settles:
- **Image input works exactly as documented**, and the answer has the same shape as a text-only choice answer (`type`, `choice`, `probabilities`, `confidence`; no extra fields). Frame size and format for Maritime screenshots (JPEG 1200x750) are well inside the limits.
- **Not a rubber stamp:** the same picture was `verified` for the item it shows and `not_verified` for an item it does not. This is one pair, a sanity check and not an accuracy measurement; the browser runner must still treat `needs_human` / `not_verified` / no decision as "do not claim success".
- **Option keys with a colon and hyphens (`continue:<uuid>`) are accepted and returned verbatim**, so thread routing can use the conversation id directly in the key. (Closes the "character rules for option keys" unknown for this shape; other punctuation is untested.)
- **`confidence` is NOT top1 minus top2.** The earlier one-sample hypothesis is refuted: a 0.9989/0.0010 split gave 0.9984 (top1-top2 = 0.9979), 0.9652/0.0227 gave 0.9478 (0.9425), 0.9187/0.0702 gave 0.8781 (0.8485), 0.9982/0.0018 gave 0.9973 (0.9964). It is always a little above top1-top2. The formula stays undocumented: keep it a vendor score and keep the threshold a settings knob (`SUBQ_MIN_CONFIDENCE`, default 0.6).
- Latency 0.39-0.52 s per call including a fresh TLS connection. Still no rate-limit or `Retry-After` headers on 200s (only security headers).
- Probability sums were within ~1e-7 of 1 on every answer; all four passed the fail-closed validator unchanged.

Not exercised live (covered by the mocked contract tests built from the documented behaviour only): 401/402/403, 429/5xx/timeout retries, 400/422 rejections, determinism for identical input (each call asked a different question).

Adapter status mapping (implemented in `app/providers/subq/client.py`): 200 + valid + confidence >= `SUBQ_MIN_CONFIDENCE` -> `ok`; 200 but invalid / not JSON / low confidence, or 400/422 (never retried, provider stays `ok`) -> `malformed`; 401/402/403, any other 3xx/4xx, connection errors, 429/5xx/timeout after exactly one retry (0.5 s back-off) -> `unavailable` and the provider overlay is marked; our own invalid request (question_id over 100 chars, not 2..255 choices, bad or oversize image, un-encodable state) -> `unavailable` without sending anything and without marking the provider down. Result `detail` carries fixed text plus SubQ's short `error_type` token only; SubQ's `detail.message` can echo request fragments and is dropped.

Privacy note for images: SubQ stores decisions together with their images under the 60-day retention policy (docs, Decisions API "Images"). Only send screenshots of the known-safe public target page (and our own `/demo/sign-in-wall`), never a real third-party sign-in or a page showing real family data.
