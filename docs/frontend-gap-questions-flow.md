# Frontend prompt: gap questions, business questions, and the full onboarding call sequence

Two separate question systems exist and do different jobs. Call the right one
for the right moment — mixing them up is why "suggestions" looked broken.

## 1. The two question systems

### A. Gap questions (`POST /{company_id}/gap-questions`, `POST /{company_id}/gap-answers`)
Narrow and automatic — these ask about the small set of fields the
verification checklist actually needs before a product can be verified:
`company.business_model`, `company.icp`, `company.products`, and per-product
`description`/`key_features`/`main_use_case`. The question text is a fixed
template (`"What is the main use case of {product}?"`), each with up to 3
`suggestions` the backend assembles for you — you never write these
questions, you only render them and the suggestions.

### B. Business questions (`GET`/`PUT /companies/{id}/business-questions`, added just now)
The rich, typed question bank from the GTM crawler's own Business
Intelligence Profile: grouped (`business_fundamentals`, `commercial`,
`sales_marketing`, `gaps`), typed (`text`, `number`, `money`, `percent`,
`choice`, `multi_choice`, `yes_no`), with `choices` and a `suggested` value
where research found one. This is where things like "What's your monthly
marketing budget?" or "Which CRM do you use?" live — information no amount of
website scraping can produce, which is why the crawler calls them "Ask
company" fields. The crawler's own `/v1/companies/{id}/questions` /
`/answers` endpoints are locked behind a shared service bearer token (not
per-user auth), so the frontend was never able to call them directly — these
two proxy endpoints on our own API are the only way to reach them:

```
GET /companies/{company_id}/business-questions
→ { "questions": [ {"id","group","text","type","fills","choices","suggested"} ... ], "ready": true }

PUT /companies/{company_id}/business-questions
  { "answers": [ {"question_id": "...", "status": "answered", "value": ...} ] }
→ forwards to the crawler, records the Q&A in the onboarding conversation, returns the crawler's ack
```

`status` is `"answered"` (with a `value` matching the question's `type` —
money is `{"amount","currency"}`, percent is 0-100, choice/multi_choice is a
string / list of strings from `choices`), `"dont_know"`, or `"skipped"` (no
`value` needed for either). Requires a crawl to have been started for the
company (`404 INTELLIGENCE_NOT_STARTED` otherwise — call this only once
`POST .../context` with `run_intelligence` or the separate intelligence/crawl
flow has actually kicked one off).

## 2. Why suggestions were coming back empty (now fixed)

Gap questions pull suggestions from three places, merged:
1. **Local AI** — the backend's own Groq/Ollama call over whatever pages were
   scraped into `Source` rows, with a hard anti-hallucination rule: a
   suggestion is kept only if its cited quote appears **verbatim** in the
   source text it claims to come from. No matching quote, no suggestion —
   this is deliberate, not a bug, and is why a company with little scraped
   text produces few or no local AI suggestions.
2. **Keyword-matched fallback** — crude regex over the same scraped pages,
   used only when the AI step above found nothing.
3. **GTM crawler suggestion** — the crawler's own graded answer for the same
   field, pulled from its completed Business Intelligence Profile.

(3) was the one silently broken: it looked up the crawler's question list by
ids like `gap:business_model` / `gap:icp`, which the crawler's `/questions`
endpoint never actually produces (those `gap:` ids are for flagging
conflicting or unconfirmed *public facts*, not a 1:1 map onto our own field
names) — so it always returned nothing, and there was no coverage at all for
the per-product fields (`description`, `key_features`, `main_use_case`),
which are most of what gap-questions actually asks about. It's rewritten now
to read the crawler's full profile report directly (the same data this PDF
export shows) and match the local product by name, so a completed crawl's
findings reliably show up as a suggestion instead of silently returning
`None`. Each suggestion carries `"verified_by"`: `"ai"`, `"keyword_match"`, or
`"gtm_crawler"` — use it if you want to visually distinguish how confident a
suggestion is.

## 3. Which AI/API is actually in play, end to end

- **Scraping + the lightweight extraction pipeline** (`POST
  /{company_id}/context` → background job → Groq/Ollama via
  `app/services/llm.py`): fills `Product.description` etc. directly, no user
  interaction. This is what `missing_fields()` checks to decide if gap
  questions are even needed.
- **The GTM deep-crawl** (`POST /companies/{id}/intelligence/crawl`, polled
  via its own job status): an entirely separate, slower, evidence-graded
  research pipeline producing the Business Intelligence Profile — richer, and
  the sole source for `business-questions` and for a chunk of gap-question
  suggestions.
- **Gap-question suggestions** (`POST /{company_id}/gap-questions`): Groq/
  Ollama again, this time constrained to the company's own already-scraped
  text plus the crawler's findings as prior context — never free-floating.
- **Business questions**: no local AI at all — these are the crawler's own
  wording/suggestion step, reached only through the proxy in §1B.

## 4. Full call sequence, start to finish

1. `POST /companies` → create the company.
2. `POST /companies/{id}/context` with `urls`/`files` → starts the
   extraction job; optionally also starts the GTM crawl (see your existing
   `/intelligence/crawl` flow) in parallel.
3. Poll `GET /companies/{id}/status` until it reports a decision — this is
   the single place `research_attention` / `verification_ready` gets posted,
   and it deliberately waits for **both** the extraction job and the
   crawl (if started) to finish before deciding anything.
4. If the conversation shows `research_attention`: the user picks "Add
   Business Context" (→ call `POST /{id}/context` again with more
   files/urls) or "Answer Questions" (→ proceed to gap-questions below).
   Record whichever they picked via `POST /{id}/messages` with
   `{"action": "upload_documents"|"answer_questions"}` — bookkeeping only,
   it does no extraction itself.
5. `POST /{id}/gap-questions` → render each question with its up-to-3
   suggestions; `POST /{id}/gap-answers` to submit answers (or `skip`).
6. Any time after a crawl exists, you can independently surface
   `GET /{id}/business-questions` (e.g. a dedicated "tell us about your
   business" step) and submit with `PUT /{id}/business-questions` — these
   aren't gated by the verification checklist, so this can run in parallel
   with steps 4-5 rather than blocking on them.
7. Once the conversation shows `verification_ready`: `GET /products/{id}`
   for each product, `PATCH /products/{id}` to edit/accept fields (pricing
   included — see [frontend-verification-flow.md](frontend-verification-flow.md)),
   `POST /products/{id}/verify` once blockers clear.
8. Once every standalone product is verified, a `company_profile` message
   appears automatically — show it, then `POST /companies/{id}/complete`.
   `current_user_status` on login/signup now reports `"onboarding_complete"`
   at that point.
